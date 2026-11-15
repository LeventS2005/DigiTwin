"""D115: the Lab's Upload scan (``POST /upload/scan``) feeding Load scan.

The Lab is Demo's six tabs without the right-hand pane, plus one button:
Upload scan sends a CT's bytes to the server, which keeps a read-only temp
copy; the page writes the copy's path into Load scan's path knob (the
``segment`` step, exactly as in Demo). Load scan here runs a FAKE
TotalSegmentator (as in test_segment.py): a tiny Python script pointed to via
MESHFORGE_TOTALSEG that thresholds the staged CT (a bright synthetic tube)
into ``aorta.nii.gz`` and writes a small blob for the ``heart`` context call.
The real TotalSegmentator runs in the Lab walkthrough, not here.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import stat
import sys
from pathlib import Path

import numpy as np
import pytest

nib = pytest.importorskip("nibabel")

from meshforge.io.upload import SUFFIX_HELP, UploadStore, scan_name  # noqa: E402

FAKE_TS = '''#!{python}
import json, os, sys
args = sys.argv[1:]
record = os.environ.get("FAKE_TS_RECORD")
if record:
    with open(record, "a") as fh:
        fh.write(json.dumps(args) + "\\n")
if args == ["--version"]:
    print("2.99.0-fake")
    sys.exit(0)
src = args[args.index("-i") + 1]
out = args[args.index("-o") + 1]
os.makedirs(out, exist_ok=True)
import nibabel as nib
import numpy as np
img = nib.load(src)
mask = (np.asanyarray(img.dataobj) > 100).astype(np.uint8)
rois = args[args.index("--roi_subset") + 1:]
rois = rois[:rois.index("--fast")] if "--fast" in rois else rois[:rois.index("--device")]
for roi in rois:
    m = mask
    if roi != "aorta":  # the heart context: a small blob beside the tube
        m = np.zeros_like(mask)
        m[36:46, 2:12, 20:28] = 1
    nib.save(nib.Nifti1Image(m, img.affine, img.header), os.path.join(out, roi + ".nii.gz"))
'''


@pytest.fixture
def fake_ts(tmp_path, monkeypatch):
    if " " in sys.executable:
        pytest.skip("a shebang line cannot carry a path with spaces")
    exe = tmp_path / "bin" / "TotalSegmentator"
    exe.parent.mkdir()
    exe.write_text(FAKE_TS.format(python=sys.executable))
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    record = tmp_path / "argv.jsonl"
    monkeypatch.setenv("MESHFORGE_TOTALSEG", str(exe))
    monkeypatch.setenv("FAKE_TS_RECORD", str(record))

    def calls():
        if not record.is_file():
            return []
        return [json.loads(line) for line in record.read_text().splitlines()]

    return calls


# a bright tube (HU 300) along the scanner z axis, air elsewhere (test_segment.py's)
SHAPE = (48, 44, 30)
SPACING = 0.8
SLICE = 1.5
TUBE_R_VOX = 9.0
TUBE_C = (24.0, 21.0)


def _tube_ct() -> np.ndarray:
    ii, jj = np.indices(SHAPE[:2])
    disk = (ii - TUBE_C[0]) ** 2 + (jj - TUBE_C[1]) ** 2 <= TUBE_R_VOX**2
    ct = np.full(SHAPE, -100, dtype=np.int16)
    ct[disk] = 300
    return ct


def _write_ct(path: Path, ct: np.ndarray) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    nib.save(nib.Nifti1Image(ct, np.diag([SPACING, SPACING, SLICE, 1.0])), str(path))
    return path


def _sha(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


@pytest.fixture
def client():
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient

    from meshforge.web.app import create_app

    with TestClient(create_app()) as c:
        yield c


def _octet(client, name: str, data: bytes, ctype: str = "application/octet-stream"):
    return client.post("/upload/scan", params={"name": name}, content=data,
                       headers={"Content-Type": ctype})


def test_upload_then_load_scan_over_http(tmp_path, fake_ts, client):
    calls = fake_ts
    ct = _write_ct(tmp_path / "Chest CT (anon).nii.gz", _tube_ct())
    r = _octet(client, ct.name, ct.read_bytes())
    assert r.status_code == 200, r.text
    up = r.json()
    copy = Path(up["path"])
    assert up["name"] == "Chest_CT_anon_.nii.gz" and copy.name == up["name"]
    assert up["sha256"] == _sha(ct) == _sha(copy) and up["bytes"] == ct.stat().st_size
    assert up["reused"] is False and not (copy.stat().st_mode & stat.S_IWUSR)  # read-only
    repo = Path(__file__).resolve().parents[1]
    assert copy.is_relative_to(client.app.state.uploads.root)
    assert not copy.resolve().is_relative_to(repo)  # a temp dir, never the repo (rule 2)
    assert client.get("/state").json()["journal_len"] == 0  # an upload is not a step
    # the copy's path is what the Lab writes into Load scan's path knob (Demo's segment step)
    r = client.post("/step/segment", json={"path": up["path"], "device": "cpu", "fast": 1})
    assert r.status_code == 200, r.text
    rec = client.get("/journal").json()["journal"][-1]
    assert rec["step"] == "segment" and rec["params"]["path"] == up["path"]
    assert rec["input_sha256"] == _sha(ct)  # the copy is byte-for-byte the scan
    # the aorta is outlined, one TotalSegmentator call for it and one for the heart
    assert [c[c.index("--roi_subset") + 1] for c in calls()[1:]] == ["aorta", "heart"]
    # ... and the heart rides beside the aorta as a ghost (the phantom Demo shows)
    geo = client.get("/geometry").json()
    assert [g["kind"] for g in geo["ghost"]] == ["heart"]
    # the same bytes again are the same file
    again = _octet(client, ct.name, ct.read_bytes()).json()
    assert again["reused"] is True and again["path"] == up["path"]


@pytest.mark.parametrize(
    "name, data, match",
    [
        ("slice.dcm", b"DICM" * 50, "Upload scan takes one CT volume file"),
        ("scan.nii.gz", b"not gzip at all", "not a gzip-compressed NIfTI"),
        ("scan.nrrd", b"\x1f\x8b\x08\x00 gzip, not nrrd", "not an NRRD file"),
        ("scan.nii", b"\x00" * 400, "not a NIfTI file"),
        ("scan.nii.gz", b"", "the upload is empty"),
    ],
)
def test_upload_refuses_a_wrong_file_loudly_and_keeps_nothing(client, name, data, match):
    r = _octet(client, name, data)
    assert r.status_code == 400 and match in r.json()["detail"]
    root = client.app.state.uploads.root
    assert list(root.rglob("*")) == []  # no partial or refused file left behind


def test_upload_takes_only_raw_bytes_and_honours_the_ceiling(client, monkeypatch):
    gz = gzip.compress(b"x" * 64)
    # a form or text/plain post (CORS-simple: no preflight) is refused
    for ctype in ("multipart/form-data; boundary=x", "text/plain", "application/x-www-form-urlencoded"):
        assert _octet(client, "a.nii.gz", gz, ctype).status_code == 415
    monkeypatch.setattr("meshforge.io.upload.MAX_SCAN_BYTES", 16)
    r = _octet(client, "a.nii.gz", b"\x1f\x8b" + b"\0" * 62)  # 64 bytes declared
    assert r.status_code == 413 and "upload ceiling" in r.json()["detail"]


def test_upload_store_streams_under_the_ceiling_and_cleans_up(tmp_path, monkeypatch):
    monkeypatch.setattr("meshforge.io.upload.MAX_SCAN_BYTES", 10)
    store = UploadStore()
    up = store.begin("big.nii.gz")
    up.write(b"\x1f\x8b" + b"x" * 6)
    with pytest.raises(ValueError, match="scan ceiling"):
        up.write(b"y" * 8)
    assert list(store.root.rglob("*")) == []
    root = store.root
    store.cleanup()
    assert not root.exists()


def test_the_upload_folder_goes_when_the_server_stops(tmp_path):
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient

    from meshforge.web.app import create_app

    with TestClient(create_app()) as c:
        r = _octet(c, "scan.nii.gz", gzip.compress(b"\0" * 1000))
        assert r.status_code == 200, r.text
        copy = Path(r.json()["path"])
        assert copy.is_file()
    assert not copy.exists() and not copy.parent.parent.exists()


def test_scan_names_are_made_safe():
    assert scan_name("Anev 04.nii.gz") == "Anev_04.nii.gz"
    assert scan_name("../../etc/x.nrrd") == "x.nrrd"
    assert scan_name("C:\\scans\\pt.NII") == "pt.NII"
    long = scan_name("a" * 300 + ".nii.gz")
    assert len(long) == 120 and long.endswith(".nii.gz")
    for bad in ("scan.dcm", ".nii.gz", "", "folder/"):
        with pytest.raises(ValueError, match=SUFFIX_HELP.split(":")[0]):
            scan_name(bad)


def test_the_frontend_calls_the_upload_route_that_exists():
    from meshforge.web.app import create_app
    import meshforge.web as web_pkg

    js = (Path(web_pkg.__file__).parent / "static" / "app.js").read_text()
    assert "/upload/scan" in js
    assert "/upload/scan" in {r.path for r in create_app().routes}

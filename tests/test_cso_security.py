"""Adversarial security fixtures for the CSO / biomedical ingest path
(session 24 security pass — findings in ``docs/security/security-cso-biomedical-findings.md``).

Every fixture is a TINY synthetic crafted input (never the real UCSF family,
rule 1): a malicious ``.cso`` / ``.dcm`` / ``.vtp`` a user opens must fail LOUD
(rule 6), never fail-open into a degenerate-but-"successful" mesh, hang, or OOM.
"""
from __future__ import annotations

import struct
from pathlib import Path

import numpy as np
import pytest

from digitwin.io import cso as csomod
from digitwin.io.cso import cso_to_surface, load_cso

try:
    import pydicom  # noqa: F401
    HAS_PYDICOM = True
except Exception:  # pragma: no cover
    HAS_PYDICOM = False

try:
    import nibabel  # noqa: F401
    HAS_NIBABEL = True
except Exception:  # pragma: no cover
    HAS_NIBABEL = False


# --------------------------------------------------------------------------- #
# minimal Binary CSO writer (the exact tokens load_cso decodes)
# --------------------------------------------------------------------------- #
def _u32(v: int) -> bytes:
    return b"U32_" + struct.pack("<I", v)


def _i32(v: int) -> bytes:
    return b"I32_" + struct.pack("<i", v)


def _str(s: str) -> bytes:
    b = s.encode("utf-8")
    return b"STR_" + struct.pack("<Q", len(b)) + b


def _vec3(x: float, y: float, z: float) -> bytes:
    return b"VEC3" + struct.pack("<3d", x, y, z)


def _ring_bytes(points, normal=(0.0, 0.0, 1.0), *, closed=True,
                module="SoCSOSplineEditor") -> bytes:
    P = np.asarray(points, dtype=float)
    n = len(P)
    out = _str("moduleCreatorString") + _str(module)
    # bbox is metadata only — keep it finite even when a point is poisoned
    fin = P[np.isfinite(P).all(axis=1)] if np.isfinite(P).any() else np.zeros((1, 3))
    bmin, bmax = fin.min(0), fin.max(0)
    out += _vec3(*normal) + _vec3(*bmin) + _vec3(*bmax)
    out += _u32(1) + _u32(0) + _u32(n)
    for k, p in enumerate(P):
        out += _vec3(*p)
        prev = (k - 1) if k > 0 else (n - 1 if closed else -1)
        out += _i32(prev) + _i32(k)
    nseg = n if closed else max(n - 1, 0)
    out += _u32(nseg)
    for _ in range(nseg):
        out += _u32(2) + b"\x00" * 48 + _i32(0) + _i32(0)
    return out


def _write_cso(path: Path, records: list[bytes], *, header_count=None) -> Path:
    hc = len(records) if header_count is None else header_count
    body = b"Binary CSO" + bytes((1, 6, 0)) + _u32(hc) + b"".join(records)
    path.write_bytes(body)
    return path


def _circle(radius, z, npts=16, cx=0.0, cy=0.0):
    a = np.linspace(0, 2 * np.pi, npts, endpoint=False)
    return np.column_stack([cx + radius * np.cos(a), cy + radius * np.sin(a),
                            np.full(npts, float(z))])


def _guide(zs):
    return np.column_stack([np.zeros(len(zs)), np.zeros(len(zs)), np.asarray(zs)])


# --------------------------------------------------------------------------- #
# F-CSO-1 — non-finite (NaN/inf) coordinates must be refused at ingest
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("bad", [np.nan, np.inf, -np.inf])
def test_nan_inf_coordinate_rejected_two_contours(tmp_path, bad):
    """The ≤2-contour path skips the incidental SVD in _order_contours, so a
    non-finite coord would flow straight into the loft (fail-open) unless caught
    at read."""
    r0 = _circle(3.0, 0.0)
    r1 = _circle(3.0, 1.5)
    r1[0, 0] = bad
    p = _write_cso(tmp_path / "bad.cso", [_ring_bytes(r0), _ring_bytes(r1)])
    with pytest.raises(ValueError, match="finite"):
        load_cso(p)


def test_nan_coordinate_rejected_with_guide(tmp_path):
    """The guide path orders via _project_param (not SVD), another route a NaN
    could ride into a 'successful' surface."""
    rings = [_circle(3.0, i * 1.5) for i in range(6)]
    rings[3][2, 1] = np.nan
    recs = [_ring_bytes(r) for r in rings]
    recs.insert(0, _ring_bytes(_guide([i * 1.5 for i in range(6)]),
                               closed=False, module="CSOMarkerGenerator"))
    p = _write_cso(tmp_path / "bad_guide.cso", recs)
    with pytest.raises(ValueError, match="finite"):
        load_cso(p)


def test_nan_plane_normal_rejected(tmp_path):
    recs = [_ring_bytes(_circle(3.0, 0.0)),
            _ring_bytes(_circle(3.0, 1.5), normal=(np.nan, 0.0, 1.0))]
    p = _write_cso(tmp_path / "bad_normal.cso", recs)
    with pytest.raises(ValueError, match="finite"):
        load_cso(p)


def test_clean_cso_still_loads(tmp_path):
    """The finite guard must not reject legitimate finite geometry."""
    recs = [_ring_bytes(_circle(3.0, i * 1.5)) for i in range(6)]
    data = load_cso(_write_cso(tmp_path / "ok.cso", recs))
    assert len(data.contours) == 6
    surf, prov = cso_to_surface(data, method="loft")
    assert prov["watertight"]
    assert np.isfinite(np.asarray(surf.points)).all()


# --------------------------------------------------------------------------- #
# F-CSO-2 — non-finite embedded centreline in a crafted .vtp must be refused
# --------------------------------------------------------------------------- #
def _clean_surface(tmp_path):
    recs = [_ring_bytes(_circle(3.0, i * 1.5)) for i in range(6)]
    data = load_cso(_write_cso(tmp_path / "src.cso", recs))
    surf, _ = cso_to_surface(data, method="loft")
    return surf


@pytest.mark.parametrize("bad", [np.nan, np.inf])
def test_nonfinite_embedded_centerline_rejected(tmp_path, bad):
    from digitwin.geometry import centerline_from_surface

    surf = _clean_surface(tmp_path)
    pts = np.array([[0.0, 0.0, 0.0], [bad, 1.0, 2.0], [3.0, 4.0, 5.0]])
    surf.field_data["meshforge_centerline_points"] = pts
    surf.field_data["meshforge_centerline_radii"] = np.array([1.0, 1.0, 1.0])
    surf.field_data["meshforge_centerline_closed"] = np.array([0])
    # fail-SAFE: return None so the caller recomputes a real skeleton centreline
    assert centerline_from_surface(surf) is None


def test_embed_centerline_rejects_nonfinite(tmp_path):
    from digitwin.geometry import embed_centerline
    from digitwin.geometry.centerline import Centerline

    surf = _clean_surface(tmp_path)
    cl = Centerline(
        points=np.array([[0.0, 0.0, 0.0], [np.nan, 1.0, 2.0]]),
        tangents=np.array([[0.0, 0.0, 1.0], [0.0, 0.0, 1.0]]),
        arc_lengths=np.array([0.0, 1.0]),
        radii=np.array([1.0, 1.0]),
        length=1.0, closed=False, voxel_size=0.0,
    )
    with pytest.raises(ValueError, match="finite"):
        embed_centerline(surf, cl)


def test_embedded_centerline_roundtrip_still_works(tmp_path):
    """A finite embedded centreline must still survive (no regression)."""
    from digitwin.geometry import centerline_from_surface, embed_centerline
    from digitwin.geometry.centerline import Centerline

    surf = _clean_surface(tmp_path)
    cl = Centerline(
        points=np.array([[0.0, 0.0, 0.0], [0.0, 0.0, 1.0], [0.0, 0.0, 2.0]]),
        tangents=np.array([[0.0, 0.0, 1.0]] * 3),
        arc_lengths=np.array([0.0, 1.0, 2.0]),
        radii=np.array([1.0, 1.0, 1.0]),
        length=2.0, closed=False, voxel_size=0.0,
    )
    embed_centerline(surf, cl)
    got = centerline_from_surface(surf)
    assert got is not None and np.allclose(got.points, cl.points)


# --------------------------------------------------------------------------- #
# F-CSO-3 — unbounded record/contour count → O(n²) ordering DoS
# --------------------------------------------------------------------------- #
def test_too_many_records_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(csomod, "MAX_RECORDS", 4, raising=False)
    recs = [_ring_bytes(_circle(3.0, i * 1.0)) for i in range(6)]  # > cap
    p = _write_cso(tmp_path / "many.cso", recs)
    with pytest.raises(ValueError, match="record"):
        load_cso(p)


def test_record_cap_is_generous(tmp_path):
    """The real cap must be far above any real segmentation (~20 rings)."""
    assert getattr(csomod, "MAX_RECORDS", 0) >= 10_000


# --------------------------------------------------------------------------- #
# F-CSO-4 — unbounded n_ring → loft allocation DoS (fires before allocating)
# --------------------------------------------------------------------------- #
def test_huge_n_ring_refused(tmp_path):
    recs = [_ring_bytes(_circle(3.0, i * 1.5)) for i in range(4)]
    data = load_cso(_write_cso(tmp_path / "ok.cso", recs))
    with pytest.raises(ValueError, match="n_ring"):
        cso_to_surface(data, method="loft", n_ring=100_000_000)


# --------------------------------------------------------------------------- #
# F-IMG-5 — DICOM PixelSpacing / origin must be finite & positive
# --------------------------------------------------------------------------- #
def _write_mf_dcm(path, *, spacing=(0.75, 0.75, 0.9), origin=(-10.0, -20.0, -30.0),
                  shape=(4, 5, 5)):
    from pydicom.dataset import Dataset, FileMetaDataset
    from pydicom.uid import CTImageStorage, ExplicitVRLittleEndian, generate_uid

    nf, R, C = shape
    stored = np.zeros((nf, R, C), dtype=np.uint16)
    fm = FileMetaDataset()
    fm.MediaStorageSOPClassUID = CTImageStorage
    fm.MediaStorageSOPInstanceUID = generate_uid()
    fm.TransferSyntaxUID = ExplicitVRLittleEndian
    ds = Dataset()
    ds.file_meta = fm
    ds.SOPClassUID = CTImageStorage
    ds.SOPInstanceUID = fm.MediaStorageSOPInstanceUID
    ds.SeriesInstanceUID = generate_uid()
    ds.FrameOfReferenceUID = generate_uid()
    ds.Modality = "CT"
    ds.Rows, ds.Columns = R, C
    ds.NumberOfFrames = nf
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.BitsAllocated = 16
    ds.BitsStored = 16
    ds.HighBit = 15
    ds.PixelRepresentation = 0
    ds.PixelSpacing = [spacing[1], spacing[0]]  # (row=y, col=x)
    ds.SpacingBetweenSlices = spacing[2]
    ds.SliceThickness = 0.625
    ds.ImagePositionPatient = list(origin)
    ds.ImageOrientationPatient = [1, 0, 0, 0, 1, 0]
    ds.RescaleSlope = 1.0
    ds.RescaleIntercept = -1024.0
    ds.PixelData = stored.tobytes()
    ds.save_as(str(path), enforce_file_format=True)
    return path


@pytest.mark.skipif(not HAS_PYDICOM, reason="pydicom not installed")
@pytest.mark.parametrize("bad_px", [np.nan, np.inf, 0.0, -1.0])
def test_dicom_bad_pixel_spacing_refused(tmp_path, bad_px):
    from digitwin.io.image import dicom_grid

    p = _write_mf_dcm(tmp_path / "bad_px.dcm", spacing=(bad_px, 0.75, 0.9))
    with pytest.raises(ValueError, match="spacing"):
        dicom_grid(p)


@pytest.mark.skipif(not HAS_PYDICOM, reason="pydicom not installed")
def test_dicom_nonfinite_origin_refused(tmp_path):
    from digitwin.io.image import dicom_grid

    p = _write_mf_dcm(tmp_path / "bad_org.dcm", origin=(np.nan, 0.0, 0.0))
    with pytest.raises(ValueError, match="finite|origin|position"):
        dicom_grid(p)


@pytest.mark.skipif(not HAS_PYDICOM, reason="pydicom not installed")
def test_dicom_clean_grid_still_loads(tmp_path):
    from digitwin.io.image import dicom_grid

    dims, sp, org = dicom_grid(_write_mf_dcm(tmp_path / "ok.dcm"))
    assert dims == (5, 5, 4)
    assert np.isfinite(sp).all() and all(s > 0 for s in sp)


# --------------------------------------------------------------------------- #
# F-IMG-6 — directory-series loader: broad except + no MAX_VOXELS ceiling
# --------------------------------------------------------------------------- #
def _write_series_slice(path, *, z, rows=5, cols=5, spacing=(0.75, 0.75)):
    """One single-frame CT slice (no NumberOfFrames) at ImagePositionPatient z —
    the building block of a synthetic DICOM *series directory*."""
    from pydicom.dataset import Dataset, FileMetaDataset
    from pydicom.uid import CTImageStorage, ExplicitVRLittleEndian, generate_uid

    stored = np.zeros((rows, cols), dtype=np.uint16)
    fm = FileMetaDataset()
    fm.MediaStorageSOPClassUID = CTImageStorage
    fm.MediaStorageSOPInstanceUID = generate_uid()
    fm.TransferSyntaxUID = ExplicitVRLittleEndian
    ds = Dataset()
    ds.file_meta = fm
    ds.SOPClassUID = CTImageStorage
    ds.SOPInstanceUID = fm.MediaStorageSOPInstanceUID
    ds.Modality = "CT"
    ds.Rows, ds.Columns = rows, cols
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.BitsAllocated = 16
    ds.BitsStored = 16
    ds.HighBit = 15
    ds.PixelRepresentation = 0
    ds.PixelSpacing = [spacing[1], spacing[0]]  # (row=y, col=x)
    ds.ImagePositionPatient = [0.0, 0.0, float(z)]
    ds.ImageOrientationPatient = [1, 0, 0, 0, 1, 0]
    ds.RescaleSlope = 1.0
    ds.RescaleIntercept = -1024.0
    ds.PixelData = stored.tobytes()
    ds.save_as(str(path), enforce_file_format=True)
    return path


def _write_series_dir(dirpath, n=4, **kw):
    dirpath.mkdir(parents=True, exist_ok=True)
    for k in range(n):
        _write_series_slice(dirpath / f"slice_{k:03d}.dcm", z=float(k), **kw)
    return dirpath


@pytest.mark.skipif(not HAS_PYDICOM, reason="pydicom not installed")
def test_dicom_series_max_voxels_ceiling(tmp_path, monkeypatch):
    """The directory-of-slices loader must refuse an oversized declared stack
    BEFORE reading any pixel data (mirrors the multi-frame path's ceiling)."""
    from digitwin.geometry import centerline as clmod
    from digitwin.io import image as imgmod

    d = _write_series_dir(tmp_path / "series", n=4, rows=5, cols=5)  # 100 voxels
    monkeypatch.setattr(clmod, "MAX_VOXELS", 50, raising=True)
    # sanity: the module the loader imports sees the patched ceiling
    with pytest.raises(ValueError, match="voxel|exceed"):
        imgmod.load_volume(d)


@pytest.mark.skipif(not HAS_PYDICOM, reason="pydicom not installed")
def test_dicom_series_straggler_still_skipped(tmp_path):
    """A genuine non-DICOM straggler (no DICM header → InvalidDicomError) is
    still skipped — the narrowed except must not regress the intended behaviour."""
    from digitwin.io.image import load_volume

    d = _write_series_dir(tmp_path / "series", n=4, rows=5, cols=5)
    (d / "README.txt").write_bytes(b"not a dicom file at all\n")
    (d / ".DS_Store").write_bytes(b"\x00\x01\x02junk")
    array, spacing, origin = load_volume(d)
    assert array.shape == (5, 5, 4)  # the 4 real slices, stragglers dropped


@pytest.mark.skipif(not HAS_PYDICOM, reason="pydicom not installed")
def test_dicom_series_corrupt_read_surfaces(tmp_path, monkeypatch):
    """A dcmread failure that is NOT an InvalidDicomError (a genuinely corrupt
    DICOM, decompression bomb, etc.) must SURFACE, not be silently swallowed
    into a truncated/empty series (the over-broad `except Exception` bug)."""
    import pydicom

    from digitwin.io.image import load_volume

    d = _write_series_dir(tmp_path / "series", n=4, rows=5, cols=5)
    real = pydicom.dcmread
    boom = d / "slice_002.dcm"

    def flaky(path, *a, **k):
        if str(path) == str(boom):
            raise RuntimeError("simulated corrupt DICOM element")
        return real(path, *a, **k)

    monkeypatch.setattr(pydicom, "dcmread", flaky)
    with pytest.raises(RuntimeError, match="corrupt"):
        load_volume(d)


@pytest.mark.skipif(not HAS_PYDICOM, reason="pydicom not installed")
def test_dicom_series_clean_loads(tmp_path):
    """No regression: a clean 4-slice series still loads with correct geometry."""
    from digitwin.io.image import load_volume

    d = _write_series_dir(tmp_path / "series", n=4, rows=5, cols=6,
                          spacing=(0.5, 0.5))
    array, spacing, origin = load_volume(d)
    assert array.shape == (6, 5, 4)               # (x=cols, y=rows, z=slices)
    assert spacing == pytest.approx((0.5, 0.5, 1.0))
    assert origin == pytest.approx((0.0, 0.0, 0.0))


# --------------------------------------------------------------------------- #
# F-IMG-7 — NIfTI get_fdata() allocates the whole array before MAX_VOXELS
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(not HAS_NIBABEL, reason="nibabel not installed")
def test_nifti_max_voxels_ceiling_before_fdata(tmp_path, monkeypatch):
    """The ceiling must fire from the header shape WITHOUT calling get_fdata()
    (which would allocate the full array first)."""
    import nibabel as nib

    from digitwin.geometry import centerline as clmod
    from digitwin.io.image import load_volume

    vol = np.zeros((8, 8, 8), dtype=np.float32)  # 512 voxels
    nii = tmp_path / "big.nii"
    nib.save(nib.Nifti1Image(vol, np.diag([1.0, 1.0, 1.0, 1.0])), str(nii))
    monkeypatch.setattr(clmod, "MAX_VOXELS", 100, raising=True)

    # get_fdata must NOT be reached — trip a sentinel if it is.
    called = {"fdata": False}
    real_fdata = nib.Nifti1Image.get_fdata

    def spy(self, *a, **k):
        called["fdata"] = True
        return real_fdata(self, *a, **k)

    monkeypatch.setattr(nib.Nifti1Image, "get_fdata", spy)
    with pytest.raises(ValueError, match="voxel|exceed"):
        load_volume(nii)
    assert called["fdata"] is False  # refused before any allocation


@pytest.mark.skipif(not HAS_NIBABEL, reason="nibabel not installed")
def test_nifti_within_ceiling_still_loads(tmp_path):
    """No regression: a small NIfTI under the ceiling still round-trips."""
    from digitwin.io.image import load_volume

    vol = np.arange(24, dtype=np.float32).reshape(2, 3, 4)
    nii = tmp_path / "ok.nii"
    import nibabel as nib

    nib.save(nib.Nifti1Image(vol, np.diag([0.5, 1.0, 2.0, 1.0])), str(nii))
    array, spacing, origin = load_volume(nii)
    assert array.shape == (2, 3, 4)
    assert spacing == pytest.approx((0.5, 1.0, 2.0))


# --------------------------------------------------------------------------- #
# F-INFO-8 — compute_import_cso read the file twice (checksum then parse):
# a single-read path removes the TOCTOU window and still records the sha256.
# --------------------------------------------------------------------------- #
def test_load_cso_checked_single_read(tmp_path):
    """load_cso_checked returns (sha256, data) from ONE read; the sha256 must
    equal the sha256 of the file bytes and the parse must match load_cso."""
    import hashlib

    from digitwin.io.cso import load_cso_checked

    recs = [_ring_bytes(_circle(3.0, i * 1.5)) for i in range(6)]
    p = _write_cso(tmp_path / "ok.cso", recs)
    expected = hashlib.sha256(p.read_bytes()).hexdigest()

    sha, data = load_cso_checked(p)
    assert sha == expected
    assert len(data.contours) == 6
    # parity with the two-call public API
    assert [c.id for c in data.contours] == [c.id for c in load_cso(p).contours]


def test_load_cso_checked_reads_once(tmp_path, monkeypatch):
    """The checksum and the parse must come from the SAME buffer — prove it by
    asserting Path.read_bytes is called exactly once for the input file."""
    from pathlib import Path as _Path

    from digitwin.io.cso import load_cso_checked

    recs = [_ring_bytes(_circle(3.0, i * 1.5)) for i in range(4)]
    p = _write_cso(tmp_path / "ok.cso", recs)

    real_read = _Path.read_bytes
    calls = {"n": 0}

    def counting_read(self, *a, **k):
        if str(self) == str(p):
            calls["n"] += 1
        return real_read(self, *a, **k)

    monkeypatch.setattr(_Path, "read_bytes", counting_read)
    sha, data = load_cso_checked(p)
    assert calls["n"] == 1  # single read → no TOCTOU window
    assert len(data.contours) == 4


def test_compute_import_cso_records_matching_sha(tmp_path, monkeypatch):
    """End-to-end: the session step records input_sha256 == sha256(file bytes)
    for a real synthetic CSO (the single-read refactor preserves provenance)."""
    import hashlib

    monkeypatch.chdir(tmp_path)
    from digitwin.pipeline import MeshingSession

    recs = [_ring_bytes(_circle(3.0, i * 1.5)) for i in range(6)]
    p = _write_cso(tmp_path / "arch.cso", recs)
    expected = hashlib.sha256(p.read_bytes()).hexdigest()

    session = MeshingSession()
    session.compute_import_cso(path=str(p), method="loft")
    assert session.input_sha256 == expected
    assert session.surface is not None


# --------------------------------------------------------------------------- #
# session-34 hardening round 2 — the overflow fail-open (fuzz finding) and a
# seeded fuzz lock on the token walk + spline-skip
# --------------------------------------------------------------------------- #
def test_huge_finite_coordinates_refused(tmp_path):
    """Coordinates around 1e154+ are FINITE (they pass every NaN/inf guard)
    but overflow the loft arithmetic (squared distances) into a NaN surface
    reported watertight=True — F-CSO-1's fail-open through the overflow door
    (found by the session-34 fuzz campaign). Must refuse at parse."""
    recs = [_ring_bytes(_circle(3.0, i * 1.5) * 1e200, normal=(0.0, 0.0, 1.0))
            for i in range(4)]
    p = _write_cso(tmp_path / "huge.cso", recs)
    with pytest.raises(ValueError, match="magnitude"):
        load_cso(p)


def test_huge_plane_normal_refused(tmp_path):
    """A ~1e300 plane normal is finite but its norm overflows to inf, and the
    normalisation then silently ZEROES the normal instead of failing."""
    recs = [_ring_bytes(_circle(3.0, 0.0), normal=(1e300, 0.0, 0.0)),
            _ring_bytes(_circle(3.0, 1.5), normal=(0.0, 0.0, 1.0))]
    p = _write_cso(tmp_path / "hugenorm.cso", recs)
    with pytest.raises(ValueError, match="normal"):
        load_cso(p)


def test_fuzz_seeded_parse_loft_contract():
    """Seeded mini-fuzz over the token walk + spline-skip (the in-suite lock
    for the session-34 fuzz campaign, which ran 36k+ mutations): every
    mutation must either refuse with ValueError or yield a FINITE loft
    surface — never another exception type, never a hang-sized allocation,
    never a non-finite 'watertight' mesh."""
    import random

    recs = [_ring_bytes(_circle(3.0, i * 1.5), normal=(0.0, 0.0, 1.0))
            for i in range(6)]
    base = b"Binary CSO" + bytes([1, 6, 0]) + _u32(len(recs)) + b"".join(recs)

    for seed in range(300):
        rng = random.Random(seed)
        b = bytearray(base)
        kind = rng.randrange(4)
        if kind == 0:  # random byte flips
            for _ in range(rng.randint(1, 8)):
                b[rng.randrange(len(b))] = rng.randrange(256)
        elif kind == 1:  # truncation
            b = b[: rng.randrange(len(b))]
        elif kind == 2:  # u32-shaped field corruption
            i = rng.randrange(len(b) - 4)
            b[i:i + 4] = struct.pack(
                "<I", rng.choice([0, 1, 2**31, 2**32 - 1, 10**9]))
        else:  # f64 overwrite, incl. HUGE-FINITE (the overflow door)
            i = rng.randrange(len(b) - 8)
            b[i:i + 8] = struct.pack("<d", rng.choice(
                [1e308, -1e308, 1e200, 0.0, float("nan"), float("inf")]))
        try:
            data = load_cso("fuzz.cso", buf=bytes(b))
            surface, prov = cso_to_surface(data, method="loft", n_ring=16)
        except ValueError:
            continue  # the contract: a loud, typed refusal
        pts = np.asarray(surface.points)
        assert np.isfinite(pts).all(), (
            f"seed {seed}: non-finite loft surface passed as "
            f"watertight={prov.get('watertight')}"
        )


# --------------------------------------------------------------------------- #
# session-34 hardening round 2 — close the --dicom anchor two-read (the last
# D61/D62 BACKLOG item: checksum and grid must come from ONE read)
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(not HAS_PYDICOM, reason="pydicom not installed")
def test_dicom_grid_checked_reads_once(tmp_path, monkeypatch):
    """The rule-1 checksum and the geometry grid must come from the SAME
    buffer: the file is read exactly once and pydicom is never handed the
    PATH (which would be a second, unchecksummed read — TOCTOU)."""
    import hashlib

    from digitwin.io.image import dicom_grid_checked

    p = _write_mf_dcm(tmp_path / "ok.dcm")
    expected_sha = hashlib.sha256(p.read_bytes()).hexdigest()

    real_read = Path.read_bytes
    calls = {"n": 0}

    def counting_read(self, *a, **k):
        if str(self) == str(p):
            calls["n"] += 1
        return real_read(self, *a, **k)

    monkeypatch.setattr(Path, "read_bytes", counting_read)

    real_dcmread = pydicom.dcmread

    def guarded_dcmread(src, *a, **k):
        assert not isinstance(src, (str, Path)), (
            "dcmread was handed the PATH — a second read outside the checksum"
        )
        return real_dcmread(src, *a, **k)

    monkeypatch.setattr(pydicom, "dcmread", guarded_dcmread)

    sha, (dims, spacing, origin) = dicom_grid_checked(p)
    assert calls["n"] == 1
    assert sha == expected_sha
    assert dims == (5, 5, 4)
    assert np.isfinite(spacing).all() and all(s > 0 for s in spacing)


@pytest.mark.skipif(not HAS_PYDICOM, reason="pydicom not installed")
def test_import_cso_dicom_anchor_single_read(tmp_path, monkeypatch):
    """End-to-end: compute_import_cso(dicom=…) anchors on a grid parsed from
    the SAME bytes its recorded dicom_sha256 checksums (pydicom never sees
    the path), and still voxelises the segmentation onto that lattice."""
    import hashlib

    monkeypatch.chdir(tmp_path)
    from digitwin.pipeline import MeshingSession

    # a grid that comfortably contains the tube below
    dcm = _write_mf_dcm(tmp_path / "anchor.dcm", spacing=(0.5, 0.5, 0.5),
                        origin=(-6.0, -6.0, -2.0), shape=(40, 24, 24))
    expected_sha = hashlib.sha256(dcm.read_bytes()).hexdigest()
    recs = [_ring_bytes(_circle(3.0, i * 1.5), normal=(0.0, 0.0, 1.0))
            for i in range(8)]
    cso = _write_cso(tmp_path / "tube.cso", recs)

    real_dcmread = pydicom.dcmread

    def guarded_dcmread(src, *a, **k):
        assert not isinstance(src, (str, Path)), (
            "dcmread was handed the PATH — a second read outside the checksum"
        )
        return real_dcmread(src, *a, **k)

    monkeypatch.setattr(pydicom, "dcmread", guarded_dcmread)

    session = MeshingSession()
    r = session.compute_import_cso(path=str(cso), dicom=str(dcm))
    assert r.summary["cso"]["dicom_sha256"] == expected_sha
    assert r.summary["cso"]["strategy"].startswith("voxel")  # anchor forces voxel
    assert session.surface is not None

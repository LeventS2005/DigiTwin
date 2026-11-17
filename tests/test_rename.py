"""P2 / D117: the meshforge -> digitwin rename is complete AND compatible.

Why each test exists:

* ``import meshforge`` must fail (and no stray ``src/meshforge`` folder may
  exist, which would turn into an importable namespace package); ``digitwin``
  must resolve inside this checkout.
* A repo-wide search: every remaining "meshforge" (any case) in a tracked file
  must be on a tight per-file, per-substring allow-list of deliberate
  compatibility lines or history; no tracked path may contain "meshforge".
* The settings helper reads ``DIGITWIN_<X>`` first, then ``MESHFORGE_<X>``;
  its callers (``find_ccx``, ``foam_runner``) honour both and name the
  variable that supplied a bad value.
* The viewport global in viewport.js and app.js agree and carry the new name.
* Installed console scripts are the digitwin ones; no ``meshforge*`` script.
* A built wheel ships the static Workbench and CSV data under ``digitwin/``.

The report-marker dual read (old ``<!-- meshforge report.html v2 -->`` still
recognised) is tested in tests/test_copilot.py (~lines 645-665); not repeated.
"""

from __future__ import annotations

import importlib.metadata as md
import re
import shutil
import stat
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from digitwin.envvars import env_lookup, env_value

ROOT = Path(__file__).resolve().parents[1]
THIS = "tests/test_rename.py"

# path -> substrings; a hit line is allowed only if its path is listed and the
# line contains one of that path's substrings. None = every line allowed.
ALLOWED: dict[str, tuple[str, ...] | None] = {
    "CHANGELOG.md": None,  # history
    THIS: None,
    "src/digitwin/envvars.py": ("MESHFORGE_", "renamed from meshforge to DigiTwin"),
    "tests/conftest.py": ("MESHFORGE_",),
    "tests/test_cfd.py": ("MESHFORGE_",),
    "tests/test_segment.py": ("MESHFORGE_",),
    "tests/test_solve.py": ("MESHFORGE_",),
    "scripts/e2e_all_features.sh": ("MESHFORGE_",),
    ".github/SECURITY.md": ("MESHFORGE_",),
    "docs/APP_GUIDE.md": ("MESHFORGE_",),
    "docs/TEST_PLAN.md": ("MESHFORGE_",),
    "docs/security/security-analysis-findings.md": ("MESHFORGE_",),
    "packaging/README.md": ("MESHFORGE_",),
    # saved-file field-data keys (existing .vtp files must keep loading)
    "src/digitwin/geometry/centerline.py": ("meshforge_centerline_",),
    "tests/test_cso_security.py": ("meshforge_centerline_",),
    "docs/security/security-cso-biomedical-findings.md": ("meshforge_centerline_",),
    # old report marker, still recognised on read
    "src/digitwin/copilot/report.py": ("<!-- meshforge report.html v2 -->",),
    "tests/test_copilot.py": (
        "<!-- meshforge report.html v2 -->",
        '"meshforge" not in REPORT_HTML_MARKER',
    ),
}


def disallowed(hits: list[str]) -> list[str]:
    """Hit lines (``path:line:text``) not covered by ALLOWED."""
    bad = []
    for hit in hits:
        path, _, rest = hit.partition(":")
        _lineno, _, text = rest.partition(":")
        allowed = ALLOWED.get(path, ())
        if path in ALLOWED and allowed is None:
            continue
        if allowed and any(s in text for s in allowed):
            continue
        bad.append(f"{path}:{rest}")
    return bad


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout


# --- a/b: the old package is gone, the new one is ours ----------------------


def test_import_meshforge_fails(tmp_path):
    proc = subprocess.run(
        [sys.executable, "-I", "-c", "import meshforge"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert proc.returncode != 0
    assert "No module named 'meshforge'" in proc.stderr
    assert not (ROOT / "src" / "meshforge").exists()


def test_digitwin_resolves_inside_repo():
    import digitwin

    pkg = Path(digitwin.__file__).resolve()
    assert (ROOT / "src" / "digitwin") in pkg.parents


# --- c: no stray "meshforge" ------------------------------------------------


def test_no_stray_meshforge_in_tracked_files():
    if not (ROOT / ".git").exists():
        pytest.skip("not a git checkout")
    names = [p for p in _git("ls-files").splitlines() if "meshforge" in p.lower()]
    assert not names, f"tracked paths named meshforge: {names}"
    grep = subprocess.run(
        ["git", "grep", "-n", "-i", "-I", "meshforge"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert grep.returncode in (0, 1), grep.stderr
    bad = disallowed(grep.stdout.splitlines())
    assert not bad, "unlisted 'meshforge' mentions:\n" + "\n".join(bad)


def test_allow_list_checker_bites():
    hits = [
        "CHANGELOG.md:3:meshforge was renamed",
        "src/digitwin/envvars.py:17:PREFIXES = ('DIGITWIN_', 'MESHFORGE_')",
        "src/digitwin/cli.py:9:import meshforge",  # path not listed
        "src/digitwin/envvars.py:30:# meshforge tool",  # listed path, wrong line
        "tests/test_solve.py:5:from meshforge import x",  # wrong substring
    ]
    assert disallowed(hits) == [
        "src/digitwin/cli.py:9:import meshforge",
        "src/digitwin/envvars.py:30:# meshforge tool",
        "tests/test_solve.py:5:from meshforge import x",
    ]


# --- d: settings helper -----------------------------------------------------


@pytest.fixture
def clean_env(monkeypatch):
    for name in ("DIGITWIN_RENTEST", "MESHFORGE_RENTEST"):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def test_env_new_name_wins(clean_env):
    clean_env.setenv("DIGITWIN_RENTEST", "new")
    clean_env.setenv("MESHFORGE_RENTEST", "old")
    assert env_value("RENTEST") == "new"
    assert env_lookup("RENTEST") == ("DIGITWIN_RENTEST", "new")


def test_env_old_alone_works(clean_env):
    clean_env.setenv("MESHFORGE_RENTEST", "old")
    assert env_value("RENTEST") == "old"
    assert env_lookup("RENTEST") == ("MESHFORGE_RENTEST", "old")


def test_env_empty_new_falls_through(clean_env):
    clean_env.setenv("DIGITWIN_RENTEST", "")
    clean_env.setenv("MESHFORGE_RENTEST", "old")
    assert env_lookup("RENTEST") == ("MESHFORGE_RENTEST", "old")


def test_env_neither_gives_default(clean_env):
    assert env_value("RENTEST") is None
    assert env_value("RENTEST", "dflt") == "dflt"
    assert env_lookup("RENTEST") == (None, None)


# --- e: callers -------------------------------------------------------------


def _exe(path: Path) -> str:
    path.write_text("#!/bin/sh\n")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


def test_find_ccx_old_name_alone(monkeypatch, tmp_path):
    from digitwin.solve.runner import find_ccx

    monkeypatch.delenv("DIGITWIN_CCX", raising=False)
    old = _exe(tmp_path / "ccx_old")
    monkeypatch.setenv("MESHFORGE_CCX", old)
    assert find_ccx() == old


def test_find_ccx_new_name_wins(monkeypatch, tmp_path):
    from digitwin.solve.runner import find_ccx

    old = _exe(tmp_path / "ccx_old")
    new = _exe(tmp_path / "ccx_new")
    monkeypatch.setenv("MESHFORGE_CCX", old)
    monkeypatch.setenv("DIGITWIN_CCX", new)
    assert find_ccx() == new


def test_foam_old_name_missing_file_names_old_variable(monkeypatch, tmp_path):
    from digitwin.solve.foam_runner import find_foam

    monkeypatch.delenv("DIGITWIN_OPENFOAM", raising=False)
    monkeypatch.setenv("MESHFORGE_OPENFOAM", str(tmp_path / "missing"))
    with pytest.raises(RuntimeError, match="MESHFORGE_OPENFOAM"):
        find_foam()


def test_loud_errors_name_the_new_variables(monkeypatch, tmp_path):
    from digitwin.solve import solve_deck
    from digitwin.solve.foam_runner import run_foam_app

    for name in ("CCX", "OPENFOAM"):
        monkeypatch.delenv("DIGITWIN_" + name, raising=False)
        monkeypatch.delenv("MESHFORGE_" + name, raising=False)
    monkeypatch.setenv("PATH", str(tmp_path))  # hide any real tool
    monkeypatch.setattr("digitwin.solve.runner._CCX_FALLBACK", tmp_path / "absent")
    deck = tmp_path / "job.inp"
    deck.write_text("*HEADING\nfake\n")
    with pytest.raises(RuntimeError, match="DIGITWIN_CCX"):
        solve_deck(deck, tmp_path / "out")
    with pytest.raises(RuntimeError, match="DIGITWIN_OPENFOAM"):
        run_foam_app("checkMesh", tmp_path)


# --- f: viewport global -----------------------------------------------------


def test_viewport_global_parity():
    static = ROOT / "src" / "digitwin" / "web" / "static"
    defined = re.findall(
        r"window\.(\w*Viewport)\s*=", (static / "viewport.js").read_text()
    )
    used = set(re.findall(r"window\.(\w*Viewport)", (static / "app.js").read_text()))
    assert len(defined) == 1, defined
    assert used == {defined[0]}
    assert "DigiTwin" in defined[0]


# --- g: console scripts -----------------------------------------------------


def test_console_scripts():
    try:
        md.distribution("digitwin")
    except md.PackageNotFoundError:
        pytest.skip("digitwin dist not installed")
    console = {e.name: e.value for e in md.entry_points(group="console_scripts")}
    gui = {e.name: e.value for e in md.entry_points(group="gui_scripts")}
    for name in ("digitwin", "digitwin-web", "digitwin-desktop"):
        assert console.get(name, "").startswith("digitwin."), name
    assert gui.get("digitwin-gui", "").startswith("digitwin.")
    assert not [n for n in (*console, *gui) if n.lower().startswith("meshforge")]


# --- h: wheel package data --------------------------------------------------


def test_wheel_ships_package_data(tmp_path):
    if not (ROOT / ".git").exists():
        pytest.skip("not a git checkout")
    before = _git("status", "--porcelain")
    src = tmp_path / "src_tree"
    src.mkdir()
    archive = subprocess.run(
        ["git", "archive", "HEAD"], cwd=ROOT, capture_output=True, check=True
    ).stdout
    subprocess.run(["tar", "-x", "-C", str(src)], input=archive, check=True)
    out = tmp_path / "wheelhouse"
    out.mkdir()
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pip", "wheel", str(src), "--no-deps",
             "--no-build-isolation", "-w", str(out)],
            capture_output=True, text=True, timeout=300,
        )
    except subprocess.TimeoutExpired:
        pytest.skip("wheel build timed out")
    wheels = list(out.glob("*.whl"))
    if proc.returncode != 0 or not wheels:
        pytest.skip(f"wheel build unavailable: {proc.stderr[-300:]}")
    names = zipfile.ZipFile(wheels[0]).namelist()
    for need in ("index.html", "app.js", "viewport.js", "styles.css"):
        assert f"digitwin/web/static/{need}" in names, need
    assert any(
        n.startswith("digitwin/data/") and n.endswith(".csv") for n in names
    )
    assert not [n for n in names if n.lower().startswith("meshforge")]
    assert _git("status", "--porcelain") == before

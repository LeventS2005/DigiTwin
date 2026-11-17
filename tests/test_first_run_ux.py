"""First-run Workbench usability regressions (session 41).

Every case here comes from a hands-on walkthrough with a real clinical .cso.
None of it touches the meshing math — these lock the *front door*: which
importer a suffix is routed to, what a mistyped path reports, whether an
unaffordable SDF grid is refused before any work is paid for, and the
explicit panel order the Workbench lays out.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from digitwin.io.load import dedicated_importer, load_surface
from digitwin.io.paths import normalize_path, normalize_path_str
from digitwin.pipeline import STEP_KNOBS, STEP_ORDER, MeshingSession


# ---- fix 1: extension routing ------------------------------------------
@pytest.mark.parametrize(
    "suffix, panel, subcommand",
    [
        (".cso", "import_cso", "import-cso"),
        (".dxf", "import_cad", "import-cad"),
        (".dwg", "import_cad", "import-cad"),
        (".step", "import_cad3d", "import-cad3d"),
        (".stp", "import_cad3d", "import-cad3d"),
        (".iges", "import_cad3d", "import-cad3d"),
        (".igs", "import_cad3d", "import-cad3d"),
        (".brep", "import_cad3d", "import-cad3d"),
        (".npy", "import_image", "import-image"),
        (".nii", "import_image", "import-image"),
        (".nii.gz", "import_image", "import-image"),
        (".dcm", "import_image", "import-image"),
    ],
)
def test_known_suffix_names_its_own_importer(tmp_path, suffix, panel, subcommand):
    """"Load surface" on a format with a dedicated importer must name that
    importer — not leak pyvista's "not able to be automatically read"."""
    target = tmp_path / f"scan{suffix}"
    target.write_bytes(b"not really a mesh")  # never opened: routing is by suffix
    with pytest.raises(ValueError) as excinfo:
        load_surface(target)
    message = str(excinfo.value)
    assert suffix in message
    assert panel in message
    assert f"meshforge {subcommand}" in message
    assert "pyvista" not in message.lower()


def test_routing_is_by_suffix_not_by_existence(tmp_path):
    """A .cso that does not exist still names import_cso: the user's real
    mistake is the front door, not the typo."""
    with pytest.raises(ValueError, match="import_cso"):
        load_surface(tmp_path / "never-written.cso")


def test_nii_gz_beats_bare_nii():
    """Longest suffix wins, so .nii.gz can never resolve as some other rule."""
    assert dedicated_importer("scan.nii.gz")[0] == ".nii.gz"
    assert dedicated_importer("scan.nii")[0] == ".nii"
    assert dedicated_importer("mesh.stl") is None
    assert dedicated_importer("MESH.CSO")[0] == ".cso"  # case-insensitive


def test_unknown_suffix_still_fails_loudly(tmp_path):
    """Routing must not turn into a permissive allow-list: an unknown format
    still errors, it is just no longer misdirected."""
    bogus = tmp_path / "mystery.qqq"
    bogus.write_bytes(b"\x00\x01\x02")
    with pytest.raises(Exception) as excinfo:
        load_surface(bogus)
    assert not isinstance(excinfo.value, FileNotFoundError)


def test_supported_surface_still_loads(tmp_path):
    """The whole point is that .stl/.vtp/.ply are untouched by fix 1."""
    import pyvista as pv

    out = tmp_path / "sphere.stl"
    pv.Sphere().triangulate().save(out)
    mesh = load_surface(out)
    assert mesh.n_points > 0 and mesh.n_cells > 0


# ---- fix 3: path normalisation + a reachable not-found message ----------
@pytest.mark.parametrize(
    "raw, expected",
    [
        ("  /a/b.stl  ", "/a/b.stl"),                    # trailing/leading space
        ('"/path/with spaces/f.cso"', "/path/with spaces/f.cso"),  # quotes
        ("'/path/with spaces/f.cso'", "/path/with spaces/f.cso"),  # single quotes
        (r"/path/Example\ CSO\ Files/f.cso", "/path/Example CSO Files/f.cso"),
        (r"/a/b\(1\).stl", "/a/b(1).stl"),               # escaped parens
        ("", ""),                                         # empty stays empty
    ],
)
def test_normalize_path_str(raw, expected):
    assert normalize_path_str(raw) == expected


def test_normalize_path_expands_home():
    assert normalize_path_str("~/x.stl") == str(Path.home() / "x.stl")
    assert normalize_path("~/x.stl") == Path.home() / "x.stl"


def test_normalize_path_is_idempotent():
    raw = r'  "~/Example\ CSO\ Files/f.cso" '
    once = normalize_path_str(raw)
    assert normalize_path_str(once) == once


def test_backslash_before_an_ordinary_char_is_left_alone():
    """We un-escape shell escapes; we do not rewrite arbitrary backslashes."""
    assert normalize_path_str(r"C:\Users\data.stl") == r"C:\Users\data.stl"


@pytest.mark.parametrize(
    "shape",
    [
        "~/definitely-not-here-{}.stl",
        '"/tmp/definitely not here {}.stl"',
        r"/tmp/definitely\ not\ here\ {}.stl",
        "/tmp/definitely-not-here-{}.stl   ",
    ],
)
def test_missing_file_says_surface_file_not_found(shape):
    """The friendly guard in load_surface used to be dead code: file_sha256
    opened the file first and raised a bare [Errno 2]."""
    session = MeshingSession()
    with pytest.raises(FileNotFoundError) as excinfo:
        session.load(shape.format("x"))
    message = str(excinfo.value)
    assert message.startswith("surface file not found: ")
    assert "Errno" not in message


def test_load_accepts_every_common_path_habit(tmp_path):
    """All four rows of the walkthrough's failing table now load."""
    import pyvista as pv

    directory = tmp_path / "Example CSO Files"
    directory.mkdir()
    target = directory / "tube.stl"
    pv.Sphere().triangulate().save(target)

    variants = [
        str(target),
        f'"{target}"',
        str(target).replace(" ", r"\ "),
        f"{target}   ",
    ]
    for raw in variants:
        session = MeshingSession()
        session.load(raw)
        assert session.input_path == target, raw
        assert session.surface.n_points > 0


def test_home_shortcut_loads(tmp_path, monkeypatch):
    import pyvista as pv

    monkeypatch.setenv("HOME", str(tmp_path))
    target = tmp_path / "aorta.stl"
    pv.Sphere().triangulate().save(target)
    session = MeshingSession()
    session.load("~/aorta.stl")
    assert session.input_path == target


def test_file_sha256_never_leaks_a_bare_errno():
    """Every rule-1 bracket hashes FIRST, so the CLI used to report
    `[Errno 2] No such file or directory` for a simple typo."""
    from digitwin.io import file_sha256

    with pytest.raises(FileNotFoundError) as excinfo:
        file_sha256("~/definitely/not/here.stl")
    message = str(excinfo.value)
    assert message == f"file not found: {Path.home()}/definitely/not/here.stl"
    assert "Errno" not in message


def test_file_sha256_still_hashes_a_real_file(tmp_path):
    import hashlib

    from digitwin.io import file_sha256

    target = tmp_path / "payload.bin"
    target.write_bytes(b"meshforge")
    assert file_sha256(f'"{target}" ') == hashlib.sha256(b"meshforge").hexdigest()


def test_workfile_paths_are_normalised(tmp_path):
    from digitwin.pipeline import load_workfile

    session = MeshingSession()
    session.generate_synth("straight_tube")
    written = session.save_workfile(f'"{tmp_path / "run.mfwork"}"')
    assert written == tmp_path / "run.mfwork"
    assert load_workfile(f"{written}  ").journal


# ---- fix 4: the voxel budget is refused up front, in the user's units ----
def _budget_error(bbox_mm: float, thickness: float) -> str:
    import pyvista as pv

    from digitwin.geometry.sdf import sdf_offset_surface

    surface = pv.Sphere(radius=bbox_mm / 2.0).triangulate()
    with pytest.raises(ValueError) as excinfo:
        sdf_offset_surface(surface, thickness)
    return str(excinfo.value)


def test_voxel_budget_error_names_thickness_not_spacing():
    """"spacing" is not a knob on any panel — the actionable knob is
    thickness, and the message must say so and quote a value that works."""
    message = _budget_error(100.0, 0.05)
    assert "thickness" in message
    assert not re.search(r"increase spacing", message)
    quoted = re.search(r"raise thickness: ([0-9.eE+-]+) or more fits", message)
    assert quoted, message
    assert float(quoted.group(1)) > 0.05


def test_voxel_budget_error_prints_plain_ints():
    """It used to format numpy scalars: 'SDF grid (np.int64(596), ...)'."""
    message = _budget_error(100.0, 0.05)
    assert "np.int64" not in message
    assert re.search(r"SDF grid \d+ x \d+ x \d+ = \d+ voxels", message), message


def test_quoted_minimum_thickness_actually_fits():
    """The number in the message is checked, not guessed."""
    import numpy as np
    import pyvista as pv

    from digitwin.geometry.centerline import MAX_VOXELS
    from digitwin.geometry.sdf import _grid_shape, min_thickness_for_budget

    bounds = pv.Sphere(radius=50.0).bounds
    t_min = min_thickness_for_budget(bounds)
    _, dims = _grid_shape(bounds, t_min, t_min / 3.0)
    assert float(np.prod(dims)) <= MAX_VOXELS
    # ...and it is genuinely near the boundary, not a lazy huge number.
    below = t_min * 0.8
    _, dims_below = _grid_shape(bounds, below, below / 3.0)
    assert float(np.prod(dims_below)) > MAX_VOXELS


def test_voxel_budget_is_refused_before_the_expensive_call(monkeypatch):
    """The dims product is known from the bounding box alone, so an
    unaffordable grid must never reach compute_implicit_distance."""
    import pyvista as pv

    from digitwin.geometry.sdf import sdf_offset_surface

    def explode(*args, **kwargs):  # pragma: no cover - must never run
        raise AssertionError("compute_implicit_distance ran despite the budget")

    monkeypatch.setattr(pv.ImageData, "compute_implicit_distance", explode)
    with pytest.raises(ValueError, match="raise thickness"):
        sdf_offset_surface(pv.Sphere(radius=50.0).triangulate(), 0.05)


def test_explicit_spacing_error_mentions_spacing():
    """A caller who DID pass spacing is told about spacing — the thickness
    wording is for the panel user, who has no spacing knob."""
    import pyvista as pv

    from digitwin.geometry.sdf import sdf_offset_surface

    surface = pv.Sphere(radius=50.0).triangulate()
    with pytest.raises(ValueError) as excinfo:
        sdf_offset_surface(surface, 10.0, spacing=0.01)
    assert "spacing" in str(excinfo.value)


def test_spacing_must_be_positive():
    import pyvista as pv

    from digitwin.geometry.sdf import sdf_offset_surface

    with pytest.raises(ValueError, match="spacing must be positive"):
        sdf_offset_surface(pv.Sphere().triangulate(), 1.0, spacing=0.0)


# ---- fix 2: the explicit panel order -----------------------------------
def test_step_order_covers_step_knobs_exactly():
    assert set(STEP_ORDER) == set(STEP_KNOBS)
    assert len(STEP_ORDER) == len(set(STEP_ORDER))


def test_imports_lead_the_panel_order():
    """A run that starts from a file starts with an import_* panel, so those
    sit directly under source rather than 7th-10th."""
    imports = [s for s in STEP_ORDER if s.startswith("import_")]
    assert STEP_ORDER[: len(imports)] == tuple(imports)
    assert set(imports) == {
        "import_cad",
        "import_cad3d",
        "import_image",
        "import_cso",
    }


def test_schema_json_is_served_in_display_order():
    from digitwin.web.schema import schema_json, step_order

    assert list(schema_json()) == list(STEP_ORDER) == step_order()


# ---- fix 8: no internal spec tags in user-facing help -------------------
def test_no_milestone_tags_in_knob_help():
    tagged = [
        f"{step}.{knob}"
        for step, knobs in STEP_KNOBS.items()
        for knob, (_, _, help_text) in knobs.items()
        if re.search(r"\(V\d+", help_text)
    ]
    assert tagged == []


def test_no_milestone_tags_in_cli_help():
    source = Path("src/digitwin/cli.py").read_text()
    assert re.search(r"\(V\d+", source) is None

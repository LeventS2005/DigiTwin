"""V16 IGES/STEP import: OCC-generated fixtures (no user files), analytic
volume/area truths, loud branches, session + CLI e2e. Everything that
touches gmsh skips cleanly when it is absent (trap #16)."""
from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import pytest

from meshforge.io.cad3d import load_solid

try:
    import gmsh  # noqa: F401

    HAS_GMSH = True
except ImportError:  # pragma: no cover
    HAS_GMSH = False

needs_gmsh = pytest.mark.skipif(not HAS_GMSH, reason="gmsh not available")


@pytest.fixture(scope="module")
def cad_files(tmp_path_factory) -> dict[str, Path]:
    """STEP box (1x1x1), STEP cylinder (r=1, h=2), IGES box — via gmsh OCC."""
    if not HAS_GMSH:
        pytest.skip("gmsh not available")
    root = tmp_path_factory.mktemp("cad3d")
    out: dict[str, Path] = {}

    def _write(name: str, builder) -> None:
        gmsh.initialize()
        try:
            gmsh.option.setNumber("General.Terminal", 0)
            gmsh.model.add(name)
            builder()
            gmsh.model.occ.synchronize()
            gmsh.write(str(root / name))
        finally:
            gmsh.finalize()
        out[name] = root / name

    _write("box.step", lambda: gmsh.model.occ.addBox(0, 0, 0, 1, 1, 1))
    _write("cyl.step", lambda: gmsh.model.occ.addCylinder(0, 0, 0, 0, 0, 2, 1))
    _write("box.iges", lambda: gmsh.model.occ.addBox(0, 0, 0, 1, 1, 1))
    return out


@needs_gmsh
def test_step_box_watertight_and_exact(cad_files):
    surface, prov = load_solid(cad_files["box.step"])
    assert surface.n_cells > 0 and surface.is_all_triangles
    assert prov["format"] == "step"
    assert prov["n_volumes"] == 1 and prov["n_surfaces"] == 6
    assert surface.is_manifold
    assert float(surface.volume) == pytest.approx(1.0, rel=0.01)
    assert float(surface.area) == pytest.approx(6.0, rel=0.01)
    np.testing.assert_allclose(prov["bbox_min"], [0, 0, 0], atol=1e-6)
    np.testing.assert_allclose(prov["bbox_max"], [1, 1, 1], atol=1e-6)


@needs_gmsh
def test_step_cylinder_volume(cad_files):
    surface, prov = load_solid(cad_files["cyl.step"], mesh_size_factor=0.5)
    assert prov["n_volumes"] == 1
    assert surface.is_manifold
    # pi r^2 h with faceting deficit under 2% at this resolution.
    assert float(surface.volume) == pytest.approx(2 * math.pi, rel=0.02)


@needs_gmsh
def test_iges_box_second_format(cad_files):
    surface, prov = load_solid(cad_files["box.iges"])
    assert prov["format"] == "iges"
    assert float(surface.volume) == pytest.approx(1.0, rel=0.01)


@needs_gmsh
def test_mesh_size_knob_changes_resolution(cad_files):
    coarse, _ = load_solid(cad_files["box.step"], mesh_size=0.5)
    fine, _ = load_solid(cad_files["box.step"], mesh_size=0.1)
    assert fine.n_cells > 2 * coarse.n_cells


def test_loud_branches_without_gmsh(tmp_path):
    """File/suffix/knob validation fires BEFORE the gmsh import."""
    with pytest.raises(FileNotFoundError):
        load_solid(tmp_path / "missing.step")
    stl = tmp_path / "thing.stl"
    stl.write_text("solid x\nendsolid x\n")
    with pytest.raises(ValueError, match="unsupported 3-D CAD suffix"):
        load_solid(stl)
    step = tmp_path / "x.step"
    step.write_text("junk")
    with pytest.raises(ValueError, match="mutually exclusive"):
        load_solid(step, mesh_size=1.0, mesh_size_factor=0.5)
    with pytest.raises(ValueError, match="positive"):
        load_solid(step, mesh_size=-1.0)


@needs_gmsh
def test_garbage_step_is_loud(tmp_path):
    bad = tmp_path / "garbage.step"
    bad.write_text("ISO-10303-21;\nthis is not a step file\n")
    with pytest.raises(ValueError, match="could not import|no surfaces"):
        load_solid(bad)


@needs_gmsh
def test_session_step_and_replay(cad_files, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from meshforge.pipeline import MeshingSession, load_workfile, replay

    session = MeshingSession()
    result = session.compute_import_cad3d(path=str(cad_files["box.step"]))
    assert session.surface is not None and session.input_sha256
    assert result.summary["cad3d"]["n_volumes"] == 1
    assert result.summary["watertight"] is True
    assert session.journal[-1]["step"] == "import_cad3d"

    wf = session.save_workfile(tmp_path / "s.mfwork")
    fresh = load_workfile(wf)
    results = replay(fresh)
    assert [r.step for r in results] == ["import_cad3d"]
    assert fresh.surface.n_cells == session.surface.n_cells

    with pytest.raises(ValueError, match="needs a STEP"):
        session.compute_import_cad3d()


@needs_gmsh
def test_cli_import_cad3d_e2e(cad_files, tmp_path):
    from meshforge.cli import main

    outdir = tmp_path / "out"
    rc = main([
        "import-cad3d", str(cad_files["cyl.step"]), "--outputs", str(outdir),
    ])
    assert rc == 0
    vtp = outdir / "cyl_surface.vtp"
    prov = json.loads((outdir / "cyl_cad3d.json").read_text())
    assert vtp.is_file()
    assert prov["check"]["mesh_ready"] is True
    assert prov["n_volumes"] == 1


@needs_gmsh
def test_cli_full_pipeline_step_to_volmesh(cad_files, tmp_path):
    """The new-format-to-volume path: STEP cylinder -> surface -> swept wall."""
    from meshforge.cli import main

    outdir = tmp_path / "out"
    rc = main(["import-cad3d", str(cad_files["cyl.step"]), "--outputs", str(outdir)])
    assert rc == 0
    rc = main([
        "volmesh", str(outdir / "cyl_surface.vtp"), "--thickness", "0.15",
        "--n-theta", "16", "--n-axial", "12", "--n-layers", "2",
        "--outputs", str(outdir),
    ])
    assert rc == 0
    summary = json.loads(
        (outdir / "cyl_surface_wall_swept.summary.json").read_text()
    )
    assert summary["quality"]["n_inverted"] == 0


def test_cli_import_cad3d_error_path(tmp_path, capsys):
    from meshforge.cli import main

    rc = main(["import-cad3d", str(tmp_path / "nope.step")])
    assert rc == 1
    assert "nope.step" in capsys.readouterr().err

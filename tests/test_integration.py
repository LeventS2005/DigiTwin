"""V12 integration: session wiring for the roadmap-v2 features."""
from __future__ import annotations

import numpy as np
import pytest

from meshforge.pipeline import MeshingSession, replay


@pytest.fixture()
def rect_hole_dxf(tmp_path):
    ezdxf = pytest.importorskip("ezdxf")
    doc = ezdxf.new()
    doc.header["$INSUNITS"] = 4
    msp = doc.modelspace()
    for a, b in (((0, 0), (10, 0)), ((10, 0), (10, 8)), ((10, 8), (0, 8)), ((0, 8), (0, 0))):
        msp.add_line(a, b)
    for a, b in (((3, 3), (3, 6)), ((3, 6), (7, 6)), ((7, 6), (7, 3)), ((7, 3), (3, 3))):
        msp.add_line(a, b)
    path = tmp_path / "part.dxf"
    doc.saveas(path)
    return path


def test_session_import_cad_then_blocks(rect_hole_dxf):
    s = MeshingSession()
    r1 = s.compute_import_cad(path=str(rect_hole_dxf))
    assert r1.summary["mesh_ready"] and r1.summary["units"] == "mm"
    assert r1.render_hints["kind"] == "polyline"

    r2 = s.compute_blocks(target_size=1.0, thickness=2.0, n_layers=2)
    assert s.volmesh is not None
    assert r2.summary["quality"]["n_inverted"] == 0
    assert r2.summary["quality"]["total_volume"] == pytest.approx(136.0)
    # ... and the deck/save machinery sees the block mesh like any volmesh.
    assert set(s.volmesh.boundary) == {"front", "back", "outer_rim", "hole_rims"}


def test_session_blocks_requires_profile():
    with pytest.raises(RuntimeError, match="compute_import_cad"):
        MeshingSession().compute_blocks(target_size=1.0)


def test_session_repair_invalidates_downstream():
    from meshforge.quality import degrade_surface
    from meshforge.synth import generate

    s = MeshingSession()
    s.generate_synth("straight_tube", n_theta=32, n_axial=21)
    # Noise-only degradation keeps the topology watertight so the
    # centerline still runs; structural fixes are covered in test_repair.py
    # — THIS test pins the invalidation semantics.
    s.surface = degrade_surface(s.surface, noise=0.005, seed=5)
    s.compute_centerline(voxel_size=0.5, n_samples=80)
    assert s.centerline is not None

    r = s.compute_repair()
    assert r.summary["mesh_ready"]
    assert s.centerline is None  # repaired surface invalidates downstream
    assert s.journal[-1]["step"] == "repair"


def test_repair_step_replays_from_workfile(tmp_path):
    from meshforge.pipeline import load_workfile
    from meshforge.synth import generate

    s = MeshingSession()
    s.generate_synth("straight_tube", n_theta=32, n_axial=21)
    s.compute_repair()  # no-op repair, but journaled
    s.compute_centerline(voxel_size=0.5, n_samples=80)
    wf = s.save_workfile(tmp_path / "session")

    fresh = load_workfile(wf)
    results = replay(fresh)
    assert [r.step for r in results] == ["generate_synth", "repair", "centerline"]


def test_session_replay_step_wraps_replay():
    s = MeshingSession()
    s.generate_synth("straight_tube", n_theta=32, n_axial=21)
    s.compute_centerline(voxel_size=0.5, n_samples=80)
    r = s.compute_replay(override="centerline.n_samples=90")
    assert r.summary["n_steps"] == 1  # only the overridden step re-ran
    assert len(s.centerline.points) == 90
    with pytest.raises(ValueError, match="STEP.KNOB=VALUE"):
        s.compute_replay(override="garbage")


def test_session_screenshot_offscreen(tmp_path):
    from meshforge.synth import generate

    s = MeshingSession()
    s.generate_synth("straight_tube", n_theta=32, n_axial=21)
    out = tmp_path / "shot.png"
    try:
        r = s.compute_screenshot(path=str(out))
    except Exception as exc:  # no offscreen GL on this machine
        pytest.skip(f"offscreen rendering unavailable: {exc}")
    assert out.is_file() and out.stat().st_size > 1000
    assert r.summary["shown"] == ["surface"]
    with pytest.raises(RuntimeError, match="nothing to render"):
        MeshingSession().compute_screenshot(path=str(tmp_path / "x.png"))


def test_session_recipe_step(tmp_path):
    yaml = pytest.importorskip("yaml")
    doc = {
        "name": "mini",
        "steps": [
            {"step": "synth", "shape": "straight_tube", "n_theta": 32, "n_axial": 21},
            {"step": "centerline", "voxel_size": 0.5, "n_samples": 80},
            {"step": "volmesh", "thickness": 0.5, "n_theta": 12, "n_axial": 10,
             "n_layers": 2},
        ],
        "sweep": {"volmesh.thickness": [0.4, 0.5]},
    }
    p = tmp_path / "mini.yaml"
    p.write_text(yaml.safe_dump(doc))
    import os

    cwd = os.getcwd()
    os.chdir(tmp_path)  # recipe outputs land under tmp, not the repo
    try:
        r = MeshingSession().compute_recipe(path=str(p))
    finally:
        os.chdir(cwd)
    assert r.summary["n_ok"] == 2 and r.summary["n_failed"] == 0

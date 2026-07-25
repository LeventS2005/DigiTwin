"""V29 transient time series: loud commensurability validation and the
CfdResult series fields (ungated), then OpenFOAM-gated series runs (skip
cleanly without it, trap #16): the startup Hagen-Poiseuille benchmark
(monotonic rise to the exact steady profile), replay identity, and the
CLI --series e2e."""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from meshforge.solve.foam_case import CfdBC, CfdSpec, write_case
from meshforge.solve.foam_runner import CfdResult, find_foam

# ---------------------------------------------------------------------------
# fixtures


def _pipe_wall(target_size: float, length: float, n_layers: int):
    from meshforge.meshing import extrude_blocks, ogrid_disk

    theta = np.linspace(0, 2 * np.pi, 129)[:-1]
    circle = np.column_stack([np.cos(theta), np.sin(theta)])  # R = 1, CCW
    bm = ogrid_disk([circle], target_size=target_size)
    return extrude_blocks(bm, thickness=length, n_layers=n_layers)


def _ico_spec(**overrides) -> CfdSpec:
    base = dict(
        solver="icoFoam",
        nu=0.1,
        bcs={
            "front": CfdBC("velocity", (0.0, 0.0, 1.0)),
            "back": CfdBC("pressure", 0.0),
        },
        end_time=1.0,
        delta_t=0.05,
        write_interval=0.25,
    )
    base.update(overrides)
    return CfdSpec(**base)


# ---------------------------------------------------------------------------
# commensurability refusals (ungated) — the probe trap: non-commensurate
# knobs step PAST end_time and the final state is never written


def test_icofoam_end_time_must_be_multiple_of_delta_t():
    with pytest.raises(ValueError, match="integer multiple of delta_t"):
        _ico_spec(end_time=0.5, delta_t=0.03, write_interval=None)
    _ico_spec(end_time=0.51, delta_t=0.03, write_interval=None)  # 17 steps: fine


def test_icofoam_write_interval_must_be_multiple_of_delta_t():
    with pytest.raises(ValueError, match="integer multiple of delta_t"):
        _ico_spec(end_time=0.6, delta_t=0.03, write_interval=0.2)


def test_icofoam_end_time_must_be_multiple_of_write_interval():
    with pytest.raises(ValueError, match="integer multiple of write_interval"):
        _ico_spec(end_time=0.5, delta_t=0.1, write_interval=0.2)


def test_write_interval_must_be_positive():
    with pytest.raises(ValueError, match="positive"):
        _ico_spec(write_interval=-0.25)
    with pytest.raises(ValueError, match="positive"):
        CfdSpec(
            solver="simpleFoam",
            bcs={"back": CfdBC("pressure", 0.0)},
            write_interval=0.0,
        )


def test_simplefoam_write_interval_must_be_whole():
    with pytest.raises(ValueError, match="whole number"):
        CfdSpec(
            solver="simpleFoam",
            bcs={"back": CfdBC("pressure", 0.0)},
            write_interval=10.5,
        )
    CfdSpec(  # whole iteration counts are fine
        solver="simpleFoam",
        bcs={"back": CfdBC("pressure", 0.0)},
        write_interval=50.0,
    )


def test_probe_knobs_pass_validation():
    _ico_spec(end_time=15.0, delta_t=0.02, write_interval=0.5)


# ---------------------------------------------------------------------------
# CfdResult series fields (ungated)


def _result(**overrides) -> CfdResult:
    base = dict(
        case=Path("x"), solver="icoFoam", elapsed=1.234, latest_time=1.0,
        grid=None, n_cells=8, u_max=1.5, p_min=0.0, p_max=2.0,
    )
    base.update(overrides)
    return CfdResult(**base)


def test_result_without_series_is_unchanged():
    r = _result()
    assert "n_frames" not in r.summary()
    assert "times" not in r.summary()
    assert "frames" not in r.to_text()


def test_result_with_series_reports_frames():
    r = _result(times=[0.0, 0.5, 1.0], frames=[object()] * 3)
    s = r.summary()
    assert s["n_frames"] == 3
    assert s["times"] == [0.0, 0.5, 1.0]
    assert "frames      : 3 (t=0 .. 1)" in r.to_text()


def test_series_knob_is_declared():
    from meshforge.pipeline import STEP_KNOBS

    assert "series" in STEP_KNOBS["cfd"]


def test_cli_series_commensurability_is_loud(tmp_path, capsys):
    # Spec validation fires before OpenFOAM (or even a polyMesh) is needed.
    from meshforge.cli import main

    rc = main([
        "cfd", str(tmp_path),
        "--solver", "icoFoam",
        "--inlet", "front=0,0,1", "--outlet", "back=0",
        "--delta-t", "0.03", "--end-time", "0.5",
        "--series",
    ])
    assert rc == 1
    assert "integer multiple" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# OpenFOAM-gated series runs (trap #16: skip cleanly when absent)

needs_foam = pytest.mark.skipif(
    find_foam() is None, reason="OpenFOAM not available"
)


@needs_foam
def test_startup_hagen_poiseuille_series_benchmark():
    """The roadmap truth: startup pipe flow converges MONOTONICALLY to
    the steady Hagen-Poiseuille profile (centerline -> 2*U_mean exactly
    in the limit; probe measured -1.25% at ts=0.15), the written times
    come back sorted and complete, and every frame lives on one mesh."""
    from meshforge.pipeline import MeshingSession

    session = MeshingSession()
    session.volmesh = _pipe_wall(0.15, 10.0, 25)
    result = session.compute_cfd(
        solver="icoFoam",
        nu=0.1,
        inlet="front=0,0,1",
        outlet="back=0",
        end_time=15.0,
        delta_t=0.02,
        write_interval=0.5,
        series=1,
    )

    expected = [0.5 * i for i in range(31)]
    assert result.summary["n_frames"] == 31
    assert result.summary["times"] == pytest.approx(expected)
    assert len(result.render_hints["frames"]) == 31

    frames = session.cfd.frames
    n_points = frames[0].n_points
    u_center = []
    for grid in frames:
        assert grid.n_points == n_points
        assert {"U", "p", "U_mag"} <= set(grid.point_data)
        line = grid.sample_over_line((0, 0, 6.0), (0, 0, 9.5), resolution=50)
        u_center.append(float(np.nanmean(line["U"][:, 2])))

    rises = np.diff(u_center)
    assert (rises > -1e-6).all(), f"not monotonic: {u_center}"
    assert abs(u_center[-1] - 2.0) / 2.0 < 0.05  # probe: -1.25% at ts=0.15
    assert u_center[0] == pytest.approx(1.0)  # uniform seed at t=0


@needs_foam
def test_series_replay_is_identical():
    """Replaying the journaled cfd step reproduces the identical series
    (same times, frame count, and read-back extrema)."""
    from meshforge.pipeline import MeshingSession, replay

    session = MeshingSession()
    session.volmesh = _pipe_wall(0.3, 10.0, 10)
    session.compute_cfd(
        solver="icoFoam", nu=0.1,
        inlet="front=0,0,1", outlet="back=0",
        end_time=1.0, delta_t=0.05, write_interval=0.25,
        series=1,
    )
    first = dict(session.journal[-1]["summary"])

    results = replay(session, overrides={"cfd": {"series": 1}})
    second = dict(session.journal[-1]["summary"])
    assert [r.step for r in results] == ["cfd"]
    first.pop("elapsed_s"), second.pop("elapsed_s")
    assert first == second
    assert second["times"] == pytest.approx([0.0, 0.25, 0.5, 0.75, 1.0])


@needs_foam
def test_series_works_on_existing_complete_case(tmp_path):
    """The series knob is a read-back knob: it applies to a complete
    external case as well (no BC/turbulence knobs involved)."""
    from meshforge.pipeline import MeshingSession

    case = tmp_path / "pipe_ext"
    wall = _pipe_wall(0.3, 10.0, 10)
    write_case(
        wall, case,
        _ico_spec(end_time=0.2, delta_t=0.05, write_interval=0.1),
    )
    session = MeshingSession()
    result = session.compute_cfd(case=str(case), series=1)
    assert result.summary["times"] == pytest.approx([0.0, 0.1, 0.2])


@needs_foam
def test_cli_cfd_series_e2e(tmp_path, capsys):
    from meshforge.cli import main
    from meshforge.export.foam import write_polymesh

    case = tmp_path / "pipe_cli"
    wall = _pipe_wall(0.3, 10.0, 10)
    write_polymesh(
        wall, case, patch_types={"front": "patch", "back": "patch"}
    )
    rc = main([
        "cfd", str(case),
        "--solver", "icoFoam", "--nu", "0.1",
        "--inlet", "front=0,0,1", "--outlet", "back=0",
        "--end-time", "0.2", "--delta-t", "0.05", "--write-interval", "0.1",
        "--series",
        "--outputs", str(tmp_path / "outputs"),
    ])
    out = capsys.readouterr().out
    assert rc == 0
    assert "frames      :" in out

    out_dir = tmp_path / "outputs" / "cfd" / "pipe_cli"
    frame_files = sorted(out_dir.glob("pipe_cli_t*.vtu"))
    assert [p.name for p in frame_files] == [
        "pipe_cli_t0000.vtu", "pipe_cli_t0001.vtu", "pipe_cli_t0002.vtu",
    ]

    import json

    summary = json.loads((out_dir / "pipe_cli_cfd.json").read_text())
    assert summary["times"] == pytest.approx([0.0, 0.1, 0.2])
    assert summary["n_frames"] == 3
    assert [Path(p).name for p in summary["frame_vtus"]] == [
        p.name for p in frame_files
    ]

    # The frames are real readable grids carrying the fields.
    import pyvista as pv

    g = pv.read(frame_files[1])
    assert {"U", "p", "U_mag"} <= set(g.point_data)

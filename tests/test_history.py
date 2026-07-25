"""V10 session history: journal, work files, selective replay."""
from __future__ import annotations

import json

import pytest

from meshforge.cli import main
from meshforge.pipeline import MeshingSession, load_workfile, replay


def _run_pipeline(session: MeshingSession) -> None:
    session.generate_synth("straight_tube", n_theta=32, n_axial=21)
    session.compute_centerline(voxel_size=0.5, n_samples=100)
    session.compute_volmesh(
        backend="swept", thickness=0.5, n_theta=16, n_axial=20, n_layers=2
    )


def test_journal_records_steps_params_summaries():
    s = MeshingSession()
    _run_pipeline(s)
    assert [r["step"] for r in s.journal] == [
        "generate_synth",
        "centerline",
        "volmesh",
    ]
    assert s.journal[0]["params"] == {
        "shape": "straight_tube",
        "n_theta": 32,
        "n_axial": 21,
    }
    assert s.journal[1]["params"]["voxel_size"] == 0.5
    assert s.journal[2]["summary"]["quality"]["n_inverted"] == 0
    # Journal must be JSON-safe end to end.
    json.dumps(s.journal)


def test_workfile_roundtrip_replays_identically(tmp_path):
    s = MeshingSession()
    _run_pipeline(s)
    original = s.journal[-1]["summary"]["quality"]
    wf = s.save_workfile(tmp_path / "session")
    assert wf.suffix == ".mfwork"

    fresh = load_workfile(wf)
    assert fresh.surface is None  # journal only, no state
    results = replay(fresh)
    assert len(results) == 3
    q = fresh.journal[-1]["summary"]["quality"]
    assert q == original  # deterministic pipeline, identical summary


def test_override_reruns_only_downstream(tmp_path):
    s = MeshingSession()
    _run_pipeline(s)
    cl_before = s.centerline
    surface_before = s.surface
    vol_before = s.journal[-1]["summary"]["quality"]["total_volume"]

    results = replay(s, {"volmesh": {"thickness": 0.7}})
    assert len(results) == 1  # only the overridden step re-ran
    assert s.centerline is cl_before  # upstream state reused (identity)
    assert s.surface is surface_before
    vol_after = s.journal[-1]["summary"]["quality"]["total_volume"]
    assert vol_after > vol_before  # thicker wall -> more volume
    assert s.journal[-1]["params"]["thickness"] == 0.7  # journal updated


def test_override_on_centerline_reruns_volmesh_too():
    s = MeshingSession()
    _run_pipeline(s)
    surface_before = s.surface

    results = replay(s, {"centerline": {"n_samples": 120}})
    assert len(results) == 2  # centerline + volmesh, not the source
    assert s.surface is surface_before
    assert s.journal[1]["params"]["n_samples"] == 120
    assert s.journal[2]["summary"]["quality"]["n_inverted"] == 0


def test_unknown_override_step_loud():
    s = MeshingSession()
    _run_pipeline(s)
    with pytest.raises(ValueError, match="unknown step"):
        replay(s, {"teleport": {"x": 1}})


def test_corrupted_workfiles_loud(tmp_path):
    bad = tmp_path / "bad.mfwork"
    bad.write_text("this is not json{")
    with pytest.raises(ValueError, match="not valid JSON"):
        load_workfile(bad)

    bad.write_text(json.dumps({"version": 2, "journal": []}))
    with pytest.raises(ValueError, match="version=1"):
        load_workfile(bad)

    bad.write_text(
        json.dumps({"version": 1, "journal": [{"step": "teleport", "params": {}}]})
    )
    with pytest.raises(ValueError, match="known step"):
        load_workfile(bad)

    with pytest.raises(ValueError, match="empty"):
        MeshingSession().save_workfile(tmp_path / "empty")


def test_cli_replay_with_override(tmp_path, capsys):
    s = MeshingSession()
    _run_pipeline(s)
    s.save("volmesh", tmp_path / "keep" / "mesh.vtu")
    wf = s.save_workfile(tmp_path / "session")

    rc = main(["replay", str(wf), "--set", "volmesh.thickness=0.65",
               "--save-workfile", str(tmp_path / "updated")])
    out = capsys.readouterr().out
    assert rc == 0
    assert "replayed    : 4 step(s)" in out
    updated = json.loads((tmp_path / "updated.mfwork").read_text())
    assert updated["journal"][2]["params"]["thickness"] == 0.65

    rc = main(["replay", str(tmp_path / "missing.mfwork")])
    assert rc == 1
    assert "not found" in capsys.readouterr().err

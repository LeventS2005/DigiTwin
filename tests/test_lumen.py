"""V47: structured lumen (blood-volume) fill — swept O-grid + guarded
smoothing, tet fallback (spec docs/specs/V47_SPEC.md, decision D75)."""
from __future__ import annotations

import math

import numpy as np
import pytest
from conftest import cso_family_dir

from meshforge.meshing.lumen import fill_lumen
from meshforge.pipeline import MeshingSession, load_workfile, replay
from meshforge.synth import generate


def _tube_radius_length(surf, truth):
    b = surf.bounds
    return (b[1] - b[0]) / 2.0, truth.length


# ---------------------------------------------------------------------------
# structured O-grid path
# ---------------------------------------------------------------------------
def test_straight_tube_ogrid_quality_groups_volume():
    surf, truth = generate("straight_tube")
    wm = fill_lumen(surf, method="ogrid", n_theta=16, n_axial=10, smooth=0)
    assert wm.backend == "lumen-ogrid"
    q = wm.quality
    assert q.n_inverted == 0
    # the V26 O-grid layout floor (~0.7071) survives the straight sweep
    assert q.sj_min > 0.65
    assert set(wm.boundary) == {"wall", "inlet", "outlet"}
    n_disk_quads = wm.boundary["inlet"].n_faces
    assert wm.boundary["outlet"].n_faces == n_disk_quads
    # lateral faces: one per rim edge per station interval; the O-grid rim
    # carries 4*n_t edges and n_t tracks n_theta/4 -> n_theta edges.
    assert wm.boundary["wall"].n_faces == 9 * 16
    r, length = _tube_radius_length(surf, truth)
    exact = math.pi * r * r * length
    # a 16-gon ring inscribes the circle: area factor sin(2pi/16)/(2pi/16)
    assert abs(q.total_volume - exact) / exact < 0.06
    assert any(n.startswith("fold number") for n in wm.notes)


def test_curved_tube_guarded_smoothing_never_regresses():
    surf, _ = generate("curved_tube")
    raw = fill_lumen(surf, method="ogrid", n_theta=16, n_axial=12, smooth=0)
    sm = fill_lumen(surf, method="ogrid", n_theta=16, n_axial=12, smooth=4)
    assert raw.quality.n_inverted == 0
    assert sm.quality.n_inverted == 0
    # the do-no-harm guard: global sj_min can never regress
    assert sm.quality.sj_min >= raw.quality.sj_min - 1e-9
    assert any("guarded smoothing" in n for n in sm.notes)
    assert not any("guarded smoothing" in n for n in raw.notes)


def test_torus_closed_centerline_refused():
    surf, _ = generate("torus")
    with pytest.raises(ValueError, match="open centrelines only"):
        fill_lumen(surf, n_theta=16, n_axial=10, smooth=0)


def test_knob_refusals_are_loud():
    surf, _ = generate("straight_tube")
    with pytest.raises(ValueError, match="auto \\| ogrid \\| tet"):
        fill_lumen(surf, method="hex")
    with pytest.raises(ValueError, match="too coarse"):
        fill_lumen(surf, n_theta=7)
    with pytest.raises(ValueError, match="too coarse"):
        fill_lumen(surf, n_axial=1)
    with pytest.raises(ValueError, match="smooth iterations"):
        fill_lumen(surf, smooth=-1)
    with pytest.raises(ValueError, match="mesh_size must be positive"):
        fill_lumen(surf, mesh_size=0.0)


def test_ogrid_count_overrides_validate():
    from meshforge.meshing.ogrid import ogrid_disk

    theta = np.linspace(0, 2 * np.pi, 32, endpoint=False)
    loop = np.column_stack([np.cos(theta), np.sin(theta)])
    with pytest.raises(ValueError, match="n_t override"):
        ogrid_disk([loop], target_size=0.2, n_t=1)
    with pytest.raises(ValueError, match="n_r override"):
        ogrid_disk([loop], target_size=0.2, n_r=0)
    bm = ogrid_disk([loop], target_size=0.2, n_t=5, n_r=3)
    # pinned counts land in the layout note verbatim
    assert "n_t 5" in bm.notes[0] and "n_r 3" in bm.notes[0]


# ---------------------------------------------------------------------------
# tet fallback path (gmsh-gated)
# ---------------------------------------------------------------------------
def test_tet_method_direct():
    pytest.importorskip("gmsh")
    surf, truth = generate("straight_tube")
    wm = fill_lumen(surf, method="tet", n_theta=16, n_axial=10)
    assert wm.backend == "lumen-tet"
    assert wm.quality.n_inverted == 0
    assert set(wm.boundary) == {"wall", "inlet", "outlet"}
    r, length = _tube_radius_length(surf, truth)
    exact = math.pi * r * r * length
    assert abs(wm.quality.total_volume - exact) / exact < 0.06


def test_auto_falls_back_to_tet_with_loud_note(monkeypatch):
    pytest.importorskip("gmsh")
    import meshforge.meshing.lumen as lumen_mod

    def boom(*a, **k):
        raise ValueError("synthetic structured failure")

    monkeypatch.setattr(lumen_mod, "_station_disks", boom)
    surf, _ = generate("straight_tube")
    wm = fill_lumen(surf, method="auto", n_theta=16, n_axial=10, smooth=0)
    assert wm.backend == "lumen-tet"
    fallback = [n for n in wm.notes if "structured O-grid fill failed" in n]
    assert fallback and "fold number" in fallback[0]
    # a forced ogrid keeps the failure LOUD instead of falling back
    with pytest.raises(ValueError, match="synthetic structured failure"):
        fill_lumen(surf, method="ogrid", n_theta=16, n_axial=10, smooth=0)


# ---------------------------------------------------------------------------
# session step / workfile / CLI
# ---------------------------------------------------------------------------
def test_session_step_records_and_replays(tmp_path):
    s = MeshingSession()
    s.generate_synth("straight_tube")
    r = s.compute_lumen(method="ogrid", n_theta=16, n_axial=10, smooth=0)
    assert r.step == "lumen"
    assert s.volmesh is not None and s.volmesh.backend == "lumen-ogrid"
    assert s.journal[-1]["step"] == "lumen"
    assert s.journal[-1]["params"]["method"] == "ogrid"
    wf = s.save_workfile(tmp_path / "lumen.mfwork")
    s2 = load_workfile(wf)
    replay(s2)
    assert s2.volmesh.backend == "lumen-ogrid"
    assert s2.volmesh.summary()["quality"]["n_inverted"] == 0


def test_cli_lumen_end_to_end(tmp_path, capsys):
    from meshforge import cli

    surf, _ = generate("straight_tube")
    src = tmp_path / "tube.vtp"
    surf.save(src)
    rc = cli.main(
        [
            "lumen", str(src),
            "--n-theta", "16", "--n-axial", "10", "--smooth", "0",
            "--outputs", str(tmp_path),
        ]
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "lumen-ogrid" in out and "inverted=0" in out
    assert "fold number" in out
    assert (tmp_path / "tube_lumen.vtu").is_file()
    assert (tmp_path / "tube_lumen.summary.json").is_file()


def test_cli_lumen_bad_method_exits_nonzero(tmp_path, capsys):
    from meshforge import cli

    surf, _ = generate("straight_tube")
    src = tmp_path / "tube.vtp"
    surf.save(src)
    rc = cli.main(["lumen", str(src), "--method", "hex", "--outputs", str(tmp_path)])
    assert rc == 1
    assert "auto | ogrid | tet" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# the real cardiac family (present on this machine only; rule-1 checksummed)
# ---------------------------------------------------------------------------
_FAMILY_DIR = cso_family_dir()  # machine-local, resolved by conftest; may be None
_FAMILY = sorted(_FAMILY_DIR.glob("*.cso")) if _FAMILY_DIR is not None else []


@pytest.mark.skipif(not _FAMILY, reason="real .cso family folder not present")
@pytest.mark.parametrize("cso_path", _FAMILY, ids=lambda p: p.stem.split("_")[0])
def test_real_family_lumen_inverted_zero(cso_path):
    pytest.importorskip("gmsh")
    from meshforge.io.cso import cso_input_sha256

    before = cso_input_sha256(cso_path)
    s = MeshingSession()
    s.compute_import_cso(path=str(cso_path))  # auto surface + centreline
    r = s.compute_lumen()  # product defaults: auto, smooth on
    q = r.summary["quality"]
    assert q["n_inverted"] == 0
    backend = r.summary["backend"]
    name = cso_path.stem.split("_")[0]
    if name == "VAM997":
        # probe-pinned: the one section too far from a disk -> honest tets,
        # with the fold-number note naming why (D75)
        assert backend == "lumen-tet"
        assert any("structured O-grid fill failed" in n for n in r.summary["notes"])
    else:
        assert backend == "lumen-ogrid"
    assert any(n.startswith("fold number") for n in r.summary["notes"])
    assert cso_input_sha256(cso_path) == before  # rule 1: unchanged

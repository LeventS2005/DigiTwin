"""V26 O-grid (butterfly) disk layout: area/volume truths, the lifted
quality floor vs the projected disk (pins D37's limit), exact conformity,
loud refusals, and session/CLI integration."""
from __future__ import annotations

import json

import numpy as np
import pytest

from digitwin.cli import main
from digitwin.io.cad import _signed_area
from digitwin.meshing import (
    extrude_blocks,
    ogrid_disk,
    project_profile,
    quad_scaled_jacobians,
)


def _circle(n: int = 128, r: float = 1.0) -> np.ndarray:
    t = np.linspace(0.0, 2.0 * np.pi, n, endpoint=False)
    return np.column_stack([r * np.cos(t), r * np.sin(t)])


# A square with a narrow off-center slit cut toward the interior: vertex
# (1, 0.3) winds BACKWARD about the centroid -> not star-shaped.
NONSTAR = np.array(
    [[0, 0], [2, 0], [2, 0.2], [1.0, 0.3], [2, 0.4], [2, 2], [0, 2]],
    dtype=float,
)


def _mesh_area(bm) -> float:
    p = bm.points[bm.quads]
    a1 = (p[:, 1, 0] - p[:, 0, 0]) * (p[:, 2, 1] - p[:, 0, 1]) - (
        p[:, 2, 0] - p[:, 0, 0]
    ) * (p[:, 1, 1] - p[:, 0, 1])
    a2 = (p[:, 2, 0] - p[:, 0, 0]) * (p[:, 3, 1] - p[:, 0, 1]) - (
        p[:, 3, 0] - p[:, 0, 0]
    ) * (p[:, 2, 1] - p[:, 0, 1])
    return float((a1 + a2).sum() / 2.0)


# ---------------------------------------------------------------------------
# truths


def test_disk_area_converges_and_extruded_volume_exact():
    loop = _circle()
    poly_area = _signed_area(loop)  # the 128-gon truth the rim lies on
    bm_1 = ogrid_disk([loop], target_size=0.1)
    area_1 = _mesh_area(bm_1)
    assert abs(area_1 - np.pi) / np.pi <= 0.005  # measured -0.161%

    bm_2 = ogrid_disk([loop], target_size=0.05)
    area_2 = _mesh_area(bm_2)
    # refinement converges toward the polygon area (-0.040% vs pi)
    assert abs(area_2 - np.pi) < abs(area_1 - np.pi)
    assert abs(area_2 - poly_area) / poly_area <= 1e-3

    # extrusion is exact: hex volume == quad area x length
    wall = extrude_blocks(bm_1, thickness=10.0, n_layers=5)
    assert wall.quality.n_inverted == 0
    assert wall.quality.total_volume == pytest.approx(area_1 * 10.0, rel=1e-9)


def test_quality_floor_lifted_above_projected_disk():
    loop = _circle()
    # O-grid sj_min is set by the layout (135 deg spoke/core corner):
    # measured 0.7071 at every target size.
    for ts in (0.15, 0.1, 0.05):
        sj = quad_scaled_jacobians(*_pts_quads(ogrid_disk([loop], target_size=ts)))
        assert float(sj.min()) >= 0.70

    # ... while the projected disk baseline decays with refinement
    proj = project_profile([loop], target_size=0.15)
    proj_sj = float(quad_scaled_jacobians(proj.points, proj.quads).min())
    ogrid = ogrid_disk([loop], target_size=0.15)
    ogrid_sj = float(quad_scaled_jacobians(ogrid.points, ogrid.quads).min())
    assert ogrid_sj > 5.0 * proj_sj  # measured 0.7071 vs 0.0852

    # ... and REFUSES at ts=0.1 (D37's documented limit) where the
    # O-grid sails through.
    with pytest.raises(ValueError, match="below floor"):
        project_profile([loop], target_size=0.1)


def _pts_quads(bm):
    return bm.points, bm.quads


def test_conformity_exact_and_rim_on_curve():
    loop = _circle()
    bm = ogrid_disk([loop], target_size=0.1)

    counts: dict[tuple[int, int], int] = {}
    for quad in bm.quads:
        for e in range(4):
            a, b = int(quad[e]), int(quad[(e + 1) % 4])
            counts[(min(a, b), max(a, b))] = counts.get((min(a, b), max(a, b)), 0) + 1
    boundary = {k for k, c in counts.items() if c == 1}
    assert not any(c > 2 for c in counts.values())
    # every boundary edge is an outer_rim edge, and vice versa
    rim_edges = {
        (min(int(a), int(b)), max(int(a), int(b)))
        for a, b in bm.edge_groups["outer_rim"]
    }
    assert boundary == rim_edges
    assert len(bm.edge_groups["hole_rims"]) == 0
    assert bm.n_blocks == 5
    assert set(np.unique(bm.block_of)) == {0, 1, 2, 3, 4}

    # no duplicate points anywhere (shared edges are shared IDS)
    assert len(np.unique(np.round(bm.points, 12), axis=0)) == len(bm.points)

    # rim nodes lie exactly on the input polygon (within chord sagitta)
    rim_nodes = np.unique(bm.edge_groups["outer_rim"])
    r = np.linalg.norm(bm.points[rim_nodes], axis=1)
    sagitta = 1.0 * (1.0 - np.cos(np.pi / 128))
    assert np.all(r <= 1.0 + 1e-9)
    assert np.all(r >= 1.0 - sagitta - 1e-9)


def test_extruded_boundary_groups():
    bm = ogrid_disk([_circle(64)], target_size=0.2)
    n_quads = len(bm.quads)
    n_rim = len(bm.edge_groups["outer_rim"])
    wall = extrude_blocks(bm, thickness=1.0, n_layers=3)
    assert set(wall.boundary) == {"front", "back", "outer_rim", "hole_rims"}
    assert len(wall.boundary["front"].face_nodes) == n_quads
    assert len(wall.boundary["back"].face_nodes) == n_quads
    assert len(wall.boundary["outer_rim"].face_nodes) == n_rim * 3
    assert len(wall.boundary["hole_rims"].face_nodes) == 0


# ---------------------------------------------------------------------------
# loud refusals


def test_refusals_are_loud():
    circle = _circle(64)
    with pytest.raises(ValueError, match="exactly one loop"):
        ogrid_disk([circle, circle[::-1] * 0.3], target_size=0.2)
    with pytest.raises(ValueError, match="CCW"):
        ogrid_disk([circle[::-1].copy()], target_size=0.2)
    with pytest.raises(ValueError, match="star-shaped"):
        ogrid_disk([NONSTAR], target_size=0.2)
    with pytest.raises(ValueError, match="core_fraction"):
        ogrid_disk([circle], target_size=0.2, core_fraction=1.0)
    with pytest.raises(ValueError, match="core_fraction"):
        ogrid_disk([circle], target_size=0.2, core_fraction=0.0)
    with pytest.raises(ValueError, match="target_size"):
        ogrid_disk([circle], target_size=0.0)
    with pytest.raises(ValueError, match="at least 4"):
        ogrid_disk([np.array([[0, 0], [1, 0], [0, 1]], float)], target_size=0.2)


def test_smoothing_refuses_ogrid_meshes():
    from digitwin.meshing.smooth import smooth_lattice

    bm = ogrid_disk([_circle(64)], target_size=0.2)
    with pytest.raises(ValueError, match="no global rectilinear lattice"):
        smooth_lattice(bm)


# ---------------------------------------------------------------------------
# session + CLI integration


def _circle_profile():
    from digitwin.io.cad import Profile2D

    return Profile2D(
        loops=[_circle(64)],
        components=[{"outer": 0, "holes": []}],
        units="mm",
        provenance={},
        mesh_ready=True,
    )


def test_session_ogrid_knobs():
    from digitwin.pipeline import MeshingSession

    s = MeshingSession()
    s.profile = _circle_profile()
    r = s.compute_blocks(target_size=0.15, thickness=5.0, n_layers=4, ogrid=1)
    assert r.summary["quality"]["n_inverted"] == 0
    assert r.summary["quality"]["sj_min"] >= 0.70
    assert s.journal[-1]["params"]["ogrid"] == 1
    assert s.journal[-1]["params"]["core_fraction"] == 0.5
    assert any("O-grid" in n for n in s.volmesh.notes)

    with pytest.raises(ValueError, match="mutually exclusive"):
        s.compute_blocks(target_size=0.15, ogrid=1, project=1)
    with pytest.raises(ValueError, match="lattice meshes only"):
        s.compute_blocks(target_size=0.15, ogrid=1, grade_ratio=2.0)


def test_cli_blocks_ogrid(tmp_path, capsys):
    profile = {
        "loops": [_circle(64).tolist()],
        "components": [{"outer": 0, "holes": []}],
        "units": "mm",
        "mesh_ready": True,
        "provenance": {},
    }
    src = tmp_path / "circle_profile.json"
    src.write_text(json.dumps(profile))
    out_dir = tmp_path / "outputs"

    rc = main(
        [
            "blocks", str(src), "--ogrid",
            "--target-size", "0.15", "--thickness", "5", "--n-layers", "4",
            "--outputs", str(out_dir),
        ]
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "inverted=0" in out
    assert "O-grid" in out
    assert (out_dir / "circle_profile_blocks.vtu").is_file()

    rc = main(["blocks", str(src), "--ogrid", "--project", "--target-size", "0.15"])
    assert rc == 1
    assert "mutually exclusive" in capsys.readouterr().err

    rc = main(["blocks", str(src), "--ogrid", "--grade-ratio", "2", "--target-size", "0.15"])
    assert rc == 1
    assert "lattice meshes only" in capsys.readouterr().err

    # a holed profile is refused loudly under --ogrid
    square = [[-3, -3], [3, -3], [3, 3], [-3, 3]]
    holed = {
        "loops": [square, _circle(32)[::-1].tolist()],
        "components": [{"outer": 0, "holes": [1]}],
        "units": "mm",
        "mesh_ready": True,
        "provenance": {},
    }
    src2 = tmp_path / "holed_profile.json"
    src2.write_text(json.dumps(holed))
    rc = main(["blocks", str(src2), "--ogrid", "--target-size", "0.5"])
    assert rc == 1
    assert "exactly one loop" in capsys.readouterr().err

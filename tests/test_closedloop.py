"""V9 closed-loop swept walls: holonomy-corrected frames, periodic lattice.

Wall volume truth (exact, both surfaces are tori, Pappus):
V = 2·pi^2·R·(r+t)^2 − 2·pi^2·R·r^2 = 2·pi^2·R·t·(2r + t).
"""
from __future__ import annotations

import math

import numpy as np
import pytest

from digitwin.geometry import compute_centerline, parallel_transport_frames
from digitwin.geometry.frames import _transport
from digitwin.meshing import get_backend
from digitwin.synth import generate


def _loop_with_holonomy(n: int = 256) -> tuple[np.ndarray, np.ndarray]:
    """Closed non-planar loop with measured PT holonomy ~ -0.67 rad."""
    u = np.linspace(0.0, 2.0 * np.pi, n, endpoint=False)
    pts = np.column_stack(
        [
            np.cos(u) * (6.0 + 1.5 * np.cos(2 * u)),
            np.sin(u) * (6.0 + 1.0 * np.sin(3 * u)),
            2.0 * np.sin(2 * u),
        ]
    )
    d = np.gradient(pts, axis=0)
    return pts, d / np.linalg.norm(d, axis=1)[:, None]


def _seam_angle(n0, n_last, t_last, t0) -> float:
    ret = _transport(n_last, t_last, t0)
    return float(np.arctan2(np.dot(np.cross(n0, ret), t0), np.dot(n0, ret)))


def test_holonomy_measured_then_cancelled() -> None:
    _, t = _loop_with_holonomy()
    n_open, _ = parallel_transport_frames(t)
    phi = _seam_angle(n_open[0], n_open[-1], t[-1], t[0])
    assert abs(phi) > 0.5  # the loop genuinely twists

    n_cl, b_cl = parallel_transport_frames(t, closed=True)
    res = _seam_angle(n_cl[0], n_cl[-1], t[-1], t[0])
    assert abs(res) < 1e-12  # first/last station frames coincide across seam
    # Frames stay orthonormal with n x b = t everywhere.
    np.testing.assert_allclose(np.linalg.norm(n_cl, axis=1), 1.0, atol=1e-9)
    np.testing.assert_allclose(
        np.einsum("ij,ij->i", n_cl, t), 0.0, atol=1e-9
    )
    np.testing.assert_allclose(np.cross(n_cl, b_cl), t, atol=1e-7)


def test_torus_swept_wall_end_to_end() -> None:
    surface, truth = generate("torus")  # r = 1.5, R = 6
    cl = compute_centerline(surface)
    assert cl.closed

    n_theta, n_axial, n_layers, t_wall = 24, 48, 3, 0.4
    wall = get_backend("swept").mesh_wall(
        surface, t_wall, centerline=cl, n_theta=n_theta, n_axial=n_axial, n_layers=n_layers
    )
    q = wall.quality
    assert q.n_inverted == 0
    assert q.sj_min > 0.9  # measured 0.9626 — the seam does not pinch

    # Exact periodic tiling: no end faces, every quad accounted for.
    assert set(wall.boundary) == {"inner", "outer"}  # no inlet/outlet
    assert wall.grid.n_cells == n_axial * n_theta * n_layers
    assert wall.boundary["inner"].n_faces == n_axial * n_theta
    assert wall.boundary["outer"].n_faces == n_axial * n_theta
    total = sum(g.n_faces for g in wall.boundary.values())
    assert total == wall.grid.extract_surface(algorithm="dataset_surface").n_cells

    # Exact annular truth: V = 2 pi^2 R t (2r + t); voxel/EDT + chordal
    # discretization measured at -1.82% -> 4% band.
    analytic = 2.0 * math.pi**2 * 6.0 * t_wall * (2 * 1.5 + t_wall)
    assert q.total_volume == pytest.approx(analytic, rel=0.04)
    assert any("periodic" in n or "closed-loop" in n for n in wall.notes)


def test_open_tube_still_has_four_groups() -> None:
    surface, _ = generate("straight_tube", n_theta=32, n_axial=21)
    cl = compute_centerline(surface)
    wall = get_backend("swept").mesh_wall(
        surface, 0.5, centerline=cl, n_theta=12, n_axial=10, n_layers=2
    )
    assert set(wall.boundary) == {"inner", "outer", "inlet", "outlet"}
    assert wall.quality.n_inverted == 0


def test_torus_tet_backend_closed_shell() -> None:
    pytest.importorskip("gmsh")
    surface, _ = generate("torus")
    cl = compute_centerline(surface)
    wall = get_backend("tet").mesh_wall(
        surface, 0.4, centerline=cl, n_theta=20, n_axial=40, mesh_size=0.6
    )
    assert wall.quality.n_inverted == 0
    assert set(wall.boundary) == {"inner", "outer"}  # empty end groups dropped
    analytic = 2.0 * math.pi**2 * 6.0 * 0.4 * (2 * 1.5 + 0.4)
    assert wall.quality.total_volume == pytest.approx(analytic, rel=0.05)

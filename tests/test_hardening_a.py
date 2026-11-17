from __future__ import annotations

import math

import numpy as np
import pytest
import pyvista as pv

from digitwin.geometry import compute_centerline, offset_wall
from digitwin.geometry.sdf import sdf_offset_surface
from digitwin.meshing import build_wall_lattice, get_backend
from digitwin.synth import generate

# A genuinely folding case: r + t = 5 > bend radius 4.5, so an unguarded
# swept wall must invert on the concave side (trap #2: do NOT soften this).
TIGHT = dict(radius=2.0, bend_radius=4.5, angle=math.pi)
T_FAT = 3.0


def test_sdf_sphere_offset_matches_closed_form() -> None:
    r, t = 5.0, 0.9
    sphere = pv.Sphere(radius=r, theta_resolution=80, phi_resolution=80)
    result = offset_wall(sphere, t)  # sdf is the default mode now

    exact = 4.0 / 3.0 * math.pi * (r + t) ** 3
    assert result.outer.n_open_edges == 0
    assert result.outer.volume == pytest.approx(exact, rel=0.025)
    # Realized thickness reported per inner vertex.
    assert np.mean(result.thickness) == pytest.approx(t, rel=0.05)
    assert result.n_capped == 0 and result.n_floored == 0
    assert any("sdf offset" in n for n in result.notes)


def test_sdf_survives_the_tight_bend_watertight() -> None:
    surface, _ = generate("curved_tube", **TIGHT)
    outer, _notes = sdf_offset_surface(surface, T_FAT)
    # Level sets cannot self-intersect: watertight, manifold, volume grows.
    assert outer.n_open_edges == 0
    assert outer.is_manifold
    assert outer.volume > surface.volume


def test_sdf_spacing_guard() -> None:
    surface, _ = generate("straight_tube")
    with pytest.raises(ValueError, match="voxels across the wall"):
        sdf_offset_surface(surface, 0.6, spacing=0.5)


def test_graded_layers_geometric_progression() -> None:
    surface, _ = generate("straight_tube")
    cl = compute_centerline(surface)
    ratio, n_layers, t = 2.0, 4, 0.8
    lattice = build_wall_lattice(
        surface, t, centerline=cl, n_theta=16, n_axial=10,
        n_layers=n_layers, layer_ratio=ratio,
    )
    # Radial node distances along one ray.
    mid = lattice.nodes[5, 3]  # (L+1, 3)
    center_dists = np.linalg.norm(mid - mid[0], axis=1)
    steps = np.diff(center_dists)
    np.testing.assert_allclose(steps[1:] / steps[:-1], ratio, rtol=1e-9)
    assert center_dists[-1] == pytest.approx(t, rel=1e-9)


def test_curvature_cap_prevents_inversion_on_tight_bend() -> None:
    surface, _ = generate("curved_tube", **TIGHT)
    cl = compute_centerline(surface)
    swept = get_backend("swept")

    guarded = swept.mesh_wall(surface, T_FAT, centerline=cl, n_theta=24, n_axial=40)
    assert guarded.quality.n_inverted == 0
    assert any("curvature cap" in n for n in guarded.notes)
    assert any("floor raised" in n for n in guarded.notes)

    # The unguarded control MUST fold — proving the guard is load-bearing.
    unguarded = swept.mesh_wall(
        surface, T_FAT, centerline=cl, n_theta=24, n_axial=40, curvature_cap=1e9
    )
    assert unguarded.quality.n_inverted > 0


def test_lattice_guard_error_branches() -> None:
    surface, _ = generate("straight_tube")
    cl = compute_centerline(surface)
    with pytest.raises(ValueError, match="layer_ratio"):
        build_wall_lattice(surface, 0.5, centerline=cl, layer_ratio=0.0)
    with pytest.raises(ValueError, match="floor_frac"):
        build_wall_lattice(surface, 0.5, centerline=cl, floor_frac=0.0)


def test_session_wall_default_is_sdf() -> None:
    from digitwin.pipeline import STEP_KNOBS, MeshingSession

    assert STEP_KNOBS["wall"]["mode"][1] == "sdf"
    s = MeshingSession()
    s.generate_synth("straight_tube")
    result = s.compute_wall(thickness=0.5)
    assert any("sdf offset" in n for n in result.summary["notes"])

from __future__ import annotations

import math

import numpy as np
import pytest
import pyvista as pv

from digitwin.geometry import compute_centerline, offset_wall
from digitwin.synth import generate


def test_sphere_offset_is_exact_per_vertex() -> None:
    r, t = 5.0, 0.5
    sphere = pv.Sphere(radius=r, theta_resolution=60, phi_resolution=60)
    result = offset_wall(sphere, t, mode="uniform")

    # Topology preserved -> per-vertex offset distance is directly checkable.
    d = np.linalg.norm(result.outer.points - sphere.points, axis=1)
    np.testing.assert_allclose(d, t, rtol=1e-6)
    assert result.n_capped == 0  # convex everywhere
    exact = 4.0 / 3.0 * math.pi * (r + t) ** 3
    assert result.outer.volume == pytest.approx(exact, rel=0.01)


def test_straight_tube_offset_volume_band() -> None:
    surface, truth = generate("straight_tube")
    r, length, t = truth.radii[0], truth.length, 0.6
    result = offset_wall(surface, t, mode="uniform")

    assert result.outer.n_open_edges == 0
    # Edge-vertex normals chamfer the cap corners, so bracket the volume:
    lo = math.pi * (r + t) ** 2 * length * 0.98
    hi = math.pi * (r + t) ** 2 * (length + 2 * t) * 1.02
    assert lo < result.outer.volume < hi
    assert np.all(result.thickness > 0)


def test_radius_scaled_mode_tracks_local_radius() -> None:
    surface, truth = generate("tapered_tube")  # r: 3.0 -> 1.5
    cl = compute_centerline(surface)
    result = offset_wall(surface, 0.4, mode="radius_scaled", centerline=cl)

    z = surface.points[:, 2]
    interior = (z > 3.0) & (z < 17.0)
    t_start = result.thickness[interior & (z < 8.0)].mean()
    t_end = result.thickness[interior & (z > 12.0)].mean()
    # Thickness ratio should track the radius ratio (r_end/r_start = 0.5).
    assert t_end / t_start == pytest.approx(0.62, abs=0.12)
    assert any("radius_scaled" in n for n in result.notes)


def test_curvature_cap_fires_on_tight_bend() -> None:
    # Deliberately harsh: thick wall on a tight bend (trap #3: the capped
    # branch must actually run in the suite).
    surface, _ = generate("curved_tube", radius=2.0, bend_radius=4.5, angle=math.pi)
    result = offset_wall(surface, 1.5, mode="uniform", curvature_cap=0.5, floor_frac=0.2)

    assert result.n_capped > 0
    assert any("curvature cap" in n for n in result.notes)
    # The floor guarantees no zero slab even where the cap bites (trap #9).
    assert result.thickness.min() >= 0.2 * 1.5 - 1e-12
    assert np.all(np.isfinite(result.thickness))
    assert result.outer.n_open_edges == 0
    # Notes are strings even with per-vertex arrays in play (trap #1).
    for note in result.notes:
        assert isinstance(note, str)


def test_wall_error_branches() -> None:
    surface, _ = generate("straight_tube")
    with pytest.raises(ValueError, match="thickness must be positive"):
        offset_wall(surface, -1.0)
    with pytest.raises(ValueError, match="unknown wall mode"):
        offset_wall(surface, 0.5, mode="banana")
    with pytest.raises(ValueError, match="requires a centerline"):
        offset_wall(surface, 0.5, mode="radius_scaled")
    with pytest.raises(ValueError, match="watertight"):
        offset_wall(pv.Plane().triangulate(), 0.5)


def test_backend_registry() -> None:
    from digitwin.geometry import get_backend

    backend = get_backend("native")
    assert backend.name == "native"
    with pytest.raises(ValueError, match="unknown geometry backend"):
        get_backend("magic")

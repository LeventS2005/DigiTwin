from __future__ import annotations

import numpy as np
import pytest
from scipy.spatial import cKDTree

from digitwin.geometry import compute_centerline
from digitwin.synth import generate

def _assert_coverage(cl, truth) -> None:
    """With cap extension (V4, default ON) the centerline reaches within
    standoff + ray/voxel slack of flat caps: length in
    [L − 2·(0.02·r_max + 1.5·voxel), 1.005·L]."""
    r_max = float(truth.radii.max())
    assert cl.length >= truth.length - 2.0 * (0.02 * r_max + 1.5 * cl.voxel_size)
    assert cl.length <= 1.005 * truth.length


def _assert_coverage_unextended(cl, truth) -> None:
    """Old behavior (extend_to_caps=False): the medial axis stops ~one radius
    short of each flat cap; bound is length >= L − 2.2·r_max, <= 1.02·L."""
    r_max = float(truth.radii.max())
    assert cl.length >= truth.length - 2.2 * r_max
    assert cl.length <= 1.02 * truth.length
    # ... and it genuinely stops short (this is what V4 fixed).
    assert cl.length <= truth.length - 1.0 * r_max


def _deviation_from_truth(cl, truth) -> np.ndarray:
    tree = cKDTree(truth.points)
    d, _ = tree.query(cl.points)
    return d


def _covered_arc(cl, truth) -> float:
    """Analytic arc length spanned between the projections of the computed
    endpoints onto the truth curve — the honest length reference for a
    centerline that cannot reach the caps."""
    tree = cKDTree(truth.points)
    _, i0 = tree.query(cl.points[0])
    _, i1 = tree.query(cl.points[-1])
    return abs(float(truth.arc_lengths[i1] - truth.arc_lengths[i0]))


def test_straight_tube_centerline() -> None:
    surface, truth = generate("straight_tube")
    cl = compute_centerline(surface)

    assert not cl.closed
    _assert_coverage(cl, truth)
    # Every computed point sits on the z-axis within ~1.5 voxels.
    axis_dist = np.linalg.norm(cl.points[:, :2], axis=1)
    assert axis_dist.max() < 1.5 * cl.voxel_size
    # Middle-half tangents are ±z.
    mid = slice(len(cl.points) // 4, 3 * len(cl.points) // 4)
    assert np.abs(cl.tangents[mid, 2]).min() > 0.999
    # EDT radius in the middle half matches the tube radius within a voxel.
    r = truth.radii[0]
    assert abs(np.mean(cl.radii[mid]) - r) < max(0.07 * r, 1.5 * cl.voxel_size)
    # Uniform arc-length resample.
    steps = np.diff(cl.arc_lengths)
    np.testing.assert_allclose(steps, steps[0], rtol=1e-9)


def test_curved_tube_centerline_follows_arc() -> None:
    surface, truth = generate("curved_tube")
    cl = compute_centerline(surface)

    _assert_coverage(cl, truth)
    dev = _deviation_from_truth(cl, truth)
    # End regions: cap extensions are straight chords along the end tangents,
    # deviating ~ext²/(2·bend_radius) ≈ 0.12 from the true arc; plus skeleton
    # wander near caps.
    assert dev.max() < 3.0 * cl.voxel_size
    mid = slice(len(cl.points) // 4, 3 * len(cl.points) // 4)
    assert dev[mid].max() < 1.5 * cl.voxel_size


def test_extension_off_keeps_old_behavior() -> None:
    surface, truth = generate("straight_tube")
    cl = compute_centerline(surface, extend_to_caps=False)
    _assert_coverage_unextended(cl, truth)
    assert not any("cap extension" in n for n in cl.notes)


def test_torus_centerline_is_closed_loop() -> None:
    surface, truth = generate("torus")
    cl = compute_centerline(surface)

    assert cl.closed
    assert abs(cl.length - truth.length) / truth.length < 0.05
    dev = _deviation_from_truth(cl, truth)
    assert dev.max() < 2.0 * cl.voxel_size


def test_constricted_tube_radius_profile_dips() -> None:
    surface, truth = generate("constricted_tube", depth=0.4)
    cl = compute_centerline(surface)

    i_min = int(np.argmin(cl.radii))
    s_min = cl.arc_lengths[i_min]
    # The dip must land within one sigma of mid-length (offset by whatever
    # cap coverage was lost at the start).
    lost = (truth.length - cl.length) / 2.0
    assert abs((s_min + lost) - truth.length / 2.0) < 2.0
    r0 = truth.radii[0]
    assert cl.radii[i_min] < 0.75 * r0  # 0.6·r0 truth + voxel tolerance


def test_smoothing_kills_staircase_without_eating_length() -> None:
    # Trap #8: the voxel staircase INFLATES the raw path length; smoothing
    # must bring the length back to the covered analytic arc, not below it,
    # while improving positional accuracy.
    surface, truth = generate("curved_tube")
    rough = compute_centerline(surface, smoothing=1e-9)
    smooth = compute_centerline(surface)  # default: 1 voxel RMS

    covered = _covered_arc(smooth, truth)
    assert abs(smooth.length - covered) / covered < 0.05
    # The unsmoothed staircase is a strictly worse length estimate.
    assert abs(rough.length - _covered_arc(rough, truth)) > abs(smooth.length - covered)
    # Positional accuracy improves (or at worst matches).
    assert (
        _deviation_from_truth(smooth, truth).mean()
        <= _deviation_from_truth(rough, truth).mean() + 1e-6
    )


def test_open_surface_rejected() -> None:
    import pyvista as pv

    plane = pv.Plane().triangulate()
    with pytest.raises(ValueError, match="open edges"):
        compute_centerline(plane)


def test_absurd_grid_rejected() -> None:
    surface, _ = generate("straight_tube")
    with pytest.raises(ValueError, match="exceeds"):
        compute_centerline(surface, voxel_size=0.001)

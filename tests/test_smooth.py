"""V17 elliptic (Winslow) smoothing: identity on uniform lattices, recovery
of perturbed interiors, fixed boundaries + exact area on projected curves,
loud guards, CLI knob."""
from __future__ import annotations

import json
from dataclasses import replace

import numpy as np
import pytest

from digitwin.cli import main
from digitwin.meshing import (
    build_block_mesh,
    extrude_blocks,
    project_profile,
    quad_scaled_jacobians,
)
from digitwin.meshing.smooth import smooth_lattice

SQUARE = np.array([[-10, -10], [10, -10], [10, 10], [-10, 10]], dtype=float)
RECT = np.array([[0, 0], [10, 0], [10, 8], [0, 8]], dtype=float)
HOLE = np.array([[3, 3], [3, 6], [7, 6], [7, 3]], dtype=float)  # CW


def _circle_hole(r: float, n: int) -> np.ndarray:
    t = np.linspace(0.0, 2.0 * np.pi, n, endpoint=False)
    return np.column_stack([r * np.cos(t), r * np.sin(t)])[::-1].copy()  # CW


def _area(bm) -> float:
    p = bm.points[bm.quads]
    return float(
        np.sum(
            0.5
            * np.abs(
                p[:, 0, 0] * (p[:, 1, 1] - p[:, 3, 1])
                + p[:, 1, 0] * (p[:, 2, 1] - p[:, 0, 1])
                + p[:, 2, 0] * (p[:, 3, 1] - p[:, 1, 1])
                + p[:, 3, 0] * (p[:, 0, 1] - p[:, 2, 1])
            )
        )
    )


def _smoothable_mask(bm) -> np.ndarray:
    """Node-id mask of nodes the smoother is allowed to move."""
    present = bm.node_id >= 0
    ok = np.zeros_like(present)
    ok[1:-1, 1:-1] = (
        present[1:-1, 1:-1]
        & present[:-2, 1:-1] & present[2:, 1:-1]
        & present[1:-1, :-2] & present[1:-1, 2:]
        & present[:-2, :-2] & present[2:, :-2]
        & present[:-2, 2:] & present[2:, 2:]
    )
    mask = np.zeros(len(bm.points), dtype=bool)
    mask[bm.node_id[ok]] = True
    return mask


def test_uniform_lattice_is_identity():
    bm = build_block_mesh([RECT], target_size=1.0)  # exact 1x1 spacing
    sm = smooth_lattice(bm)
    assert float(np.abs(sm.points - bm.points).max()) < 1e-12
    assert any("converged in 1 iteration" in n for n in sm.notes)


def test_perturbed_interior_recovers_tensor_grid():
    bm = build_block_mesh([RECT], target_size=1.0)
    movable = _smoothable_mask(bm)
    rng = np.random.default_rng(17)
    bad = np.array(bm.points)
    bad[movable] += rng.uniform(-0.3, 0.3, size=(int(movable.sum()), 2))
    perturbed = replace(bm, points=bad)

    sj_bad = quad_scaled_jacobians(perturbed.points, perturbed.quads).min()
    sm = smooth_lattice(perturbed)
    sj_new = quad_scaled_jacobians(sm.points, sm.quads).min()
    assert sj_new > 0.98 > sj_bad
    # Harmonic solution on a straight uniform boundary IS the tensor grid.
    assert float(np.abs(sm.points - bm.points).max()) < 0.02
    assert any("converged" in n for n in sm.notes)
    # Fixed nodes are byte-identical.
    np.testing.assert_array_equal(sm.points[~movable], bm.points[~movable])


def test_projected_circle_boundary_fixed_area_exact():
    bm = project_profile([SQUARE, _circle_hole(5.0, 64)], target_size=1.25)
    sm = smooth_lattice(bm)
    movable = _smoothable_mask(bm)
    np.testing.assert_array_equal(sm.points[~movable], bm.points[~movable])
    for name in ("outer_rim", "hole_rims"):
        rim_nodes = np.unique(bm.edge_groups[name])
        np.testing.assert_array_equal(sm.points[rim_nodes], bm.points[rim_nodes])
    assert _area(sm) == pytest.approx(_area(bm), rel=1e-9)
    # V21 default: the quality guard removes the V17-D1 trade — sj_min may
    # never regress below the input.
    sj_before = quad_scaled_jacobians(bm.points, bm.quads).min()
    sj_after = quad_scaled_jacobians(sm.points, sm.quads).min()
    assert sj_after >= sj_before - 1e-9
    assert any("quality guard" in n for n in sm.notes)
    wall = extrude_blocks(sm, thickness=2.0, n_layers=2)
    assert wall.quality.n_inverted == 0


def test_guard_off_reproduces_v17_trade():
    """V17-D1 stays documented: plain Winslow (guard OFF) lowers peak sj
    beside the snapped rim, 0.8349 -> ~0.68 on the circle fixture."""
    bm = project_profile([SQUARE, _circle_hole(5.0, 64)], target_size=1.25)
    sm = smooth_lattice(bm, guard_quality=False)
    assert any("sj_min 0.83" in n and "-> 0.68" in n for n in sm.notes)


def test_guard_full_revert_with_zero_passes():
    """guard_passes=0 makes smoothing all-or-nothing: the circle case
    regresses under plain Winslow, so the guard reverts it entirely."""
    bm = project_profile([SQUARE, _circle_hole(5.0, 64)], target_size=1.25)
    sm = smooth_lattice(bm, guard_passes=0)
    np.testing.assert_array_equal(sm.points, bm.points)
    assert any("fully REVERTED" in n for n in sm.notes)


def test_guard_partial_blend_can_improve_projected_mesh():
    """At this resolution the guard blends 68 nodes instead of reverting —
    and guarded smoothing IMPROVES the projected mesh (0.8365 -> 0.8432
    measured), which plain Winslow could not do without regressing."""
    bm = project_profile([SQUARE, _circle_hole(5.0, 64)], target_size=1.6)
    sm = smooth_lattice(bm)
    assert any("blended back toward their input positions" in n for n in sm.notes)
    sj_before = quad_scaled_jacobians(bm.points, bm.quads).min()
    sj_after = quad_scaled_jacobians(sm.points, sm.quads).min()
    assert sj_after >= sj_before - 1e-9
    assert sj_after > 0.84 > sj_before  # measured improvement
    wall = extrude_blocks(sm, thickness=2.0, n_layers=1)
    assert wall.quality.n_inverted == 0


def test_guard_knob_validation():
    bm = build_block_mesh([RECT], target_size=1.0)
    with pytest.raises(ValueError, match="guard_passes"):
        smooth_lattice(bm, guard_passes=-1)


def test_multiblock_with_hole_smooths_and_extrudes():
    bm = build_block_mesh([RECT, HOLE], target_size=0.9)  # non-uniform ticks
    sm = smooth_lattice(bm)
    rim_nodes = np.unique(
        np.concatenate([bm.edge_groups["outer_rim"], bm.edge_groups["hole_rims"]])
    )
    np.testing.assert_array_equal(sm.points[rim_nodes], bm.points[rim_nodes])
    assert _area(sm) == pytest.approx(_area(bm), rel=1e-9)
    wall = extrude_blocks(sm, thickness=1.0, n_layers=1)
    assert wall.quality.n_inverted == 0


def test_knob_validation():
    bm = build_block_mesh([RECT], target_size=1.0)
    with pytest.raises(ValueError, match="max_iter"):
        smooth_lattice(bm, max_iter=0)
    with pytest.raises(ValueError, match="tol"):
        smooth_lattice(bm, tol=0.0)
    with pytest.raises(ValueError, match="omega"):
        smooth_lattice(bm, omega=1.5)


def test_nonconvergence_is_a_loud_note():
    bm = build_block_mesh([RECT], target_size=1.0)
    movable = _smoothable_mask(bm)
    bad = np.array(bm.points)
    bad[movable] += np.random.default_rng(3).uniform(-0.3, 0.3, (movable.sum(), 2))
    sm = smooth_lattice(replace(bm, points=bad), max_iter=1)
    assert any("did NOT converge" in n for n in sm.notes)


def test_folded_boundary_quad_is_refused():
    bm = build_block_mesh([RECT], target_size=1.0)
    bad = np.array(bm.points)
    # Fold a corner quad by dragging a boundary corner deep inside.
    corner = int(bm.node_id[0, 0])
    bad[corner] = [2.5, 2.5]
    with pytest.raises(ValueError, match="folded"):
        # max_iter=1: one damped step cannot unfold a boundary-driven fold,
        # so the guard fires deterministically.
        smooth_lattice(replace(bm, points=bad), max_iter=1)


def test_cli_blocks_smooth_flag(tmp_path, capsys):
    profile = {
        "loops": [SQUARE.tolist(), _circle_hole(5.0, 64).tolist()],
        "components": [{"outer": 0, "holes": [1]}],
        "units": "mm",
        "mesh_ready": True,
        "provenance": {},
    }
    src = tmp_path / "round_profile.json"
    src.write_text(json.dumps(profile))
    rc = main([
        "blocks", str(src), "--project", "--smooth",
        "--target-size", "1.25", "--thickness", "2",
        "--outputs", str(tmp_path / "outputs"),
    ])
    out = capsys.readouterr().out
    assert rc == 0
    assert "inverted=0" in out
    assert "elliptic smoothing converged" in out

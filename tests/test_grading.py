"""V20 transition grading: exact ratios, conformity/area invariants,
fine-cells-at-features placement, projection pass-through, loud branches,
smooth-interaction warning, CLI."""
from __future__ import annotations

import json

import numpy as np
import pytest

from meshforge.cli import main
from meshforge.meshing import build_block_mesh, extrude_blocks, project_profile
from meshforge.meshing.blocks import _graded_ticks

RECT = np.array([[0, 0], [10, 0], [10, 8], [0, 8]], dtype=float)
HOLE = np.array([[3, 3], [3, 6], [7, 6], [7, 3]], dtype=float)  # CW
SQUARE = np.array([[-10, -10], [10, -10], [10, 10], [-10, 10]], dtype=float)


def _circle_hole(r: float, n: int) -> np.ndarray:
    t = np.linspace(0.0, 2.0 * np.pi, n, endpoint=False)
    return np.column_stack([r * np.cos(t), r * np.sin(t)])[::-1].copy()


def test_graded_ticks_exact_properties():
    t = _graded_ticks(0.0, 10.0, 8, 3.0)
    assert t[0] == 0.0 and t[-1] == 10.0 and len(t) == 9
    s = np.diff(t)
    assert (s > 0).all()
    assert s.max() / s.min() == pytest.approx(3.0)
    np.testing.assert_allclose(s, s[::-1])  # symmetric
    assert s.argmin() in (0, len(s) - 1)  # fine at the ends

    # ratio 1 and n < 3 degrade to uniform exactly.
    np.testing.assert_array_equal(_graded_ticks(0, 1, 5, 1.0), np.linspace(0, 1, 6))
    np.testing.assert_array_equal(_graded_ticks(0, 1, 2, 4.0), np.linspace(0, 1, 3))
    # Odd cell count: the single middle cell carries the max spacing.
    s5 = np.diff(_graded_ticks(0.0, 1.0, 5, 2.0))
    assert s5.argmax() == 2 and s5.max() / s5.min() == pytest.approx(2.0)


def test_ratio_one_is_byte_identical():
    a = build_block_mesh([RECT, HOLE], target_size=0.9)
    b = build_block_mesh([RECT, HOLE], target_size=0.9, grade_ratio=1.0)
    np.testing.assert_array_equal(a.points, b.points)
    np.testing.assert_array_equal(a.quads, b.quads)


def test_graded_rect_invariants():
    plain = build_block_mesh([RECT], target_size=1.0)
    graded = build_block_mesh([RECT], target_size=1.0, grade_ratio=3.0)
    # Counts identical — grading redistributes, never re-counts.
    assert len(graded.points) == len(plain.points)
    assert len(graded.quads) == len(plain.quads)
    assert any("transition grading" in n for n in graded.notes)

    wall_p = extrude_blocks(plain, thickness=1.0, n_layers=1)
    wall_g = extrude_blocks(graded, thickness=1.0, n_layers=1)
    assert wall_g.quality.n_inverted == 0
    # Rectilinear grading keeps rectangles: sj exactly 1, volume exact.
    assert wall_g.quality.sj_min == pytest.approx(1.0)
    assert wall_g.quality.total_volume == pytest.approx(
        wall_p.quality.total_volume, rel=1e-12
    )


def test_fine_cells_sit_at_the_hole_rim():
    bm = build_block_mesh([RECT, HOLE], target_size=0.5, grade_ratio=4.0)
    xs = np.unique(bm.points[:, 0])
    s = np.diff(xs)
    # x = 3 and x = 7 are hole edges (coarse grid lines): the spacing right
    # next to them must be the interval minimum on both sides.
    for feature in (3.0, 7.0):
        k = int(np.argmin(np.abs(xs - feature)))
        assert xs[k] == pytest.approx(feature, abs=1e-9)
        assert s[k - 1] < s[k - 2]  # spacing shrinks approaching the feature
        assert s[k] < s[k + 1]      # and grows again moving away from it
    wall = extrude_blocks(bm, thickness=1.0, n_layers=1)
    assert wall.quality.n_inverted == 0


def test_projection_pass_through():
    bm = project_profile(
        [SQUARE, _circle_hole(5.0, 64)], target_size=1.25, grade_ratio=2.0
    )
    assert any("transition grading" in n for n in bm.notes)
    wall = extrude_blocks(bm, thickness=2.0, n_layers=1)
    assert wall.quality.n_inverted == 0


def test_bad_ratio_is_loud():
    with pytest.raises(ValueError, match="grade_ratio"):
        build_block_mesh([RECT], target_size=1.0, grade_ratio=0.5)


def test_cli_grading_and_smooth_warning(tmp_path, capsys):
    profile = {
        "loops": [RECT.tolist(), HOLE.tolist()],
        "components": [{"outer": 0, "holes": [1]}],
        "units": "mm",
        "mesh_ready": True,
        "provenance": {},
    }
    src = tmp_path / "rect_profile.json"
    src.write_text(json.dumps(profile))

    rc = main([
        "blocks", str(src), "--target-size", "0.9", "--grade-ratio", "3",
        "--thickness", "1", "--outputs", str(tmp_path / "o1"),
    ])
    out = capsys.readouterr().out
    assert rc == 0
    assert "transition grading: max/min spacing 3" in out
    assert "inverted=0" in out and "sj_min=1" in out

    rc = main([
        "blocks", str(src), "--target-size", "0.9", "--grade-ratio", "3",
        "--smooth", "--thickness", "1", "--outputs", str(tmp_path / "o2"),
    ])
    out = capsys.readouterr().out
    assert rc == 0
    assert "WARNING: Winslow smoothing equidistributes" in out

    rc = main([
        "blocks", str(src), "--target-size", "0.9", "--grade-ratio", "0.2",
        "--thickness", "1", "--outputs", str(tmp_path / "o3"),
    ])
    assert rc == 1
    assert "grade_ratio" in capsys.readouterr().err

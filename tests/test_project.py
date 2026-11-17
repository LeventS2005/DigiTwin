"""V6 projection method: rim snapping, corner capture, TFI re-blend."""
from __future__ import annotations

import json

import numpy as np
import pytest

from digitwin.cli import main
from digitwin.io.cad import _signed_area
from digitwin.meshing import (
    build_block_mesh,
    extrude_blocks,
    project_profile,
    quad_scaled_jacobians,
)

SQUARE = np.array([[-10, -10], [10, -10], [10, 10], [-10, 10]], dtype=float)


def _circle_hole(r: float, n: int) -> np.ndarray:
    t = np.linspace(0.0, 2.0 * np.pi, n, endpoint=False)
    return np.column_stack([r * np.cos(t), r * np.sin(t)])[::-1].copy()  # CW


def _mesh_area(bm) -> float:
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


def test_circle_in_square_rim_on_circle_and_quality() -> None:
    hole = _circle_hole(5.0, 64)
    bm = project_profile([SQUARE, hole], target_size=1.25)

    # Every hole-rim node lies on the circle polygon (within loop chord).
    rim_nodes = np.unique(bm.edge_groups["hole_rims"])
    r = np.linalg.norm(bm.points[rim_nodes], axis=1)
    chord_sagitta = 5.0 * (1 - np.cos(np.pi / 64))
    assert np.all(r <= 5.0 + 1e-9)
    assert np.all(r >= 5.0 - chord_sagitta - 1e-9)

    sj = quad_scaled_jacobians(bm.points, bm.quads)
    # Measured 0.835 at this resolution; floor with headroom.
    assert float(sj.min()) > 0.7
    assert any("projection" in n for n in bm.notes)


def test_circle_area_converges_first_order() -> None:
    target = 400.0 - abs(_signed_area(_circle_hole(5.0, 64)))
    errs = []
    for size in (2.5, 1.25, 0.625):
        bm = project_profile([SQUARE, _circle_hole(5.0, 64)], target_size=size)
        errs.append(abs(_mesh_area(bm) - target) / target)
    # Measured: 0.670% -> 0.177% -> 0.049%.
    assert errs[0] > errs[1] > errs[2]
    assert errs[2] < 0.001


def test_diamond_corners_captured_exactly() -> None:
    diamond = np.array([[0, -6], [6, 0], [0, 6], [-6, 0]], dtype=float)[::-1].copy()
    bm = project_profile([SQUARE, diamond], target_size=1.25)
    for corner in ([0, -6], [6, 0], [0, 6], [-6, 0]):
        d = np.linalg.norm(bm.points - np.asarray(corner, dtype=float), axis=1).min()
        assert d == 0.0  # snapped EXACTLY, not merely close

    wall = extrude_blocks(bm, thickness=2.0, n_layers=2)
    assert wall.quality.n_inverted == 0
    assert wall.quality.sj_min > 0.5  # measured 0.6897


def test_rectilinear_profile_roundtrips_unchanged() -> None:
    rect = np.array([[0, 0], [10, 0], [10, 8], [0, 8]], dtype=float)
    hole = np.array([[3, 3], [3, 6], [7, 6], [7, 3]], dtype=float)
    a = build_block_mesh([rect, hole], target_size=1.0)
    b = project_profile([rect, hole], target_size=1.0)
    assert np.allclose(a.points, b.points)
    assert np.array_equal(a.quads, b.quads)
    assert not any("projection" in n for n in b.notes)


def test_quality_gate_raises_loudly() -> None:
    hole = _circle_hole(5.0, 64)
    with pytest.raises(ValueError, match="scaled Jacobian"):
        project_profile([SQUARE, hole], target_size=1.25, min_sj=0.99)


def test_orientation_errors() -> None:
    with pytest.raises(ValueError, match="CCW"):
        project_profile([SQUARE[::-1].copy()], target_size=1.0)
    with pytest.raises(ValueError, match="CW"):
        project_profile([SQUARE, _circle_hole(3.0, 32)[::-1].copy()], target_size=1.0)


def test_cli_blocks_project(tmp_path, capsys) -> None:
    hole = _circle_hole(5.0, 64)
    profile = {
        "loops": [SQUARE.tolist(), hole.tolist()],
        "components": [{"outer": 0, "holes": [1]}],
        "units": "mm",
        "mesh_ready": True,
        "provenance": {},
    }
    src = tmp_path / "round_profile.json"
    src.write_text(json.dumps(profile))
    out_dir = tmp_path / "outputs"

    # Without --project the curved hole must be refused pointing to V6 ...
    rc = main(["blocks", str(src), "--target-size", "1.25", "--outputs", str(out_dir)])
    assert rc == 1
    assert "V6 projection" in capsys.readouterr().err

    # ... and with --project it meshes cleanly.
    rc = main(
        [
            "blocks", str(src), "--project",
            "--target-size", "1.25", "--thickness", "2",
            "--outputs", str(out_dir),
        ]
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "inverted=0" in out
    assert "projection" in out

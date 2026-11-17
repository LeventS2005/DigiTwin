"""V5 multi-block: rectilinear block decomposition, TFI quads, hex extrusion."""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import numpy as np
import pytest

from digitwin.cli import main
from digitwin.meshing import build_block_mesh, extrude_blocks

RECT = np.array([[0, 0], [10, 0], [10, 8], [0, 8]], dtype=float)
HOLE = np.array([[3, 3], [3, 6], [7, 6], [7, 3]], dtype=float)  # CW


def _cottage_dwg() -> Path | None:
    env = os.environ.get("MESHFORGE_COTTAGE_DWG")
    if env and Path(env).is_file():
        return Path(env)
    cand = Path(__file__).resolve().parents[1] / "Cottage_Pane_Window.dwg"
    return cand if cand.is_file() else None


needs_cottage = pytest.mark.skipif(
    shutil.which("dwg2dxf") is None or _cottage_dwg() is None,
    reason="dwg2dxf converter or Cottage_Pane_Window.dwg not available",
)


def test_plain_rect_exact_counts() -> None:
    bm = build_block_mesh([RECT], target_size=1.0)
    assert bm.n_blocks == 1
    assert len(bm.quads) == 10 * 8
    assert len(bm.points) == 11 * 9
    assert len(bm.edge_groups["outer_rim"]) == 2 * (10 + 8)
    assert len(bm.edge_groups["hole_rims"]) == 0

    wall = extrude_blocks(bm, thickness=2.0, n_layers=2)
    assert wall.grid.n_cells == 160
    assert wall.grid.n_points == 99 * 3
    assert wall.quality.n_inverted == 0
    assert wall.quality.sj_min == pytest.approx(1.0, abs=1e-9)  # perfect bricks
    assert wall.quality.total_volume == pytest.approx(10 * 8 * 2.0, rel=1e-12)
    faces = {k: g.n_faces for k, g in wall.boundary.items()}
    assert faces == {"front": 80, "back": 80, "outer_rim": 72, "hole_rims": 0}


def test_rect_with_hole_exact_counts_and_shared_edges() -> None:
    bm = build_block_mesh([RECT, HOLE], target_size=1.0)
    # Coarse grid 3x3, center block is the hole -> 8 active blocks.
    assert bm.n_blocks == 8
    assert len(bm.quads) == 80 - 12
    assert len(bm.points) == 99 - 6  # interior hole nodes dropped
    # Exact index-arithmetic merge: no duplicated coordinates anywhere.
    assert len(np.unique(bm.points.round(9), axis=0)) == len(bm.points)
    assert len(bm.edge_groups["hole_rims"]) == 2 * (4 + 3)

    wall = extrude_blocks(bm, thickness=2.0, n_layers=2)
    assert wall.quality.n_inverted == 0
    assert wall.quality.total_volume == pytest.approx(68 * 2.0, rel=1e-12)
    faces = {k: g.n_faces for k, g in wall.boundary.items()}
    assert faces == {"front": 68, "back": 68, "outer_rim": 72, "hole_rims": 28}


def test_target_size_refines_globally() -> None:
    bm = build_block_mesh([RECT, HOLE], target_size=0.5)
    assert len(bm.quads) == 20 * 16 - 8 * 6
    # Neighbouring blocks share subdivision counts per grid interval,
    # so every quad is exactly 0.5 x 0.5.
    d = bm.points[bm.quads[:, 2]] - bm.points[bm.quads[:, 0]]
    np.testing.assert_allclose(np.abs(d), 0.5, rtol=1e-12)


def test_l_shape_rectilinear_polygon() -> None:
    l_shape = np.array(
        [[0, 0], [10, 0], [10, 5], [6, 5], [6, 8], [0, 8]], dtype=float
    )
    bm = build_block_mesh([l_shape], target_size=1.0)
    assert bm.n_blocks == 3  # 2x2 coarse grid minus the notch
    assert len(bm.quads) == 80 - 4 * 3
    wall = extrude_blocks(bm, thickness=1.0)
    assert wall.quality.n_inverted == 0
    assert wall.quality.total_volume == pytest.approx(68.0, rel=1e-12)


def test_non_rectilinear_refused_pointing_to_v6() -> None:
    rot = np.array([[0, 0], [7, 3], [4, 10], [-3, 7]], dtype=float)  # rotated square
    with pytest.raises(ValueError, match="V6 projection"):
        build_block_mesh([rot], target_size=1.0)
    t = np.linspace(0, 2 * np.pi, 24, endpoint=False)
    circle = np.column_stack([np.cos(t), np.sin(t)])
    with pytest.raises(ValueError, match="V6 projection"):
        build_block_mesh([circle], target_size=0.2)


def test_orientation_and_degenerate_errors() -> None:
    with pytest.raises(ValueError, match="CCW"):
        build_block_mesh([RECT[::-1].copy()], target_size=1.0)
    with pytest.raises(ValueError, match="CW"):
        build_block_mesh([RECT, HOLE[::-1].copy()], target_size=1.0)
    with pytest.raises(ValueError, match="target_size"):
        build_block_mesh([RECT], target_size=0.0)
    with pytest.raises(ValueError, match="thickness"):
        extrude_blocks(build_block_mesh([RECT], target_size=2.0), thickness=-1.0)


def test_cli_blocks_roundtrip(tmp_path, capsys) -> None:
    # Profile JSON in import-cad format: rect with one hole component.
    profile = {
        "loops": [RECT.tolist(), HOLE.tolist()],
        "components": [{"outer": 0, "holes": [1]}],
        "units": "mm",
        "mesh_ready": True,
        "provenance": {},
    }
    src = tmp_path / "part_profile.json"
    src.write_text(json.dumps(profile))
    out_dir = tmp_path / "outputs"

    rc = main(
        [
            "blocks", str(src),
            "--target-size", "1", "--thickness", "2", "--n-layers", "2",
            "--outputs", str(out_dir),
        ]
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "inverted=0" in out
    assert (out_dir / "part_profile_blocks.vtu").is_file()
    summary = json.loads((out_dir / "part_profile_blocks.summary.json").read_text())
    assert summary["quality"]["total_volume"] == pytest.approx(136.0)

    rc = main(["blocks", str(src), "--outer", "5", "--outputs", str(out_dir)])
    assert rc == 1
    assert "out of range" in capsys.readouterr().err


@needs_cottage
def test_cottage_window_multiblock_end_to_end(tmp_path) -> None:
    """The flagship V5 validation: sash material of 'Cottage Pane 5 x 4' =
    outer frame rectangle minus the 20 pane rectangles, hex-meshed by
    rectilinear block decomposition. Loop edges all lie on grid lines, so
    the mesh volume equals (A_outer - 20*A_pane) x thickness EXACTLY."""
    from digitwin.io import file_sha256
    from digitwin.io.cad import _signed_area, load_profile

    dwg = _cottage_dwg()
    before = file_sha256(dwg)
    prof = load_profile(dwg)
    assert file_sha256(dwg) == before

    ring_outer = prof.loops[prof.components[0]["outer"]]
    panes = []
    for comp in prof.components:
        lp = prof.loops[comp["outer"]]
        lo, hi = lp.min(axis=0), lp.max(axis=0)
        if abs((hi[0] - lo[0]) - 177.03) < 0.1 and abs((hi[1] - lo[1]) - 170.68) < 0.1:
            panes.append(lp[::-1].copy())  # CCW face outer -> CW hole
    assert len(panes) == 20

    bm = build_block_mesh([ring_outer] + panes, target_size=40.0)
    wall = extrude_blocks(bm, thickness=40.0, n_layers=2)

    assert wall.quality.n_inverted == 0
    a_outer = _signed_area(ring_outer)
    a_panes = sum(abs(_signed_area(p)) for p in panes)
    expected = (a_outer - a_panes) * 40.0
    assert wall.quality.total_volume == pytest.approx(expected, rel=1e-9)
    # 20 pane cavities: hole rim faces exist and tile both layers.
    assert wall.boundary["hole_rims"].n_faces > 0
    assert wall.boundary["hole_rims"].n_faces % 2 == 0
    assert wall.boundary["front"].n_faces == len(bm.quads)

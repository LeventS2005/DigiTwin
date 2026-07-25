"""V8 surface repair: mechanical fixes, loud non-fixes, degrade round-trip."""
from __future__ import annotations

import numpy as np
import pytest
import pyvista as pv

from meshforge.cli import main
from meshforge.quality import check_surface, degrade_surface, repair_surface
from meshforge.synth import generate


def _tube():
    surface, _ = generate("straight_tube", n_theta=32, n_axial=21)
    return surface


def test_duplicate_vertices_merged_watertight():
    # A tetrahedron built with each face carrying its OWN copies of the
    # shared vertices: watertight geometry, torn topology.
    corners = np.array(
        [[0, 0, 0], [1, 0, 0], [0, 1, 0], [0, 0, 1]], dtype=float
    )
    tris_c = [(0, 2, 1), (0, 1, 3), (1, 2, 3), (0, 3, 2)]
    pts, tris, k = [], [], 0
    for tri in tris_c:
        pts.extend(corners[list(tri)])
        tris.append([k, k + 1, k + 2])
        k += 3
    torn = pv.PolyData(
        np.asarray(pts),
        np.column_stack([np.full(4, 3), np.asarray(tris)]).ravel(),
    )
    assert torn.n_open_edges > 0

    repaired, report = repair_surface(torn)
    assert report.merged_vertices == 8  # 12 copies -> 4 vertices
    assert repaired.n_points == 4
    assert repaired.n_open_edges == 0
    assert report.mesh_ready


def test_degenerate_and_duplicate_faces_removed():
    surface = _tube()
    n = surface.n_cells
    tris = surface.faces.reshape(-1, 4)[:, 1:]
    # Append: 2 exact duplicates + 1 repeated-vertex sliver.
    extra = np.vstack([tris[0], tris[10][::-1], [tris[5][0], tris[5][0], tris[5][1]]])
    faces = np.column_stack(
        [np.full(n + 3, 3), np.vstack([tris, extra])]
    ).ravel()
    dirty = pv.PolyData(surface.points, faces)

    repaired, report = repair_surface(dirty)
    assert report.degenerate_faces_removed == 1
    assert report.duplicate_faces_removed == 2
    assert repaired.n_cells == n
    assert report.mesh_ready


def test_small_holes_filled_big_holes_reported():
    surface = _tube()
    holed = degrade_surface(surface, poke_holes=3, seed=1)
    assert holed.n_open_edges > 0

    repaired, report = repair_surface(holed)
    assert report.holes_filled == 3
    assert report.holes_left == 0
    assert repaired.n_open_edges == 0
    assert report.mesh_ready

    # A 3-edge hole must be REFUSED when max_hole_edges is below it.
    _, tight = repair_surface(holed, max_hole_edges=2)
    assert tight.holes_filled == 0
    assert tight.holes_left == 3
    assert not tight.mesh_ready
    assert any("left open" in n for n in tight.notes)


def test_non_manifold_reported_never_fixed(nonmanifold_vtp):
    surface = pv.read(nonmanifold_vtp)
    repaired, report = repair_surface(surface)
    assert report.non_manifold_edges > 0
    assert not report.mesh_ready
    assert any("NOT auto-fixed" in n for n in report.notes)
    # Faces are preserved: repair does not delete its way out.
    assert repaired.n_cells == surface.n_cells


def test_degraded_tube_full_pipeline_regression():
    """The flagship V8 test: degrade -> repair -> mesh_ready -> volmesh
    with 0 inverted cells."""
    surface, _ = generate("straight_tube")
    degraded = degrade_surface(
        surface, noise=0.01, poke_holes=3, duplicate_faces=4, seed=7
    )
    repaired, report = repair_surface(degraded)
    assert report.mesh_ready
    assert report.holes_filled == 3
    assert report.duplicate_faces_removed >= 4  # noise keeps dups byte-equal

    check = check_surface(repaired, path="degraded", sha256="-")
    assert check.mesh_ready

    from meshforge.geometry import compute_centerline
    from meshforge.meshing import get_backend

    cl = compute_centerline(repaired)
    wall = get_backend("swept").mesh_wall(
        repaired, 0.5, centerline=cl, n_theta=16, n_axial=20, n_layers=2
    )
    assert wall.quality.n_inverted == 0


def test_degrade_validation():
    surface = _tube()
    with pytest.raises(ValueError, match="poke"):
        degrade_surface(surface, poke_holes=10**6)


def test_cli_repair_roundtrip(tmp_path, capsys):
    surface, _ = generate("straight_tube", n_theta=32, n_axial=21)
    degraded = degrade_surface(surface, poke_holes=2, duplicate_faces=3, seed=3)
    src = tmp_path / "damaged.vtp"
    degraded.save(src)
    out_dir = tmp_path / "outputs"

    rc = main(["repair", str(src), "--outputs", str(out_dir)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "holes       : 2 filled, 0 left" in out
    assert (out_dir / "damaged_repaired.vtp").is_file()
    assert (out_dir / "damaged_repair.json").is_file()

    # Non-mesh-ready result exits 1 (holes too big to fill).
    rc = main(
        ["repair", str(src), "--max-hole-edges", "2", "--outputs", str(out_dir)]
    )
    assert rc == 1

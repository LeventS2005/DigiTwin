"""V11 branching trees: y_junction truth, tree extraction, tet wall."""
from __future__ import annotations

import math

import numpy as np
import pytest

from meshforge.cli import main
from meshforge.geometry import compute_sections, extract_tree, mesh_tree_wall
from meshforge.synth import generate, generate_tree


@pytest.fixture(scope="module")
def y_case():
    surface, truth = generate_tree("y_junction")
    return surface, truth


@pytest.fixture(scope="module")
def y_tree(y_case):
    surface, _ = y_case
    return extract_tree(surface)


def _match(branch, tree, truth):
    tip_dir = branch.tip - tree.junction
    tip_dir = tip_dir / np.linalg.norm(tip_dir)
    best = max(truth.branches, key=lambda tb: float(np.dot(tb.direction, tip_dir)))
    return best, float(np.dot(best.direction, tip_dir))


def test_y_junction_surface_and_volume_truth(y_case):
    surface, truth = y_case
    assert surface.n_open_edges == 0
    assert surface.is_manifold
    # Outward orientation must hold SIGNED (pv .volume hides a flip).
    p = np.asarray(surface.points)
    tri = surface.faces.reshape(-1, 4)[:, 1:]
    signed = float(
        np.einsum("ij,ij->i", p[tri[:, 0]], np.cross(p[tri[:, 1]], p[tri[:, 2]])).sum() / 6
    )
    assert signed > 0
    assert truth.n_branches == 3
    assert surface.volume == pytest.approx(truth.volume, rel=0.02)  # measured -0.78%


def test_y_junction_degenerate_params_loud():
    with pytest.raises(ValueError, match="angle"):
        generate_tree("y_junction", angle=2.0)
    with pytest.raises(ValueError, match="positive"):
        generate_tree("y_junction", child_radius=-1.0)
    with pytest.raises(ValueError, match="unknown tree"):
        generate_tree("nope")


def test_tree_extraction_branches_and_junction(y_case, y_tree):
    _, truth = y_case
    tree = y_tree
    assert len(tree.branches) == 3
    # The medial branch point sits slightly downstream of the geometric
    # origin (parent is wider); measured 0.40.
    assert float(np.linalg.norm(tree.junction - truth.junction)) < 0.8

    matched = set()
    for b in tree.branches:
        best, dot = _match(b, tree, truth)
        matched.add(best.name)
        assert dot > 0.995  # axis direction recovered
        # Flat tips + junction ball: skeleton covers 75-100% of each axis
        # (measured parent 7.11/8.0, children ~5.7/7.0).
        assert 0.75 * best.length <= b.length <= best.length + 2 * tree.voxel_size
        assert float(np.median(b.radii)) == pytest.approx(best.radius, rel=0.05)
    assert matched == {"parent", "child_a", "child_b"}


def test_sections_per_branch(y_case, y_tree):
    surface, truth = y_case
    parent = max(
        y_tree.branches, key=lambda b: _match(b, y_tree, truth)[0].length
    )
    n = len(parent.points)
    secs = compute_sections(surface, parent, stations=list(range(n // 3, 2 * n // 3, 5)))
    areas = np.array([s.area for s in secs])
    r = _match(parent, y_tree, truth)[0].radius
    assert np.abs(areas - math.pi * r**2).max() / (math.pi * r**2) < 0.1


def test_unbranched_tube_refused():
    surface, _ = generate("straight_tube", n_theta=32, n_axial=21)
    with pytest.raises(ValueError, match="no junction"):
        extract_tree(surface)


def test_swept_refused_at_junctions(y_case):
    surface, _ = y_case
    with pytest.raises(ValueError, match="refused at junctions"):
        mesh_tree_wall(surface, thickness=0.4, backend="swept")
    with pytest.raises(ValueError, match="thickness"):
        mesh_tree_wall(surface, thickness=-1.0)


def test_tree_tet_wall(y_case):
    pytest.importorskip("gmsh")
    surface, _ = y_case
    from meshforge.geometry.sdf import sdf_offset_surface

    wall = mesh_tree_wall(surface, thickness=0.4, mesh_size=0.6)
    q = wall.quality
    assert q.n_inverted == 0  # nan-aware (trap #13)
    assert q.sj_min > 0  # slivers exist on marching-cubes shells, none inverted
    assert set(wall.boundary) == {"inner", "outer"}
    # Volume parity with the shell pair (both watertight meshes).
    outer, _notes = sdf_offset_surface(surface, 0.4)
    expected = outer.volume - surface.volume
    assert q.total_volume == pytest.approx(expected, rel=1e-3)


def test_cli_synth_y_junction(tmp_path, capsys):
    out = tmp_path / "outputs"
    rc = main(["synth", "y_junction", "--set", "child_length=6", "--outputs", str(out)])
    text = capsys.readouterr().out
    assert rc == 0
    assert "branches    : 3" in text
    assert (out / "y_junction.vtp").is_file()
    assert (out / "y_junction_truth.json").is_file()

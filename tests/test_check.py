from __future__ import annotations

from pathlib import Path

import pyvista as pv

from digitwin.io import file_sha256, load_surface
from digitwin.quality import check_surface


def _check_for(path: Path):
    return check_surface(load_surface(path), path=str(path), sha256=file_sha256(path))


def test_sphere_is_mesh_ready(sphere_stl: Path) -> None:
    report = _check_for(sphere_stl)
    assert report.n_open_edges == 0
    assert report.n_non_manifold_edges == 0
    assert report.all_triangles
    assert report.watertight
    assert report.mesh_ready
    assert report.notes == []


def test_open_surface_not_ready(open_plane_ply: Path) -> None:
    report = _check_for(open_plane_ply)
    assert report.n_open_edges > 0
    assert not report.watertight
    assert not report.mesh_ready
    assert any("open edges" in n for n in report.notes)


def test_nonmanifold_not_ready(nonmanifold_vtp: Path) -> None:
    report = _check_for(nonmanifold_vtp)
    assert report.n_non_manifold_edges > 0
    assert not report.watertight
    assert not report.mesh_ready
    assert any("non-manifold" in n for n in report.notes)


def test_quads_not_ready(tmp_path: Path) -> None:
    quads = pv.Plane(i_resolution=3, j_resolution=3)  # quad cells, not triangles
    path = tmp_path / "quads.vtp"
    quads.save(path)
    report = _check_for(path)
    assert not report.all_triangles
    assert not report.mesh_ready
    assert any("non-triangle" in n for n in report.notes)

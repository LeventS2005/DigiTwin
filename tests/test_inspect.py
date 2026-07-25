from __future__ import annotations

import math
from pathlib import Path

from meshforge.io import file_sha256, load_surface
from meshforge.quality import inspect_surface

from conftest import SPHERE_RADIUS


def _report_for(path: Path):
    return inspect_surface(load_surface(path), path=str(path), sha256=file_sha256(path))


def test_sphere_ground_truth(sphere_stl: Path) -> None:
    report = _report_for(sphere_stl)

    assert report.all_triangles
    assert report.n_triangles == report.n_cells
    assert report.n_open_edges == 0
    assert report.is_manifold
    assert report.watertight
    assert report.notes == []

    # Analytic ground truth; a 60x60 sphere discretization sits well within 1%.
    exact_area = 4.0 * math.pi * SPHERE_RADIUS**2
    exact_volume = (4.0 / 3.0) * math.pi * SPHERE_RADIUS**3
    assert report.volume is not None
    assert abs(report.area - exact_area) / exact_area < 0.01
    assert abs(report.volume - exact_volume) / exact_volume < 0.01

    for lo, hi, half in zip(report.bounds[0::2], report.bounds[1::2], [SPHERE_RADIUS] * 3):
        assert abs(lo + half) < 0.05 * half
        assert abs(hi - half) < 0.05 * half


def test_open_surface_refuses_volume(open_plane_ply: Path) -> None:
    report = _report_for(open_plane_ply)

    assert report.n_open_edges > 0
    assert not report.watertight
    assert report.volume is None
    assert any("open edges" in note for note in report.notes)


def test_nonmanifold_surface_refuses_volume(nonmanifold_vtp: Path) -> None:
    report = _report_for(nonmanifold_vtp)

    assert not report.is_manifold
    assert not report.watertight
    assert report.volume is None
    assert any("non-manifold" in note for note in report.notes)


def test_report_serialization_roundtrip(sphere_stl: Path) -> None:
    import json

    report = _report_for(sphere_stl)
    parsed = json.loads(report.to_json())
    assert parsed["sha256"] == report.sha256
    assert parsed["watertight"] is True
    assert parsed["n_points"] == report.n_points

    text = report.to_text()
    assert report.sha256 in text
    assert "watertight  : True" in text

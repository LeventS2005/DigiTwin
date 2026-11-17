from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
import pyvista as pv

from digitwin.io import file_sha256, load_surface


def test_load_surface_returns_polydata(sphere_stl: Path) -> None:
    mesh = load_surface(sphere_stl)
    assert isinstance(mesh, pv.PolyData)
    assert mesh.n_points > 0
    assert mesh.n_cells > 0


def test_load_missing_file_fails_loudly(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="not found"):
        load_surface(tmp_path / "nope.stl")


def test_load_non_surface_fails_loudly(tmp_path: Path) -> None:
    grid = pv.ImageData(dimensions=(3, 3, 3)).to_tetrahedra()
    path = tmp_path / "volume.vtu"
    grid.save(path)
    with pytest.raises(TypeError, match="expected a surface"):
        load_surface(path)


def test_load_empty_surface_fails_loudly(tmp_path: Path) -> None:
    path = tmp_path / "empty.vtp"
    pv.PolyData().save(path)
    with pytest.raises(ValueError, match="empty"):
        load_surface(path)


def test_sha256_matches_reference_and_input_unchanged(sphere_stl: Path) -> None:
    raw_before = sphere_stl.read_bytes()
    checksum = file_sha256(sphere_stl)
    assert checksum == hashlib.sha256(raw_before).hexdigest()

    load_surface(sphere_stl)  # loading must be read-only
    assert sphere_stl.read_bytes() == raw_before
    assert file_sha256(sphere_stl) == checksum

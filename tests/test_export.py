from __future__ import annotations

from pathlib import Path

import meshio
import numpy as np
import pytest

from meshforge.export import export_surface, guard_output_path
from meshforge.io import load_surface


@pytest.mark.parametrize("fmt", ["vtu", "vtk", "ply", "obj"])
def test_export_roundtrip(sphere_stl: Path, tmp_path: Path, fmt: str) -> None:
    mesh = load_surface(sphere_stl)
    out = export_surface(mesh, tmp_path / f"sphere_out.{fmt}", input_path=sphere_stl)
    assert out.suffix == f".{fmt}"
    assert out.is_file()

    back = meshio.read(out)
    assert len(back.points) == mesh.n_points
    tri_blocks = [c for c in back.cells if c.type == "triangle"]
    assert sum(len(b.data) for b in tri_blocks) == mesh.n_cells
    # PLY/OBJ may store float32; compare with a tolerance, not exactly.
    np.testing.assert_allclose(np.asarray(back.points), mesh.points, rtol=1e-5, atol=1e-5)


def test_export_default_format_is_vtu(sphere_stl: Path, tmp_path: Path) -> None:
    mesh = load_surface(sphere_stl)
    out = export_surface(mesh, tmp_path / "noext", input_path=sphere_stl)
    assert out.suffix == ".vtu"


def test_explicit_format_wins_over_suffix(sphere_stl: Path, tmp_path: Path) -> None:
    mesh = load_surface(sphere_stl)
    out = export_surface(mesh, tmp_path / "conflict.ply", fmt="vtu", input_path=sphere_stl)
    assert out.suffix == ".vtu"
    assert not (tmp_path / "conflict.ply").exists()


def test_export_rejects_unknown_format(sphere_stl: Path, tmp_path: Path) -> None:
    mesh = load_surface(sphere_stl)
    with pytest.raises(ValueError, match="unknown export format"):
        export_surface(mesh, tmp_path / "surface.step", input_path=sphere_stl)
    with pytest.raises(ValueError, match="unknown export format"):
        export_surface(mesh, tmp_path / "x.vtu", fmt="step", input_path=sphere_stl)


def test_export_refuses_to_overwrite_input(tmp_path: Path, sphere_stl: Path) -> None:
    # The hard case on purpose: output path IS the input path (same file, .ply).
    mesh = load_surface(sphere_stl)
    ply_input = tmp_path / "input.ply"
    mesh.save(ply_input)
    raw_before = ply_input.read_bytes()

    with pytest.raises(ValueError, match="refusing to overwrite input"):
        export_surface(load_surface(ply_input), ply_input, input_path=ply_input)
    # Also via a relative-ish alias of the same file.
    alias = ply_input.parent / "." / "input.ply"
    with pytest.raises(ValueError, match="refusing to overwrite input"):
        export_surface(load_surface(ply_input), alias, input_path=ply_input)

    assert ply_input.read_bytes() == raw_before


def test_guard_output_path_ignores_none() -> None:
    guard_output_path("anywhere.vtu", input_paths=[None])  # must not raise

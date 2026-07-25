from __future__ import annotations

import hashlib
from pathlib import Path

import pyvista as pv


def file_sha256(path: str | Path) -> str:
    """SHA-256 of the file bytes; used to prove inputs stay byte-for-byte unchanged."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_surface(path: str | Path) -> pv.PolyData:
    """Load a triangulated surface (.stl/.ply/.vtp/.obj/...) read-only.

    Rejects non-surface data (volume grids, point clouds) rather than
    guessing a conversion — the caller must know what they loaded.
    """
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"surface file not found: {path}")
    mesh = pv.read(path)
    if not isinstance(mesh, pv.PolyData):
        raise TypeError(
            f"expected a surface (PolyData), got {type(mesh).__name__} from {path.name}; "
            "meshforge inspects surfaces, not volume grids"
        )
    if mesh.n_points == 0 or mesh.n_cells == 0:
        raise ValueError(f"surface is empty (0 points or 0 cells): {path.name}")
    return mesh

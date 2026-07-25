from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import pyvista as pv
from vtkmodules.util.vtkConstants import VTK_TRIANGLE


@dataclass
class SurfaceReport:
    """Read-only inspection result for one surface file.

    ``volume`` is None (with a note) when the surface is not watertight —
    a sentinel plus a human-readable reason, never a silently wrong number.
    """

    path: str
    sha256: str
    n_points: int
    n_cells: int
    n_triangles: int
    all_triangles: bool
    bounds: tuple[float, float, float, float, float, float]
    area: float
    n_open_edges: int
    is_manifold: bool
    watertight: bool
    volume: float | None
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "sha256": self.sha256,
            "n_points": self.n_points,
            "n_cells": self.n_cells,
            "n_triangles": self.n_triangles,
            "all_triangles": self.all_triangles,
            "bounds": list(self.bounds),
            "area": self.area,
            "n_open_edges": self.n_open_edges,
            "is_manifold": self.is_manifold,
            "watertight": self.watertight,
            "volume": self.volume,
            "notes": self.notes,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2)

    def to_text(self) -> str:
        b = self.bounds
        lines = [
            f"surface     : {self.path}",
            f"sha256      : {self.sha256}",
            f"points      : {self.n_points}",
            f"cells       : {self.n_cells}  (triangles: {self.n_triangles},"
            f" all-triangles: {self.all_triangles})",
            f"bounds      : x [{b[0]:.6g}, {b[1]:.6g}]"
            f"  y [{b[2]:.6g}, {b[3]:.6g}]  z [{b[4]:.6g}, {b[5]:.6g}]",
            f"area        : {self.area:.6g}",
            f"open edges  : {self.n_open_edges}",
            f"manifold    : {self.is_manifold}",
            f"watertight  : {self.watertight}",
            f"volume      : {'n/a' if self.volume is None else f'{self.volume:.6g}'}",
        ]
        for note in self.notes:
            lines.append(f"note        : {note}")
        return "\n".join(lines) + "\n"


def count_triangles(mesh: pv.PolyData) -> int:
    """Number of VTK_TRIANGLE cells (robust to mixed-polygon surfaces)."""
    celltypes = mesh.cast_to_unstructured_grid().celltypes
    return int((celltypes == VTK_TRIANGLE).sum())


def inspect_surface(mesh: pv.PolyData, *, path: str, sha256: str) -> SurfaceReport:
    """Compute read-only surface metrics. Never modifies ``mesh``."""
    notes: list[str] = []

    n_triangles = count_triangles(mesh)
    all_triangles = n_triangles == mesh.n_cells
    if not all_triangles:
        notes.append(
            f"{mesh.n_cells - n_triangles} non-triangle cells present; "
            "downstream meshing expects an all-triangle surface"
        )

    n_open_edges = int(mesh.n_open_edges)
    is_manifold = bool(mesh.is_manifold)
    watertight = n_open_edges == 0 and is_manifold

    volume: float | None
    if watertight:
        volume = float(mesh.volume)
    else:
        volume = None
        if n_open_edges > 0:
            notes.append(f"volume undefined: surface has {n_open_edges} open edges")
        if not is_manifold:
            notes.append("volume undefined: surface has non-manifold edges")

    return SurfaceReport(
        path=path,
        sha256=sha256,
        n_points=int(mesh.n_points),
        n_cells=int(mesh.n_cells),
        n_triangles=n_triangles,
        all_triangles=all_triangles,
        bounds=tuple(float(v) for v in mesh.bounds),
        area=float(mesh.area),
        n_open_edges=n_open_edges,
        is_manifold=is_manifold,
        watertight=watertight,
        volume=volume,
        notes=notes,
    )

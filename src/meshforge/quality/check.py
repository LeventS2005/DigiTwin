from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import pyvista as pv

from meshforge.quality.inspect import count_triangles


@dataclass
class CheckReport:
    """Mesh-readiness verdict for downstream (offset/volume-mesh) stages."""

    path: str
    sha256: str
    n_open_edges: int
    n_non_manifold_edges: int
    all_triangles: bool
    watertight: bool
    mesh_ready: bool
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "sha256": self.sha256,
            "n_open_edges": self.n_open_edges,
            "n_non_manifold_edges": self.n_non_manifold_edges,
            "all_triangles": self.all_triangles,
            "watertight": self.watertight,
            "mesh_ready": self.mesh_ready,
            "notes": self.notes,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2)

    def to_text(self) -> str:
        lines = [
            f"surface            : {self.path}",
            f"sha256             : {self.sha256}",
            f"open edges         : {self.n_open_edges}",
            f"non-manifold edges : {self.n_non_manifold_edges}",
            f"all triangles      : {self.all_triangles}",
            f"watertight         : {self.watertight}",
            f"mesh_ready         : {self.mesh_ready}",
        ]
        for note in self.notes:
            lines.append(f"note               : {note}")
        return "\n".join(lines) + "\n"


def check_surface(mesh: pv.PolyData, *, path: str, sha256: str) -> CheckReport:
    """Read-only readiness check: open edges, non-manifold edges, triangles."""
    notes: list[str] = []

    n_open = int(mesh.n_open_edges)
    non_manifold = mesh.extract_feature_edges(
        boundary_edges=False,
        non_manifold_edges=True,
        feature_edges=False,
        manifold_edges=False,
    )
    n_non_manifold = int(non_manifold.n_cells)

    n_triangles = count_triangles(mesh)
    all_triangles = n_triangles == mesh.n_cells
    watertight = n_open == 0 and n_non_manifold == 0

    if n_open > 0:
        notes.append(f"{n_open} open edges: surface is not closed")
    if n_non_manifold > 0:
        notes.append(f"{n_non_manifold} non-manifold edges: ambiguous inside/outside")
    if not all_triangles:
        notes.append(
            f"{mesh.n_cells - n_triangles} non-triangle cells: run triangulation first"
        )

    return CheckReport(
        path=path,
        sha256=sha256,
        n_open_edges=n_open,
        n_non_manifold_edges=n_non_manifold,
        all_triangles=all_triangles,
        watertight=watertight,
        mesh_ready=watertight and all_triangles,
        notes=notes,
    )

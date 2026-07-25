"""Centerlines, frames, cross-sections, wall offsets (native backend)."""
from __future__ import annotations

from meshforge.geometry.backends import NativeGeometryBackend, get_backend
from meshforge.geometry.centerline import (
    Centerline,
    centerline_from_surface,
    compute_centerline,
    embed_centerline,
)
from meshforge.geometry.frames import parallel_transport_frames
from meshforge.geometry.sections import (
    Constriction,
    Section,
    compute_sections,
    detect_constrictions,
)
from meshforge.geometry.thickness import ThicknessSpec, resolve_thickness
from meshforge.geometry.tree import Branch, TreeSkeleton, extract_tree, mesh_tree_wall
from meshforge.geometry.wall import WallResult, offset_wall

__all__ = [
    "Branch",
    "TreeSkeleton",
    "extract_tree",
    "mesh_tree_wall",
    "Centerline",
    "Constriction",
    "NativeGeometryBackend",
    "Section",
    "ThicknessSpec",
    "WallResult",
    "resolve_thickness",
    "centerline_from_surface",
    "compute_centerline",
    "compute_sections",
    "embed_centerline",
    "detect_constrictions",
    "get_backend",
    "offset_wall",
    "parallel_transport_frames",
]

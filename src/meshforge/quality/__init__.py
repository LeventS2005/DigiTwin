"""Surface/volume mesh quality, inspection, and repair."""
from __future__ import annotations

from meshforge.quality.check import CheckReport, check_surface
from meshforge.quality.inspect import SurfaceReport, count_triangles, inspect_surface
from meshforge.quality.repair import RepairReport, degrade_surface, repair_surface

__all__ = [
    "CheckReport",
    "RepairReport",
    "SurfaceReport",
    "check_surface",
    "count_triangles",
    "degrade_surface",
    "inspect_surface",
    "repair_surface",
]

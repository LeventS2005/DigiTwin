"""Surface/volume mesh quality, inspection, and repair."""
from __future__ import annotations

from digitwin.quality.check import CheckReport, check_surface
from digitwin.quality.inspect import SurfaceReport, count_triangles, inspect_surface
from digitwin.quality.repair import RepairReport, degrade_surface, repair_surface

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

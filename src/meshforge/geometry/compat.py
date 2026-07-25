"""Version shims for pyvista API churn (BACKLOG #5).

pyvista 0.48 deprecated ``select_enclosed_points`` in favor of
``select_interior_points`` (keyword-only, renamed output array
``selected_points``). This wrapper spans both so the pin can move freely.
"""
from __future__ import annotations

import numpy as np
import pyvista as pv


def interior_point_mask(
    grid, surface: pv.PolyData, *, check_surface: bool = True
) -> np.ndarray:
    """Boolean per-point mask of grid points inside the closed surface."""
    if hasattr(grid, "select_interior_points"):
        sel = grid.select_interior_points(surface, check_surface=check_surface)
        return np.asarray(sel["selected_points"]).astype(bool)
    sel = grid.select_enclosed_points(surface, tolerance=0.0, check_surface=check_surface)
    return np.asarray(sel["SelectedPoints"]).astype(bool)

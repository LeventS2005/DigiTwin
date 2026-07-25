from __future__ import annotations

import numpy as np
import pyvista as pv

from meshforge.geometry.centerline import MAX_VOXELS


def sdf_offset_surface(
    surface: pv.PolyData,
    thickness: float,
    *,
    spacing: float | None = None,
) -> tuple[pv.PolyData, list[str]]:
    """Outer wall as the +thickness level set of a signed distance field.

    Watertight and non-self-intersecting by construction (level sets cannot
    fold, unlike per-vertex normal offsets — trap #9). Resolution rule from
    trap #10: ~3 voxels across the wall (default spacing = t/3), grid padded
    past the level set so the isosurface is never clipped, and accuracy
    bought with grid size — never by decimating the input.
    """
    if thickness <= 0:
        raise ValueError(f"thickness must be positive, got {thickness}")
    if surface.n_open_edges > 0:
        raise ValueError("SDF offset needs a watertight surface")
    if spacing is None:
        spacing = thickness / 3.0
    if spacing > thickness / 2.0:
        raise ValueError(
            f"spacing {spacing:.4g} is coarser than thickness/2; "
            "resolve at least ~3 voxels across the wall (trap: badly "
            "undercounted wall volume)"
        )

    pad = thickness + 4.0 * spacing
    b = surface.bounds
    origin = np.array([b[0], b[2], b[4]]) - pad
    extent = np.array([b[1] - b[0], b[3] - b[2], b[5] - b[4]]) + 2.0 * pad
    dims = np.ceil(extent / spacing).astype(int) + 1
    if int(np.prod(dims)) > MAX_VOXELS:
        raise ValueError(
            f"SDF grid {tuple(dims)} exceeds {MAX_VOXELS} voxels; "
            f"increase spacing (currently {spacing:.4g})"
        )

    # Exact signed distance to the triangulated surface (negative inside):
    # a voxel-EDT field carries an O(spacing) outward bias (~+3% wall volume
    # at spacing=t/3); vtkImplicitPolyDataDistance has no such bias, so grid
    # spacing only controls level-set feature resolution.
    grid = pv.ImageData(dimensions=dims, spacing=(spacing,) * 3, origin=origin)
    grid = grid.compute_implicit_distance(surface)
    outer = (
        grid.contour([thickness], scalars="implicit_distance")
        .extract_largest()
        .triangulate()
        .clean()
    )
    # contour() can leave degenerate line cells behind; keep faces only.
    outer = pv.PolyData(outer.points, outer.faces)

    if outer.n_points == 0:
        raise ValueError("SDF isosurface is empty; thickness/spacing inconsistent")
    if outer.n_open_edges > 0:
        raise ValueError(
            f"SDF isosurface has {outer.n_open_edges} open edges — the grid "
            "padding failed to contain the level set (report this; do not "
            "fail silently)"
        )
    notes = [
        f"sdf offset: spacing {spacing:.4g} (~{thickness / spacing:.1f} voxels across wall), "
        f"grid {tuple(int(d) for d in dims)}, level {thickness:.4g}"
    ]
    return outer, notes

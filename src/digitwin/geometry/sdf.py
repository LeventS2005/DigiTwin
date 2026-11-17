from __future__ import annotations

import math

import numpy as np
import pyvista as pv

from digitwin.geometry.centerline import MAX_VOXELS


def _grid_shape(bounds, thickness: float, spacing: float):
    """``(origin, dims)`` of the SDF grid with ``dims`` kept as float64.

    The ONE place the grid geometry is derived, so the budget message and the
    grid that is actually built can never disagree. Float dims because the
    budget search probes thicknesses small enough to overflow an int cast —
    ``inf`` compares honestly, a wrapped ``np.int64`` does not.
    """
    pad = thickness + 4.0 * spacing
    origin = np.array([bounds[0], bounds[2], bounds[4]]) - pad
    extent = (
        np.array(
            [bounds[1] - bounds[0], bounds[3] - bounds[2], bounds[5] - bounds[4]]
        )
        + 2.0 * pad
    )
    return origin, np.ceil(extent / spacing) + 1.0


def min_thickness_for_budget(bounds, max_voxels: int = MAX_VOXELS) -> float:
    """Smallest DEFAULT-spacing thickness whose SDF grid fits ``max_voxels``.

    ``spacing = thickness / 3``, so a thinner wall means a finer grid: the
    voxel count falls as thickness rises. The user-facing knob is ``thickness``
    (there is no ``spacing`` knob), so a budget refusal has to quote a real
    thickness to raise it to — this computes that number for the surface at
    hand instead of leaving the user to guess.

    Rounded UP to a friendly 2 decimals, then re-checked, so the quoted value
    is always one that actually fits.
    """

    def fits(t: float) -> bool:
        if not t > 0.0:
            return False  # spacing -> 0 -> an infinite grid; never "fits"
        _, dims = _grid_shape(bounds, t, t / 3.0)
        n = float(np.prod(dims))
        return math.isfinite(n) and n <= max_voxels

    # As thickness grows the grid shrinks towards a constant, so an upper
    # bound always exists; find one, then bisect.
    hi = max(float(bounds[1] - bounds[0]), 1e-6)
    for _ in range(64):
        if fits(hi):
            break
        hi *= 2.0
    else:  # pragma: no cover - unreachable for any finite bounding box
        raise ValueError("no thickness fits the SDF voxel budget for these bounds")
    lo = 0.0
    for _ in range(80):
        mid = 0.5 * (lo + hi)
        if mid <= 0.0:
            break
        if fits(mid):
            hi = mid
        else:
            lo = mid
    friendly = math.ceil(hi * 100.0) / 100.0
    while not fits(friendly):  # pragma: no cover - rounding safety net
        friendly += 0.01
    return friendly


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
    spacing_given = spacing is not None
    if spacing is None:
        spacing = thickness / 3.0
    if not spacing > 0:
        raise ValueError(f"spacing must be positive, got {spacing}")
    if spacing > thickness / 2.0:
        raise ValueError(
            f"spacing {spacing:.4g} is coarser than thickness/2; "
            "resolve at least ~3 voxels across the wall (trap: badly "
            "undercounted wall volume)"
        )

    # Budget FIRST: dims are known from the bounding box alone, so an
    # unaffordable grid is refused in milliseconds — never after paying for
    # compute_implicit_distance below.
    b = surface.bounds
    origin, dims_f = _grid_shape(b, thickness, spacing)
    dims = dims_f.astype(int)
    n_voxels = int(np.prod(dims_f))
    if n_voxels > MAX_VOXELS:
        nx, ny, nz = (int(d) for d in dims)  # plain ints: never leak np.int64
        box = " x ".join(
            f"{float(b[2 * i + 1] - b[2 * i]):.4g}" for i in range(3)
        )
        if spacing_given:
            raise ValueError(
                f"SDF grid {nx} x {ny} x {nz} = {n_voxels} voxels exceeds the "
                f"{MAX_VOXELS} budget for this {box} bounding box; "
                f"raise the explicit spacing (currently {spacing:.4g}) or the "
                f"thickness (currently {thickness:.4g})"
            )
        t_min = min_thickness_for_budget(b)
        raise ValueError(
            f"SDF grid {nx} x {ny} x {nz} = {n_voxels} voxels exceeds the "
            f"{MAX_VOXELS} budget for this {box} bounding box. The grid is "
            f"sized off thickness (spacing = thickness/3 = {spacing:.4g}), so "
            f"raise thickness: {t_min:.4g} or more fits (currently "
            f"{thickness:.4g})."
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

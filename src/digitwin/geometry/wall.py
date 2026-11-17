from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pyvista as pv

WALL_MODES = ("sdf", "uniform", "radius_scaled")


@dataclass
class WallResult:
    """Outer offset surface + the per-vertex thickness actually applied."""

    outer: pv.PolyData
    thickness: np.ndarray  # (n_points,) applied offset per inner vertex
    n_capped: int  # vertices limited by the curvature cap
    n_floored: int  # vertices raised to the floor
    notes: list[str] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        return {
            "thickness_min": float(self.thickness.min()),
            "thickness_mean": float(self.thickness.mean()),
            "thickness_max": float(self.thickness.max()),
            "n_capped": self.n_capped,
            "n_floored": self.n_floored,
            "notes": self.notes,
        }


def offset_wall(
    surface: pv.PolyData,
    thickness: float | None = None,
    *,
    thickness_spec=None,
    mode: str = "sdf",
    centerline=None,
    curvature_cap: float = 0.5,
    floor_frac: float = 0.2,
    sdf_spacing: float | None = None,
) -> WallResult:
    """Outer wall construction. Default ``sdf``: level-set offset, watertight
    and non-self-intersecting by construction (hardening series default).

    ``uniform``/``radius_scaled``: per-vertex normal offset with a curvature
    guard — an outward offset by t folds where a principal curvature κ < 0
    and t ≥ −1/κ, so thickness is capped at ``curvature_cap``·(−1/κ) on
    concave vertices and floored at ``floor_frac``·t. All messages reduce
    per-vertex arrays to scalars explicitly — never format an ndarray.
    """
    from digitwin.geometry.thickness import resolve_thickness

    if mode not in WALL_MODES:
        raise ValueError(f"unknown wall mode {mode!r}; supported: {WALL_MODES}")
    if surface.n_open_edges > 0:
        raise ValueError("wall offset needs a watertight inner surface")
    spec = resolve_thickness(thickness, thickness_spec)

    if spec.kind == "outer":
        # Measured outer surface IS the wall — ingest it, report realized
        # thickness with the end-cap guard+repair (trap #4).
        inner_n = surface.compute_normals(
            point_normals=True, cell_normals=False, auto_orient_normals=True
        )
        t_applied = spec.sample(np.asarray(inner_n.points), normals=inner_n["Normals"])
        outer = spec.outer.triangulate()
        if outer.volume <= surface.volume:
            raise ValueError(
                "measured outer surface volume does not exceed the inner "
                "surface; it cannot be the outer wall"
            )
        return WallResult(
            outer=outer,
            thickness=t_applied,
            n_capped=0,
            n_floored=0,
            notes=["measured outer surface ingested"] + spec.notes,
        )

    if mode == "sdf":
        if spec.kind != "scalar":
            raise ValueError(
                "sdf mode needs a scalar thickness; use mode='uniform' for "
                "per-position thickness fields"
            )
        thickness = spec.scalar
        from scipy.spatial import cKDTree

        from digitwin.geometry.sdf import sdf_offset_surface

        outer, notes = sdf_offset_surface(surface, thickness, spacing=sdf_spacing)
        # Report the realized per-inner-vertex wall thickness for inspection.
        tree = cKDTree(outer.points)
        t_applied, _ = tree.query(surface.points)
        if outer.volume <= surface.volume:
            raise ValueError("SDF outer volume did not grow; field sign error")
        return WallResult(
            outer=outer,
            thickness=np.asarray(t_applied),
            n_capped=0,
            n_floored=0,
            notes=notes,
        )

    notes = []
    inner = surface.compute_normals(
        point_normals=True, cell_normals=False, auto_orient_normals=True
    )
    normals = inner["Normals"]

    if spec.kind == "scalar":
        nominal = float(spec.scalar)
        t = np.full(inner.n_points, nominal)
    else:  # per-position field (array/callable)
        t = spec.sample(np.asarray(inner.points), normals=normals)
        nominal = float(np.median(t))
        notes.extend(spec.notes)
        notes.append(
            f"thickness field ({spec.kind}): {float(t.min()):.4g}"
            f"..{float(t.max()):.4g}, median {nominal:.4g}"
        )
    if mode == "radius_scaled":
        if centerline is None:
            raise ValueError("radius_scaled mode requires a centerline")
        from scipy.spatial import cKDTree

        tree = cKDTree(centerline.points)
        r_local, _ = tree.query(inner.points)
        r_ref = float(np.mean(centerline.radii))
        t = t * (r_local / r_ref)
        notes.append(
            f"radius_scaled: local radius {float(r_local.min()):.4g}"
            f"..{float(r_local.max()):.4g}, reference {r_ref:.4g}"
        )

    # Curvature guard on the concave principal direction.
    kappa_min = np.asarray(inner.curvature("minimum"))
    kappa_min = np.where(np.isfinite(kappa_min), kappa_min, 0.0)
    concave = kappa_min < 0
    cap = np.full(inner.n_points, np.inf)
    cap[concave] = curvature_cap / (-kappa_min[concave])
    capped = t > cap
    t = np.minimum(t, cap)

    floor = floor_frac * nominal
    floored = t < floor
    t = np.maximum(t, floor)

    n_capped = int(capped.sum())
    n_floored = int(floored.sum())
    if n_capped:
        notes.append(
            f"curvature cap limited {n_capped} vertices "
            f"(min applied thickness {float(t.min()):.4g})"
        )
    if n_floored:
        notes.append(
            f"floor raised {n_floored} vertices to {floor:.4g}; "
            "inspect the bend — consider the SDF offset backend"
        )

    outer = inner.copy()
    outer.points = inner.points + normals * t[:, None]
    outer = outer.compute_normals(auto_orient_normals=True)

    if outer.volume <= surface.volume:
        raise ValueError(
            f"offset produced a smaller volume ({float(outer.volume):.4g} <= "
            f"{float(surface.volume):.4g}); normals or curvature guard failed"
        )
    return WallResult(
        outer=outer, thickness=t, n_capped=n_capped, n_floored=n_floored, notes=notes
    )

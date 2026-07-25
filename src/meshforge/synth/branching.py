"""Branching synthetic surfaces (V11): the Y-junction tree.

Three flat-capped cylinder segments (parent + two children) are SDF-unioned
and the zero level set extracted by marching cubes — a clean watertight
join with no seam geometry. Tips are FLAT (see _cylinder_sdf: thinning
erodes grid-aligned round tips into nothing), so like the tube generators
each branch's medial axis stops ~one radius short of its tip.

Truth: exact per-branch axes/radii/lengths away from the junction ball,
plus a total volume from a partial-volume-weighted numeric integral of the
EXACT field (accurate to O(h²), far beyond mesh error).
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pyvista as pv


@dataclass
class BranchTruth:
    name: str
    start: np.ndarray  # (3,) junction end of the axis
    end: np.ndarray  # (3,) tip end (center of the tip sphere)
    radius: float

    @property
    def direction(self) -> np.ndarray:
        d = self.end - self.start
        return d / np.linalg.norm(d)

    @property
    def length(self) -> float:
        return float(np.linalg.norm(self.end - self.start))


@dataclass
class TreeTruth:
    shape: str
    params: dict[str, Any]
    branches: list[BranchTruth]
    junction: np.ndarray  # (3,)
    volume: float  # numeric integral of the exact union field
    surface_area: float | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def n_branches(self) -> int:
        return len(self.branches)

    def to_dict(self) -> dict[str, Any]:
        return {
            "shape": self.shape,
            "params": self.params,
            "junction": self.junction.tolist(),
            "volume": self.volume,
            "surface_area": self.surface_area,
            "branches": [
                {
                    "name": b.name,
                    "start": b.start.tolist(),
                    "end": b.end.tolist(),
                    "radius": b.radius,
                    "length": b.length,
                }
                for b in self.branches
            ],
            "notes": self.notes,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2)


def _cylinder_sdf(pts: np.ndarray, a: np.ndarray, b: np.ndarray, r: float) -> np.ndarray:
    """Exact SDF of a FLAT-capped finite cylinder from a to b.

    Flat caps on purpose: 3-D thinning erodes a grid-aligned ROUND tip
    symmetrically (no voxel ever becomes an endpoint) and the whole branch
    skeleton collapses — the axis-aligned parent of the Y-junction lost its
    skeleton entirely with capsules. Flat caps behave like the tube
    generators: the medial axis stops ~one radius short of each tip.
    """
    ab = b - a
    length = float(np.linalg.norm(ab))
    axis = ab / length
    ap = pts - a
    z = ap @ axis
    radial = np.linalg.norm(ap - z[:, None] * axis[None, :], axis=1)
    dr = radial - r
    dz = np.maximum(-z, z - length)
    outside = np.sqrt(np.maximum(dr, 0.0) ** 2 + np.maximum(dz, 0.0) ** 2)
    inside = np.minimum(np.maximum(dr, dz), 0.0)
    return outside + inside


def y_junction(
    *,
    parent_radius: float = 1.5,
    child_radius: float = 1.1,
    parent_length: float = 8.0,
    child_length: float = 7.0,
    angle: float = 0.6,
    spacing: float | None = None,
) -> tuple[pv.PolyData, TreeTruth]:
    """Parent tube splitting into two children at ±angle from its axis."""
    for name, v in (
        ("parent_radius", parent_radius),
        ("child_radius", child_radius),
        ("parent_length", parent_length),
        ("child_length", child_length),
        ("angle", angle),
    ):
        if not v > 0:
            raise ValueError(f"{name} must be positive, got {v}")
    if not angle < math.pi / 2:
        raise ValueError(f"angle must be < pi/2 (children ahead of the split), got {angle}")
    if spacing is None:
        spacing = min(parent_radius, child_radius) / 6.0

    origin = np.zeros(3)
    p_start = np.array([0.0, 0.0, -parent_length])
    c_dir1 = np.array([math.sin(angle), 0.0, math.cos(angle)])
    c_dir2 = np.array([-math.sin(angle), 0.0, math.cos(angle)])
    segments = [
        ("parent", p_start, origin, parent_radius),
        ("child_a", origin, c_dir1 * child_length, child_radius),
        ("child_b", origin, c_dir2 * child_length, child_radius),
    ]

    all_ends = np.array([p_start, origin, c_dir1 * child_length, c_dir2 * child_length])
    r_max = max(parent_radius, child_radius)
    lo = all_ends.min(axis=0) - (r_max + 4 * spacing)
    hi = all_ends.max(axis=0) + (r_max + 4 * spacing)
    dims = np.ceil((hi - lo) / spacing).astype(int) + 1
    grid = pv.ImageData(dimensions=dims, spacing=(spacing,) * 3, origin=lo)
    pts = np.asarray(grid.points, dtype=float)

    d = np.full(len(pts), np.inf)
    for _, a, b, r in segments:
        d = np.minimum(d, _cylinder_sdf(pts, a, b, r))
    grid.point_data["d"] = d
    surface = grid.contour([0.0], scalars="d").triangulate().clean()
    if surface.n_cells == 0 or surface.n_open_edges > 0:
        raise ValueError(
            "y_junction level set is not watertight — decrease spacing "
            f"(currently {spacing:.4g})"
        )
    # contour() on this field emits INWARD normals (pv .volume hides it by
    # returning a magnitude) — downstream interior selection needs outward.
    p = np.asarray(surface.points)
    tri = surface.faces.reshape(-1, 4)[:, 1:]
    signed = float(
        np.einsum("ij,ij->i", p[tri[:, 0]], np.cross(p[tri[:, 1]], p[tri[:, 2]])).sum()
        / 6.0
    )
    if signed < 0:
        faces = np.column_stack(
            [np.full(len(tri), 3, dtype=np.int64), tri[:, ::-1]]
        ).ravel()
        surface = pv.PolyData(p.copy(), faces)

    # Partial-volume weighted integral of the exact field: O(h^2) accurate.
    frac = np.clip(0.5 - d / spacing, 0.0, 1.0)
    volume = float(frac.sum()) * spacing**3

    truth = TreeTruth(
        shape="y_junction",
        params={
            "parent_radius": parent_radius,
            "child_radius": child_radius,
            "parent_length": parent_length,
            "child_length": child_length,
            "angle": angle,
            "spacing": spacing,
        },
        branches=[
            # Normalized so start = junction side, end = tip side.
            BranchTruth("parent", origin.copy(), p_start.copy(), parent_radius),
            BranchTruth("child_a", origin.copy(), c_dir1 * child_length, child_radius),
            BranchTruth("child_b", origin.copy(), c_dir2 * child_length, child_radius),
        ],
        junction=origin,
        volume=volume,
        notes=[
            "volume is a partial-volume weighted quadrature of the exact "
            "union SDF (not mesh-derived)",
            "branch axes are exact away from the junction blend ball",
        ],
    )
    return surface, truth


TREES = {"y_junction": y_junction}


def generate_tree(shape: str, **params: Any) -> tuple[pv.PolyData, TreeTruth]:
    try:
        gen = TREES[shape]
    except KeyError:
        raise ValueError(
            f"unknown tree shape {shape!r}; available: {', '.join(sorted(TREES))}"
        ) from None
    return gen(**params)

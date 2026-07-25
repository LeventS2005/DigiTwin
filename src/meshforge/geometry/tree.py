"""Branching-tree skeletons (V11): junction detection + per-branch paths.

The voxel skeleton of a branched vessel is a graph, not a path: tips are
degree-1 voxels, the junction is a cluster of degree>=3 voxels. Each branch
is the geodesic from a tip to the junction cluster, smoothed and resampled
exactly like the single-tube centerline (Branch duck-types Centerline for
sections). Wall meshing goes SDF offset + tet on the shell pair — a swept
lattice has no single sweep axis at a junction and is refused loudly.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import networkx as nx
import numpy as np
import pyvista as pv
from scipy.interpolate import interp1d, splev, splprep

from meshforge.geometry.centerline import _skeleton_graph, _voxelize_interior


@dataclass
class Branch:
    """One tip-to-junction path; duck-types Centerline for compute_sections."""

    name: str
    points: np.ndarray  # (N, 3), points[0] = tip side, points[-1] = junction
    tangents: np.ndarray
    arc_lengths: np.ndarray
    radii: np.ndarray
    length: float
    closed: bool = False

    @property
    def tip(self) -> np.ndarray:
        return self.points[0]


@dataclass
class TreeSkeleton:
    branches: list[Branch]
    junction: np.ndarray  # (3,)
    voxel_size: float
    notes: list[str] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        return {
            "n_branches": len(self.branches),
            "junction": self.junction.tolist(),
            "branch_lengths": [b.length for b in self.branches],
            "branch_radii_mid": [
                float(np.median(b.radii)) for b in self.branches
            ],
            "voxel_size": self.voxel_size,
            "notes": self.notes,
        }


def extract_tree(
    surface: pv.PolyData,
    *,
    voxel_size: float | None = None,
    smoothing: float | None = None,
    n_samples: int = 80,
) -> TreeSkeleton:
    """Voxel/EDT/skeleton tree: branch tips, junction cluster, branch paths."""
    import math

    from scipy import ndimage

    notes: list[str] = []
    if surface.n_open_edges > 0:
        raise ValueError(
            f"surface has {surface.n_open_edges} open edges; "
            "tree extraction needs a watertight surface"
        )
    if voxel_size is None:
        voxel_size = math.dist(surface.bounds[::2], surface.bounds[1::2]) / 150.0
        notes.append(f"voxel_size defaulted to bbox_diag/150 = {voxel_size:.4g}")
    if smoothing is None:
        smoothing = voxel_size

    mask, origin = _voxelize_interior(surface, voxel_size)
    if not mask.any():
        raise ValueError("voxelization produced an empty interior")
    edt = ndimage.distance_transform_edt(mask, sampling=voxel_size)

    from skimage.morphology import skeletonize

    skeleton = skeletonize(mask)
    graph = _skeleton_graph(skeleton, voxel_size)
    if graph.number_of_nodes() == 0:
        raise ValueError("skeleton is empty; surface may be too thin for the grid")
    main = max(nx.connected_components(graph), key=len)
    sub = graph.subgraph(main)
    voxels = np.argwhere(skeleton)

    tips = [n for n in main if sub.degree(n) == 1]
    branch_voxels = [n for n in main if sub.degree(n) >= 3]
    if not branch_voxels:
        raise ValueError(
            "no junction found in the skeleton — this looks like an "
            "unbranched tube; use compute_centerline instead"
        )
    if len(tips) < 3:
        raise ValueError(
            f"tree extraction found a junction but only {len(tips)} tip(s); "
            "the skeleton is too coarse — decrease voxel_size"
        )

    # Junction cluster: the largest connected component of degree>=3 voxels.
    jsub = sub.subgraph(branch_voxels)
    clusters = sorted(nx.connected_components(jsub), key=len, reverse=True)
    junction_nodes = set(clusters[0])
    junction = (origin + voxels[list(junction_nodes)] * voxel_size).mean(axis=0)
    if len(clusters) > 1:
        notes.append(
            f"{len(clusters)} junction clusters found; using the largest "
            f"({len(junction_nodes)} voxels) — others may be skeleton noise"
        )

    lengths = nx.multi_source_dijkstra_path_length(
        sub, junction_nodes, weight="weight"
    )
    branches: list[Branch] = []
    for k, tip in enumerate(sorted(tips, key=lambda n: -lengths.get(n, 0.0))):
        if tip not in lengths:
            notes.append(f"tip {k}: unreachable from the junction — skipped")
            continue
        # Geodesic tip -> nearest junction voxel.
        path = nx.multi_source_dijkstra(sub, junction_nodes, tip, weight="weight")[1]
        path = list(reversed(path))  # tip ... junction
        pts = origin + voxels[path] * voxel_size
        pts = np.vstack([pts, junction])  # snap the end onto the cluster centroid
        radii_raw = edt[tuple(voxels[path].T)]

        if len(pts) < 4:
            notes.append(f"branch {k}: only {len(pts)} voxels — skipped")
            continue
        s_param = len(pts) * smoothing**2
        tck, _ = splprep(pts.T, s=s_param, k=min(3, len(pts) - 1))
        u = np.linspace(0.0, 1.0, max(10 * n_samples, 500))
        dense = np.column_stack(splev(u, tck))
        seg = np.linalg.norm(np.diff(dense, axis=0), axis=1)
        cum = np.concatenate([[0.0], np.cumsum(seg)])
        length = float(cum[-1])
        targets = np.linspace(0.0, length, n_samples)
        uu = interp1d(cum, u)(targets)
        points = np.column_stack(splev(uu, tck))
        deriv = np.column_stack(splev(uu, tck, der=1))
        tangents = deriv / np.linalg.norm(deriv, axis=1)[:, None]
        raw_cum = np.concatenate(
            [[0.0], np.cumsum(np.linalg.norm(np.diff(pts[:-1], axis=0), axis=1))]
        )
        raw_cum *= length / raw_cum[-1] if raw_cum[-1] > 0 else 1.0
        radii = interp1d(
            raw_cum,
            radii_raw,
            bounds_error=False,
            fill_value=(radii_raw[0], radii_raw[-1]),
        )(targets)
        branches.append(
            Branch(
                name=f"branch_{k}",
                points=points,
                tangents=tangents,
                arc_lengths=targets,
                radii=np.asarray(radii),
                length=length,
            )
        )

    if len(branches) < 3:
        raise ValueError(
            f"tree extraction produced only {len(branches)} usable branch(es); "
            "see notes: " + "; ".join(notes)
        )
    notes.append(
        f"tree: {len(branches)} branches, junction cluster "
        f"{len(junction_nodes)} voxels at ({junction[0]:.4g}, "
        f"{junction[1]:.4g}, {junction[2]:.4g})"
    )
    return TreeSkeleton(
        branches=branches, junction=junction, voxel_size=voxel_size, notes=notes
    )


def mesh_tree_wall(
    surface: pv.PolyData,
    *,
    thickness: float,
    backend: str = "tet",
    spacing: float | None = None,
    mesh_size: float | None = None,
    optimize: bool = True,
):
    """Wall mesh for a branched surface: SDF offset (whole tree) + tets
    between the shell pair. The swept backend is refused: a junction has no
    single sweep axis."""
    if backend != "tet":
        raise ValueError(
            f"backend {backend!r} is refused at junctions: a swept lattice "
            "needs one sweep axis and a branching tree has none — use "
            "backend='tet'"
        )
    if not thickness > 0:
        raise ValueError(f"thickness must be positive, got {thickness}")
    from meshforge.geometry.sdf import sdf_offset_surface
    from meshforge.meshing.tet import mesh_shell_pair

    outer, sdf_notes = sdf_offset_surface(surface, thickness, spacing=spacing)
    wall = mesh_shell_pair(
        surface, outer, mesh_size=mesh_size or 0.8 * thickness, optimize=optimize
    )
    wall.notes.extend(sdf_notes)
    return wall

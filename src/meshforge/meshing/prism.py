"""External-flow boundary layers (V22): wedge/prism layers grown OUTWARD
from a closed triangulated body surface — the CFD-side complement of the
wall-internal swept layers.

Growth directions are area-weighted vertex normals, Laplacian-smoothed
over the vertex adjacency so concave features share blended directions.
Wedges use the probe-verified positive-volume VTK ordering (bottom tri
wound so its normal points AWAY from the top: (a, c, b | a+, c+, b+)).

Honest limit (noted on the mesh): per-cell inversions are counted by the
nan-aware quality pass, but GLOBAL self-intersection of far-apart columns
(layers thicker than a gap they grow into) is not detected.
"""
from __future__ import annotations

import numpy as np
import pyvista as pv

from meshforge.meshing.base import BoundaryGroup, WallMesh
from meshforge.quality.volume import volume_quality


def _vertex_normals(points: np.ndarray, tris: np.ndarray, smoothing: int) -> np.ndarray:
    face_n = np.cross(
        points[tris[:, 1]] - points[tris[:, 0]],
        points[tris[:, 2]] - points[tris[:, 0]],
    )  # length = 2x area: area weighting for free
    normals = np.zeros_like(points)
    for k in range(3):
        np.add.at(normals, tris[:, k], face_n)

    # Vertex adjacency for the Laplacian passes.
    nbrs: dict[int, set[int]] = {}
    for a, b, c in tris:
        for i, j in ((a, b), (b, c), (c, a)):
            nbrs.setdefault(int(i), set()).add(int(j))
            nbrs.setdefault(int(j), set()).add(int(i))

    def _unit(v: np.ndarray) -> np.ndarray:
        norm = np.linalg.norm(v, axis=1, keepdims=True)
        if np.any(norm == 0.0):
            raise ValueError(
                "degenerate vertex normal (zero length) — the surface has "
                "a vertex whose incident faces cancel; repair it first"
            )
        return v / norm

    normals = _unit(normals)
    for _ in range(smoothing):
        acc = normals.copy()
        for i, js in nbrs.items():
            acc[i] = normals[i] + normals[list(js)].sum(axis=0)
        normals = _unit(acc)
    return normals


def mesh_boundary_layers(
    surface: pv.PolyData,
    first_thickness: float,
    *,
    n_layers: int = 5,
    growth: float = 1.2,
    normal_smoothing: int = 10,
) -> WallMesh:
    """Grow ``n_layers`` geometric wedge layers outward from ``surface``."""
    if not first_thickness > 0:
        raise ValueError(f"first_thickness must be positive, got {first_thickness}")
    if n_layers < 1:
        raise ValueError(f"n_layers must be >= 1, got {n_layers}")
    if not growth >= 1.0:
        raise ValueError(f"growth must be >= 1, got {growth}")
    if normal_smoothing < 0:
        raise ValueError(f"normal_smoothing must be >= 0, got {normal_smoothing}")
    if not surface.is_all_triangles:
        raise ValueError("boundary layers need an all-triangle surface")
    if surface.n_open_edges > 0 or not surface.is_manifold:
        raise ValueError(
            f"boundary layers need a closed manifold body surface "
            f"({surface.n_open_edges} open edge(s)); repair it first"
        )

    base = np.asarray(surface.points, dtype=float)
    tris = surface.faces.reshape(-1, 4)[:, 1:].astype(np.int64)

    # Outward orientation, never assumed: signed volume must be positive.
    signed = float(
        np.einsum(
            "ij,ij->i", base[tris[:, 0]], np.cross(base[tris[:, 1]], base[tris[:, 2]])
        ).sum()
        / 6.0
    )
    if signed == 0.0:
        raise ValueError("surface has zero signed volume — degenerate body")
    if signed < 0:
        tris = tris[:, ::-1].copy()

    normals = _vertex_normals(base, tris, normal_smoothing)

    if growth == 1.0:
        dists = first_thickness * np.arange(1, n_layers + 1, dtype=float)
    else:
        dists = first_thickness * (growth ** np.arange(1, n_layers + 1) - 1.0) / (
            growth - 1.0
        )
    n_verts = len(base)
    points = np.vstack([base] + [base + d * normals for d in dists])

    wedges = []
    for k in range(n_layers):
        lo, hi = k * n_verts, (k + 1) * n_verts
        for a, b, c in tris:
            # Positive VTK wedge: bottom wound away from the top (probe D36).
            wedges.append(
                [6, lo + a, lo + c, lo + b, hi + a, hi + c, hi + b]
            )
    cells = np.asarray(wedges, dtype=np.int64).ravel()
    celltypes = np.full(n_layers * len(tris), pv.CellType.WEDGE, dtype=np.uint8)
    grid = pv.UnstructuredGrid(cells, celltypes, points)
    grid.cell_data["region"] = np.ones(grid.n_cells, dtype=np.int32)

    top = n_layers * n_verts
    boundary = {
        "body": BoundaryGroup(
            "body", np.arange(len(tris), dtype=np.int64), tris.copy()
        ),
        "outer": BoundaryGroup(
            "outer",
            np.arange((n_layers - 1) * len(tris), n_layers * len(tris), dtype=np.int64),
            tris + top,
        ),
    }
    quality = volume_quality(grid)
    notes = [
        f"boundary layers: {n_layers} layer(s), first {first_thickness:.6g}, "
        f"growth {growth:g}, total {dists[-1]:.6g}, "
        f"normal smoothing {normal_smoothing} pass(es)",
        "limit: per-cell inversion is counted, but global self-intersection "
        "of far-apart columns is not detected",
    ]
    return WallMesh(
        grid=grid, boundary=boundary, backend="prism", quality=quality, notes=notes
    )

"""Surface repair (V8): eliminate clean-up for triangulated surfaces.

``repair_surface`` fixes what can be fixed mechanically — duplicate
vertices, degenerate/zero-area triangles, exact-duplicate faces, small
holes — and REPORTS what it will not touch: non-manifold edges are never
"fixed" silently (any resolution is a modelling decision).

``degrade_surface`` is the regression counterpart: controlled damage
(vertex noise, poked holes, duplicated faces) for round-trip tests.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pyvista as pv


@dataclass
class RepairReport:
    n_points_before: int
    n_points_after: int
    n_faces_before: int
    n_faces_after: int
    merged_vertices: int
    degenerate_faces_removed: int
    duplicate_faces_removed: int
    holes_filled: int
    holes_left: int
    non_manifold_edges: int
    mesh_ready: bool
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2)

    def to_text(self) -> str:
        lines = [
            f"points      : {self.n_points_before} -> {self.n_points_after} "
            f"({self.merged_vertices} merged)",
            f"faces       : {self.n_faces_before} -> {self.n_faces_after} "
            f"({self.degenerate_faces_removed} degenerate, "
            f"{self.duplicate_faces_removed} duplicate removed)",
            f"holes       : {self.holes_filled} filled, {self.holes_left} left",
            f"non-manifold: {self.non_manifold_edges} edge(s) (never auto-fixed)",
            f"mesh_ready  : {self.mesh_ready}",
        ]
        lines += [f"note        : {n}" for n in self.notes]
        return "\n".join(lines) + "\n"


def _tri_faces(surface: pv.PolyData) -> np.ndarray:
    return surface.faces.reshape(-1, 4)[:, 1:]


def _build(points: np.ndarray, tris: np.ndarray) -> pv.PolyData:
    faces = np.column_stack(
        [np.full(len(tris), 3, dtype=np.int64), tris.astype(np.int64)]
    ).ravel()
    return pv.PolyData(points, faces)


def _boundary_loops(tris: np.ndarray) -> tuple[list[list[int]], list[str]]:
    """Chain directed boundary edges (edges whose reverse is absent) into
    closed vertex loops. Returns (loops, notes for unchainable pieces)."""
    directed = set()
    for a, b, c in tris:
        directed |= {(int(a), int(b)), (int(b), int(c)), (int(c), int(a))}
    boundary = [(a, b) for (a, b) in directed if (b, a) not in directed]
    nxt: dict[int, int] = {}
    notes: list[str] = []
    for a, b in boundary:
        if a in nxt:
            notes.append(
                f"boundary vertex {a} has multiple outgoing boundary edges "
                "(non-manifold rim) — its holes were not filled"
            )
        nxt[a] = b
    loops: list[list[int]] = []
    seen: set[int] = set()
    for a, _ in boundary:
        if a in seen:
            continue
        loop = [a]
        seen.add(a)
        cur = nxt.get(a)
        broken = False
        while cur is not None and cur != a:
            if cur in seen:
                broken = True
                break
            loop.append(cur)
            seen.add(cur)
            cur = nxt.get(cur)
        if cur is None or broken:
            notes.append(f"open boundary chain at vertex {a} could not be closed")
            continue
        loops.append(loop)
    return loops, notes


def repair_surface(
    surface: pv.PolyData,
    *,
    merge_tol: float | None = None,
    max_hole_edges: int = 16,
) -> tuple[pv.PolyData, RepairReport]:
    """Mechanical clean-up; never silently resolves non-manifold topology."""
    surface = surface.triangulate()
    notes: list[str] = []
    n_pts0, n_faces0 = surface.n_points, surface.n_cells

    diag = float(np.linalg.norm(np.asarray(surface.bounds[1::2]) - surface.bounds[::2]))
    if diag <= 0:
        raise ValueError("degenerate surface (zero-size bounding box)")
    if merge_tol is None:
        merge_tol = 1e-9 * diag

    # 1. duplicate-vertex merge (absolute tolerance). vtkCleanPolyData also
    # eats triangles that collapse to repeated vertices — count them as
    # degenerate removals, not silence.
    cleaned = surface.clean(tolerance=merge_tol, absolute=True)
    merged = n_pts0 - cleaned.n_points
    if merged:
        notes.append(f"merged {merged} duplicate vertex/vertices (tol {merge_tol:.4g})")
    points = np.asarray(cleaned.points, dtype=float)
    tris = _tri_faces(cleaned)
    n_degen = n_faces0 - len(tris)

    # 2. remaining degenerate faces: repeated vertex ids or ~zero area.
    a, b, c = points[tris[:, 0]], points[tris[:, 1]], points[tris[:, 2]]
    area2 = np.linalg.norm(np.cross(b - a, c - a), axis=1)
    repeated = (
        (tris[:, 0] == tris[:, 1]) | (tris[:, 1] == tris[:, 2]) | (tris[:, 0] == tris[:, 2])
    )
    degenerate = repeated | (area2 < 1e-14 * diag * diag)
    n_degen += int(degenerate.sum())
    if degenerate.any():
        tris = tris[~degenerate]
    if n_degen:
        notes.append(f"removed {n_degen} degenerate/zero-area triangle(s)")

    # 3. exact-duplicate faces (same vertex set).
    keys = np.sort(tris, axis=1)
    _, first_idx = np.unique(keys, axis=0, return_index=True)
    n_dup = len(tris) - len(first_idx)
    if n_dup:
        notes.append(f"removed {n_dup} duplicate face(s)")
        tris = tris[np.sort(first_idx)]

    # 4. small hole fill — but NEVER around non-manifold topology: a "hole"
    # rim touching an over-shared edge is a modelling problem, and filling
    # it would silently pick a sheet. Detect via undirected edge use counts.
    edges = np.sort(
        np.concatenate([tris[:, [0, 1]], tris[:, [1, 2]], tris[:, [2, 0]]]), axis=1
    )
    _, edge_counts = np.unique(edges, axis=0, return_counts=True)
    n_nonmanifold_edges = int((edge_counts > 2).sum())

    holes_filled = 0
    holes_left = 0
    new_pts: list[np.ndarray] = []
    new_tris: list[list[int]] = []
    if n_nonmanifold_edges:
        loops = []
        notes.append(
            "hole filling skipped: surface has non-manifold edges "
            "(fix those first — see below)"
        )
    else:
        loops, loop_notes = _boundary_loops(tris)
        notes.extend(loop_notes)
    for loop in loops:
        if len(loop) > max_hole_edges:
            holes_left += 1
            notes.append(
                f"hole with {len(loop)} edges left open "
                f"(> max_hole_edges={max_hole_edges})"
            )
            continue
        center_id = len(points) + len(new_pts)
        new_pts.append(points[loop].mean(axis=0))
        # Boundary edges run a->b in face order; fill triangles must carry
        # b->a. The loop follows boundary direction, so (b, a, center).
        for k in range(len(loop)):
            a_v, b_v = loop[k], loop[(k + 1) % len(loop)]
            new_tris.append([b_v, a_v, center_id])
        holes_filled += 1
    if new_pts:
        points = np.vstack([points, np.asarray(new_pts)])
        tris = np.vstack([tris, np.asarray(new_tris, dtype=tris.dtype)])
        notes.append(f"filled {holes_filled} hole(s) (fan + centroid)")

    repaired = _build(points, tris)

    # 5. non-manifold edges: report, never fix.
    if n_nonmanifold_edges:
        notes.append(
            f"{n_nonmanifold_edges} non-manifold edge(s) present — NOT "
            "auto-fixed; resolve upstream (a repair cannot decide which "
            "sheet is right)"
        )

    mesh_ready = (
        repaired.n_open_edges == 0
        and n_nonmanifold_edges == 0
        and repaired.is_manifold
    )
    report = RepairReport(
        n_points_before=n_pts0,
        n_points_after=repaired.n_points,
        n_faces_before=n_faces0,
        n_faces_after=repaired.n_cells,
        merged_vertices=merged,
        degenerate_faces_removed=n_degen,
        duplicate_faces_removed=n_dup,
        holes_filled=holes_filled,
        holes_left=holes_left,
        non_manifold_edges=n_nonmanifold_edges,
        mesh_ready=bool(mesh_ready),
        notes=notes,
    )
    return repaired, report


def degrade_surface(
    surface: pv.PolyData,
    *,
    noise: float = 0.0,
    poke_holes: int = 0,
    duplicate_faces: int = 0,
    seed: int = 0,
) -> pv.PolyData:
    """Controlled damage for repair regression tests (deterministic)."""
    rng = np.random.default_rng(seed)
    surface = surface.triangulate()
    points = np.asarray(surface.points, dtype=float).copy()
    tris = _tri_faces(surface).copy()

    if noise > 0:
        points += rng.normal(0.0, noise, size=points.shape)
    if poke_holes > 0:
        if poke_holes >= len(tris):
            raise ValueError(f"cannot poke {poke_holes} holes into {len(tris)} faces")
        # Spread the holes: pick faces far apart so rims stay disjoint.
        centroids = points[tris].mean(axis=1)
        picked: list[int] = [int(rng.integers(len(tris)))]
        while len(picked) < poke_holes:
            d = np.min(
                np.linalg.norm(centroids[:, None] - centroids[picked][None], axis=2),
                axis=1,
            )
            picked.append(int(np.argmax(d)))
        tris = np.delete(tris, picked, axis=0)
    if duplicate_faces > 0:
        dup = tris[rng.integers(0, len(tris), size=duplicate_faces)]
        tris = np.vstack([tris, dup])
    return _build(points, tris)

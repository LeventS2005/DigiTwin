"""Multi-block structured meshing of rectilinear 2-D profiles (V5).

TrueGrid-style multi-block for profiles whose loops are axis-aligned
(window panes!): global grid lines from the loop coordinates decompose the
plane into rectangular blocks; blocks inside the material get tensor-product
quad meshes (TFI degenerates to the tensor grid on rectangles — projection
onto curved boundaries is V6); shared block edges merge EXACTLY by index
arithmetic because subdivision counts are per grid interval, not per block.
``extrude_blocks`` turns the quad mesh into hexes with named face groups
(front/back/outer_rim/hole_rims) in a WallMesh.

Loop conventions match ``Profile2D``/``extrude_loops``: loops[0] is the
outer boundary CCW, the rest are holes CW. Non-rectilinear profiles are
refused loudly — curved boundaries need the V6 projection method.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pyvista as pv

from meshforge.io.cad import _point_in_loop, _signed_area
from meshforge.meshing.base import BoundaryGroup, WallMesh
from meshforge.quality.volume import volume_quality


@dataclass
class BlockMesh2D:
    """Structured multi-block quad mesh on the global fine lattice.

    The lattice fields (node_id grid, fine ticks, coarse->fine maps, active
    mask, source loops) let the V6 projection method re-blend block
    interiors by TFI after boundary nodes move.
    """

    points: np.ndarray  # (N, 2)
    quads: np.ndarray  # (M, 4) CCW
    block_of: np.ndarray  # (M,) coarse block id per quad
    n_blocks: int
    edge_groups: dict[str, np.ndarray]  # name -> (k, 2) node-id edges
    node_id: np.ndarray  # (nfx, nfy) global node ids, -1 where unused
    first_fx: np.ndarray  # coarse tick k -> fine index of xs[k]
    first_fy: np.ndarray
    active: np.ndarray  # (n_coarse_x, n_coarse_y) material blocks
    loops: list[np.ndarray] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _validate_rectilinear(loops: list[np.ndarray], tol: float) -> None:
    for li, lp in enumerate(loops):
        d = np.roll(lp, -1, axis=0) - lp
        axis_ok = (np.abs(d[:, 0]) <= tol) | (np.abs(d[:, 1]) <= tol)
        if not bool(axis_ok.all()):
            k = int(np.argmin(axis_ok))
            raise ValueError(
                f"loop {li} has a non-axis-aligned edge at vertex {k} "
                f"(delta {d[k][0]:.6g}, {d[k][1]:.6g}) — the multi-block "
                "mesher supports rectilinear profiles only; curved/skewed "
                "boundaries need the V6 projection method"
            )


def _merged_ticks(values: np.ndarray, tol: float) -> np.ndarray:
    """Sorted unique coordinates, merging values within tol."""
    vals = np.sort(np.asarray(values, dtype=float))
    out = [vals[0]]
    for v in vals[1:]:
        if v - out[-1] > tol:
            out.append(v)
    return np.asarray(out)


def select_loops(
    loops: list,
    components: list[dict],
    *,
    outer: int = 0,
    component_holes: bool = True,
    holes_like: str | None = None,
    holes_tol: float = 0.5,
) -> tuple[list[np.ndarray], int]:
    """Assemble a meshable [outer CCW, holes CW...] set from profile data.

    Material selection is explicit (V5-D2): the outer loop of component
    ``outer``, that component's own holes, plus every OTHER component whose
    outer bbox matches ``holes_like`` ("WxH", e.g. "177.03x170.68") within
    ``holes_tol`` — those outers are reversed into holes. Returns
    (selected_loops, n_holes_like_matched).
    """
    if not 0 <= outer < len(components):
        raise ValueError(
            f"outer component {outer} out of range; profile has "
            f"{len(components)} component(s)"
        )
    comp = components[outer]
    selected = [np.asarray(loops[comp["outer"]], dtype=float)]
    if component_holes:
        selected.extend(np.asarray(loops[h], dtype=float) for h in comp["holes"])
    n_matched = 0
    if holes_like:
        try:
            w_s, h_s = str(holes_like).lower().split("x")
            want_w, want_h = float(w_s), float(h_s)
        except ValueError:
            raise ValueError(
                f"holes_like expects WxH (e.g. 177.03x170.68), got {holes_like!r}"
            ) from None
        for ci, c in enumerate(components):
            if ci == outer:
                continue
            lp = np.asarray(loops[c["outer"]], dtype=float)
            lo, hi = lp.min(axis=0), lp.max(axis=0)
            if (
                abs((hi[0] - lo[0]) - want_w) <= holes_tol
                and abs((hi[1] - lo[1]) - want_h) <= holes_tol
            ):
                selected.append(lp[::-1].copy())  # CCW outer -> CW hole
                n_matched += 1
        if n_matched == 0:
            raise ValueError(
                f"holes_like {holes_like!r}: no component matched within "
                f"tolerance {holes_tol}"
            )
    return selected, n_matched


def _graded_ticks(a: float, b: float, n: int, ratio: float) -> np.ndarray:
    """n cells across [a, b] with spacing growing geometrically from BOTH
    ends toward the middle, max/min spacing == ratio (V20 transition
    grading: fine next to every coarse grid line, coarse mid-span).
    Fewer than 3 cells cannot grade symmetrically -> uniform."""
    if n < 3 or ratio == 1.0:
        return np.linspace(a, b, n + 1)
    exponents = np.minimum(np.arange(n), np.arange(n)[::-1])
    r = ratio ** (1.0 / exponents.max())
    w = r ** exponents.astype(float)
    ticks = a + (b - a) * np.concatenate([[0.0], np.cumsum(w)]) / w.sum()
    ticks[-1] = b  # exact endpoint (no cumulative rounding)
    return ticks


def build_block_mesh(
    loops: list[np.ndarray],
    *,
    target_size: float,
    tol: float | None = None,
    grade_ratio: float = 1.0,
) -> BlockMesh2D:
    """Decompose a rectilinear profile into blocks and quad-mesh them.

    ``loops[0]`` outer CCW, rest holes CW (Profile2D convention).
    ``target_size`` is the maximum element edge length; each grid interval
    is subdivided ceil(width/target_size) times GLOBALLY, so neighbouring
    blocks share identical edge nodes by construction. ``grade_ratio > 1``
    grades each interval geometrically from both ends toward the middle
    (V20): counts stay global, so conformity is untouched.
    """
    if not target_size > 0:
        raise ValueError(f"target_size must be positive, got {target_size}")
    if not grade_ratio >= 1.0:
        raise ValueError(
            f"grade_ratio must be >= 1 (max/min spacing within an interval), "
            f"got {grade_ratio}"
        )
    loops = [np.asarray(lp, dtype=float) for lp in loops]
    if not loops:
        raise ValueError("need at least one loop")
    if _signed_area(loops[0]) <= 0:
        raise ValueError("outer loop must be CCW (positive signed area)")
    for i, lp in enumerate(loops[1:], start=1):
        if _signed_area(lp) >= 0:
            raise ValueError(f"hole loop {i} must be CW (negative signed area)")

    allpts = np.vstack(loops)
    lo, hi = allpts.min(axis=0), allpts.max(axis=0)
    diag = float(np.hypot(*(hi - lo)))
    if diag <= 0:
        raise ValueError("degenerate profile (zero-size bounding box)")
    if tol is None:
        tol = 1e-6 * diag

    _validate_rectilinear(loops, tol)

    xs = _merged_ticks(allpts[:, 0], tol)
    ys = _merged_ticks(allpts[:, 1], tol)
    if len(xs) < 2 or len(ys) < 2:
        raise ValueError("profile collapses to a line on the grid")

    # Coarse block (i, j) is active iff its center is material: inside the
    # outer loop and outside every hole (even-odd).
    cx = (xs[:-1] + xs[1:]) / 2.0
    cy = (ys[:-1] + ys[1:]) / 2.0
    active = np.zeros((len(cx), len(cy)), dtype=bool)
    for i, x in enumerate(cx):
        for j, y in enumerate(cy):
            p = np.array([x, y])
            if not _point_in_loop(p, loops[0]):
                continue
            if any(_point_in_loop(p, h) for h in loops[1:]):
                continue
            active[i, j] = True
    if not active.any():
        raise ValueError("no material blocks found — holes swallow the profile?")

    # Global fine ticks: per-interval subdivision counts are global, so
    # neighbouring blocks share identical boundary nodes (exact index
    # arithmetic, no tolerance merge).
    def refine(ticks: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        fine = [ticks[0]]
        first_fine = np.zeros(len(ticks), dtype=np.int64)
        for k in range(len(ticks) - 1):
            n = max(1, int(np.ceil((ticks[k + 1] - ticks[k]) / target_size)))
            fine.extend(_graded_ticks(ticks[k], ticks[k + 1], n, grade_ratio)[1:])
            first_fine[k + 1] = len(fine) - 1
        return np.asarray(fine), first_fine

    fx, first_fx = refine(xs)
    fy, first_fy = refine(ys)
    nfx, nfy = len(fx), len(fy)

    # Fine cell (p, q) belongs to coarse block (i, j); keep material cells.
    block_i = np.searchsorted(first_fx, np.arange(nfx - 1), side="right") - 1
    block_j = np.searchsorted(first_fy, np.arange(nfy - 1), side="right") - 1
    keep = active[np.ix_(block_i, block_j)]  # (nfx-1, nfy-1)

    node_used = np.zeros((nfx, nfy), dtype=bool)
    kp, kq = np.nonzero(keep)
    for dp in (0, 1):
        for dq in (0, 1):
            node_used[kp + dp, kq + dq] = True
    node_id = np.full((nfx, nfy), -1, dtype=np.int64)
    node_id[node_used] = np.arange(int(node_used.sum()))
    gx, gy = np.meshgrid(fx, fy, indexing="ij")
    points = np.column_stack([gx[node_used], gy[node_used]])

    quads = np.column_stack(
        [
            node_id[kp, kq],
            node_id[kp + 1, kq],
            node_id[kp + 1, kq + 1],
            node_id[kp, kq + 1],
        ]
    )  # CCW in the plane
    block_lin = block_i[kp] * len(cy) + block_j[kq]
    _, block_of = np.unique(block_lin, return_inverse=True)
    n_blocks = int(block_of.max()) + 1

    # Boundary edges: used by exactly one quad; classified by which loop
    # they lie on (outer -> outer_rim, any hole -> hole_rims).
    edge_count: dict[tuple[int, int], int] = {}
    for quad in quads:
        for e in range(4):
            a, b = int(quad[e]), int(quad[(e + 1) % 4])
            key = (min(a, b), max(a, b))
            edge_count[key] = edge_count.get(key, 0) + 1
    boundary_edges = np.array(
        [k for k, c in edge_count.items() if c == 1], dtype=np.int64
    ).reshape(-1, 2)

    def on_loop(mid: np.ndarray, lp: np.ndarray) -> bool:
        a = lp
        b = np.roll(lp, -1, axis=0)
        ab = b - a
        denom = np.einsum("ij,ij->i", ab, ab)
        denom = np.where(denom == 0.0, 1.0, denom)
        t = np.clip(np.einsum("ij,ij->i", mid - a, ab) / denom, 0.0, 1.0)
        proj = a + t[:, None] * ab
        return bool((np.linalg.norm(mid - proj, axis=1) <= tol).any())

    outer_rim, hole_rims = [], []
    for a, b in boundary_edges:
        mid = (points[a] + points[b]) / 2.0
        if on_loop(mid, loops[0]):
            outer_rim.append((a, b))
        elif any(on_loop(mid, h) for h in loops[1:]):
            hole_rims.append((a, b))
        else:
            raise ValueError(
                f"boundary edge at ({mid[0]:.6g}, {mid[1]:.6g}) lies on no "
                "profile loop — block classification is inconsistent"
            )

    notes = [
        f"blocks {n_blocks} active of {len(cx)}x{len(cy)} coarse grid, "
        f"{len(quads)} quads, {len(points)} nodes, target_size {target_size:.6g}"
    ]
    if grade_ratio > 1.0:
        notes.append(
            f"transition grading: max/min spacing {grade_ratio:g} within "
            f"each interval (fine cells at the coarse grid lines)"
        )
    return BlockMesh2D(
        points=points,
        quads=quads,
        block_of=block_of,
        n_blocks=n_blocks,
        edge_groups={
            "outer_rim": np.asarray(outer_rim, dtype=np.int64).reshape(-1, 2),
            "hole_rims": np.asarray(hole_rims, dtype=np.int64).reshape(-1, 2),
        },
        node_id=node_id,
        first_fx=first_fx,
        first_fy=first_fy,
        active=active,
        loops=loops,
        notes=notes,
    )


def extrude_blocks(
    bm: BlockMesh2D,
    *,
    thickness: float,
    n_layers: int = 1,
) -> WallMesh:
    """Extrude the block quad mesh to hexahedra along +z with named face
    groups front (z=0), back (z=thickness), outer_rim, hole_rims."""
    if not thickness > 0:
        raise ValueError(f"thickness must be positive, got {thickness}")
    if n_layers < 1:
        raise ValueError(f"n_layers must be >= 1, got {n_layers}")

    n2d = len(bm.points)
    zs = np.linspace(0.0, thickness, n_layers + 1)
    points = np.column_stack(
        [
            np.tile(bm.points[:, 0], n_layers + 1),
            np.tile(bm.points[:, 1], n_layers + 1),
            np.repeat(zs, n2d),
        ]
    )

    n_quads = len(bm.quads)
    hexes = np.empty((n_quads * n_layers, 8), dtype=np.int64)
    for k in range(n_layers):
        base = bm.quads + k * n2d
        top = bm.quads + (k + 1) * n2d
        hexes[k * n_quads : (k + 1) * n_quads] = np.hstack([base, top])

    cells = np.column_stack(
        [np.full(len(hexes), 8, dtype=np.int64), hexes]
    ).ravel()
    celltypes = np.full(len(hexes), pv.CellType.HEXAHEDRON, dtype=np.uint8)
    grid = pv.UnstructuredGrid(cells, celltypes, points)
    grid.cell_data["region"] = np.ones(grid.n_cells, dtype=np.int32)

    # Face groups. Cell ids: layer k block q -> k*n_quads + q.
    front = BoundaryGroup(
        name="front",
        cell_ids=np.arange(n_quads, dtype=np.int64),
        face_nodes=bm.quads[:, ::-1].copy(),  # outward -z
    )
    back = BoundaryGroup(
        name="back",
        cell_ids=np.arange((n_layers - 1) * n_quads, n_layers * n_quads, dtype=np.int64),
        face_nodes=(bm.quads + n_layers * n2d).copy(),  # outward +z
    )

    # Rim faces: boundary edge (a, b) of quad q extrudes to one quad face
    # per layer. Find each edge's parent quad for the cell id.
    edge_owner: dict[tuple[int, int], int] = {}
    for qi, quad in enumerate(bm.quads):
        for e in range(4):
            a, b = int(quad[e]), int(quad[(e + 1) % 4])
            edge_owner[(min(a, b), max(a, b))] = qi

    def rim_group(name: str, edges: np.ndarray) -> BoundaryGroup:
        cell_ids, face_nodes = [], []
        for a, b in edges:
            qi = edge_owner[(min(int(a), int(b)), max(int(a), int(b)))]
            for k in range(n_layers):
                cell_ids.append(k * n_quads + qi)
                face_nodes.append(
                    [
                        int(a) + k * n2d,
                        int(b) + k * n2d,
                        int(b) + (k + 1) * n2d,
                        int(a) + (k + 1) * n2d,
                    ]
                )
        return BoundaryGroup(
            name=name,
            cell_ids=np.asarray(cell_ids, dtype=np.int64),
            face_nodes=np.asarray(face_nodes, dtype=np.int64).reshape(-1, 4),
        )

    boundary = {
        "front": front,
        "back": back,
        "outer_rim": rim_group("outer_rim", bm.edge_groups["outer_rim"]),
        "hole_rims": rim_group("hole_rims", bm.edge_groups["hole_rims"]),
    }

    quality = volume_quality(grid)
    notes = list(bm.notes) + [
        f"extruded {n_layers} layer(s) to thickness {thickness:.6g}"
    ]
    return WallMesh(
        grid=grid, boundary=boundary, backend="blocks", quality=quality, notes=notes
    )

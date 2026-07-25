"""The projection method (V6): block meshes conforming to curved profiles.

TrueGrid's signature move: build a structured multi-block mesh on a
rectilinear PROXY of the profile (curved loops replaced by their bounding
rectangles), snap the proxy-rim nodes onto the true curves (closest point),
capture feature corners exactly (tangent discontinuities sharper than the
angle knob attract their nearest rim node), then re-blend every block
interior by transfinite interpolation from the updated edges. A quality
gate (0 inverted quads + min scaled-Jacobian floor) refuses bad results
loudly instead of shipping them.
"""
from __future__ import annotations

import math

import numpy as np

from meshforge.io.cad import _signed_area
from meshforge.meshing.blocks import BlockMesh2D, _validate_rectilinear, build_block_mesh


def _is_rectilinear(loop: np.ndarray, tol: float) -> bool:
    d = np.roll(loop, -1, axis=0) - loop
    return bool(((np.abs(d[:, 0]) <= tol) | (np.abs(d[:, 1]) <= tol)).all())


def _bbox_proxy(loop: np.ndarray, hole: bool) -> np.ndarray:
    lo, hi = loop.min(axis=0), loop.max(axis=0)
    rect = np.array(
        [[lo[0], lo[1]], [hi[0], lo[1]], [hi[0], hi[1]], [lo[0], hi[1]]]
    )
    return rect[::-1].copy() if hole else rect  # holes CW, outers CCW


def _closest_on_polyline(p: np.ndarray, loop: np.ndarray) -> np.ndarray:
    a = loop
    b = np.roll(loop, -1, axis=0)
    ab = b - a
    denom = np.einsum("ij,ij->i", ab, ab)
    denom = np.where(denom == 0.0, 1.0, denom)
    t = np.clip(np.einsum("ij,ij->i", p - a, ab) / denom, 0.0, 1.0)
    proj = a + t[:, None] * ab
    k = int(np.argmin(np.linalg.norm(p - proj, axis=1)))
    return proj[k]


def _corner_vertices(loop: np.ndarray, angle_deg: float) -> np.ndarray:
    """Vertices where the boundary tangent turns by more than angle_deg."""
    d_in = loop - np.roll(loop, 1, axis=0)
    d_out = np.roll(loop, -1, axis=0) - loop
    n_in = np.linalg.norm(d_in, axis=1)
    n_out = np.linalg.norm(d_out, axis=1)
    ok = (n_in > 0) & (n_out > 0)
    cosang = np.ones(len(loop))
    cosang[ok] = np.einsum("ij,ij->i", d_in[ok], d_out[ok]) / (n_in[ok] * n_out[ok])
    return loop[np.cos(math.radians(angle_deg)) > cosang]


def _on_polyline_mask(pts: np.ndarray, loop: np.ndarray, tol: float) -> np.ndarray:
    """Boolean mask of pts lying within tol of the closed polyline."""
    a = loop
    b = np.roll(loop, -1, axis=0)
    ab = b - a
    denom = np.einsum("ij,ij->i", ab, ab)
    denom = np.where(denom == 0.0, 1.0, denom)
    mask = np.zeros(len(pts), dtype=bool)
    for i, p in enumerate(pts):
        t = np.clip(np.einsum("ij,ij->i", p - a, ab) / denom, 0.0, 1.0)
        proj = a + t[:, None] * ab
        mask[i] = bool((np.linalg.norm(p - proj, axis=1) <= tol).any())
    return mask


def _relinearize_block_edges(
    bm: BlockMesh2D, pts: np.ndarray, projected: np.ndarray
) -> None:
    """Straighten every block edge that is NOT a projected rim between its
    (possibly moved) endpoint corners (in place).

    When a proxy corner snaps diagonally onto the curve, the two block edges
    leaving it would otherwise keep their old interior nodes — a kink that
    both neighbouring blocks inherit. Projected rim edges (all nodes
    snapped) are left exactly on the curve. Idempotent on shared edges.
    """

    def fix_line(ids: np.ndarray) -> None:
        if len(ids) < 3 or (ids < 0).any():
            return
        interior = ids[1:-1]
        if projected[interior].all():
            return  # rim edge: stays on the curve
        frac = np.linspace(0.0, 1.0, len(ids))[1:-1, None]
        pts[interior] = pts[ids[0]] + frac * (pts[ids[-1]] - pts[ids[0]])

    n_cx, n_cy = bm.active.shape
    for i in range(n_cx):
        for j in range(n_cy):
            if not bm.active[i, j]:
                continue
            p0, p1 = int(bm.first_fx[i]), int(bm.first_fx[i + 1])
            q0, q1 = int(bm.first_fy[j]), int(bm.first_fy[j + 1])
            fix_line(bm.node_id[p0 : p1 + 1, q0])  # south
            fix_line(bm.node_id[p0 : p1 + 1, q1])  # north
            fix_line(bm.node_id[p0, q0 : q1 + 1])  # west
            fix_line(bm.node_id[p1, q0 : q1 + 1])  # east


def _tfi_reblend(bm: BlockMesh2D, pts: np.ndarray) -> None:
    """Recompute every active block's interior nodes by transfinite
    interpolation from the block's current edge positions (in place).
    On unmoved straight edges TFI reproduces the tensor grid exactly."""
    n_cx, n_cy = bm.active.shape
    for i in range(n_cx):
        for j in range(n_cy):
            if not bm.active[i, j]:
                continue
            p0, p1 = int(bm.first_fx[i]), int(bm.first_fx[i + 1])
            q0, q1 = int(bm.first_fy[j]), int(bm.first_fy[j + 1])
            ids = bm.node_id[p0 : p1 + 1, q0 : q1 + 1]
            if (ids < 0).any():  # pragma: no cover - active blocks are full
                raise ValueError("active block has missing lattice nodes")
            nu, nv = ids.shape
            if nu < 3 or nv < 3:
                continue  # no interior nodes to blend
            grid_pts = pts[ids]  # (nu, nv, 2)
            u = np.linspace(0.0, 1.0, nu)[:, None, None]
            v = np.linspace(0.0, 1.0, nv)[None, :, None]
            e_w = grid_pts[0][None, :, :]  # u = 0 edge
            e_e = grid_pts[-1][None, :, :]  # u = 1 edge
            e_s = grid_pts[:, 0][:, None, :]  # v = 0 edge
            e_n = grid_pts[:, -1][:, None, :]  # v = 1 edge
            c_sw, c_se = grid_pts[0, 0], grid_pts[-1, 0]
            c_nw, c_ne = grid_pts[0, -1], grid_pts[-1, -1]
            tfi = (
                (1 - u) * e_w
                + u * e_e
                + (1 - v) * e_s
                + v * e_n
                - (
                    (1 - u) * (1 - v) * c_sw
                    + u * (1 - v) * c_se
                    + (1 - u) * v * c_nw
                    + u * v * c_ne
                )
            )
            pts[ids[1:-1, 1:-1]] = tfi[1:-1, 1:-1]


def quad_scaled_jacobians(points: np.ndarray, quads: np.ndarray) -> np.ndarray:
    """Per-quad min corner scaled Jacobian (2-D); negative = inverted."""
    p = points[quads]  # (M, 4, 2)
    sj = np.full(len(quads), np.inf)
    for k in range(4):
        e1 = p[:, (k + 1) % 4] - p[:, k]
        e2 = p[:, (k - 1) % 4] - p[:, k]
        cross = e1[:, 0] * e2[:, 1] - e1[:, 1] * e2[:, 0]
        n1 = np.linalg.norm(e1, axis=1)
        n2 = np.linalg.norm(e2, axis=1)
        denom = np.where((n1 > 0) & (n2 > 0), n1 * n2, 1.0)
        corner = np.where((n1 > 0) & (n2 > 0), cross / denom, -1.0)
        sj = np.minimum(sj, corner)
    return sj


def project_profile(
    loops: list[np.ndarray],
    *,
    target_size: float,
    tol: float | None = None,
    corner_angle_deg: float = 30.0,
    min_sj: float = 0.05,
    grade_ratio: float = 1.0,
) -> BlockMesh2D:
    """Block-mesh a profile whose loops may be curved/skewed.

    Rectilinear loops go straight to the multi-block mesher; every other
    loop is replaced by its bounding rectangle for decomposition, then its
    rim nodes are projected onto the true curve (feature corners captured
    exactly) and block interiors are re-blended by TFI. Raises loudly when
    the projected mesh has inverted quads or min scaled Jacobian < min_sj.
    """
    loops = [np.asarray(lp, dtype=float) for lp in loops]
    if not loops:
        raise ValueError("need at least one loop")
    if _signed_area(loops[0]) <= 0:
        raise ValueError("outer loop must be CCW (positive signed area)")
    for i, lp in enumerate(loops[1:], start=1):
        if _signed_area(lp) >= 0:
            raise ValueError(f"hole loop {i} must be CW (negative signed area)")

    allpts = np.vstack(loops)
    diag = float(np.hypot(*(allpts.max(axis=0) - allpts.min(axis=0))))
    if tol is None:
        tol = 1e-6 * diag

    rectilinear = [_is_rectilinear(lp, tol) for lp in loops]
    proxies = [
        lp if rect else _bbox_proxy(lp, hole=(li > 0))
        for li, (lp, rect) in enumerate(zip(loops, rectilinear))
    ]
    _validate_rectilinear(proxies, tol)  # bbox proxies are rectilinear by construction

    bm = build_block_mesh(
        proxies, target_size=target_size, tol=tol, grade_ratio=grade_ratio
    )
    pts = bm.points.copy()

    n_projected = 0
    n_corners = 0
    projected = np.zeros(len(pts), dtype=bool)
    for li, (lp, rect, proxy) in enumerate(zip(loops, rectilinear, proxies)):
        if rect:
            continue
        rim = np.nonzero(_on_polyline_mask(pts, proxy, tol))[0]
        if len(rim) == 0:
            raise ValueError(
                f"loop {li}: no mesh nodes found on its proxy rectangle — "
                "projection cannot attach"
            )
        for n in rim:
            pts[n] = _closest_on_polyline(pts[n], lp)
        projected[rim] = True
        n_projected += len(rim)
        # Feature corners: each grabs its nearest rim node exactly.
        for corner in _corner_vertices(lp, corner_angle_deg):
            n = rim[int(np.argmin(np.linalg.norm(pts[rim] - corner, axis=1)))]
            pts[n] = corner
            n_corners += 1

    _relinearize_block_edges(bm, pts, projected)
    _tfi_reblend(bm, pts)

    sj = quad_scaled_jacobians(pts, bm.quads)
    n_inverted = int((sj <= 0).sum())
    if n_inverted:
        raise ValueError(
            f"projection produced {n_inverted} inverted quad(s) "
            f"(worst sj {float(sj.min()):.4g}); refine target_size or fix the profile"
        )
    if float(sj.min()) < min_sj:
        raise ValueError(
            f"projected mesh quality below floor: min scaled Jacobian "
            f"{float(sj.min()):.4g} < {min_sj}; refine target_size"
        )

    bm.points = pts
    bm.loops = loops  # downstream sees the TRUE profile, not the proxies
    n_curved = sum(1 for r in rectilinear if not r)
    if n_curved:
        bm.notes.append(
            f"projection: {n_projected} rim node(s) snapped onto "
            f"{n_curved} curved loop(s), "
            f"{n_corners} corner(s) captured, sj_min {float(sj.min()):.4g}"
        )
    return bm

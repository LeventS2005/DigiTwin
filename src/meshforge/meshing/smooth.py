"""Elliptic (Winslow) smoothing for block-mesh fine lattices (V17).

Damped-Jacobi iteration of the Winslow equations in lattice index space:

    alpha * x_xixi - 2 * beta * x_xieta + gamma * x_etaeta = 0
    alpha = x_eta . x_eta,  beta = x_xi . x_eta,  gamma = x_xi . x_xi

Only nodes whose full 8-neighbour stencil exists in the lattice move; rim
nodes (outer loop, hole rims — V6-projected ones included) always miss a
neighbour, so every boundary stays exactly fixed and the enclosed area is
preserved exactly. Block interfaces are ordinary interior fine nodes and
smooth across. Winslow equidistributes interior spacing by design — on
non-uniform tick spacing nodes drift toward uniformity inside the fixed
boundary frame (the TrueGrid-style behaviour, documented in V17_SPEC).
"""
from __future__ import annotations

from dataclasses import replace

import numpy as np

from meshforge.meshing.blocks import BlockMesh2D


def smooth_lattice(
    bm: BlockMesh2D,
    *,
    max_iter: int = 500,
    tol: float = 1e-3,
    omega: float = 0.8,
    guard_quality: bool = True,
    guard_passes: int = 5,
) -> BlockMesh2D:
    """Winslow-smooth the fine lattice; returns a new BlockMesh2D.

    ``guard_quality`` (V21, default ON) makes smoothing DO-NO-HARM: after
    convergence, any moved node whose incident min scaled Jacobian fell
    below its pre-smoothing baseline is blended back toward its original
    position (t in {0, 1/4, 1/2, 3/4} maximising the local min-sj), for up
    to ``guard_passes`` passes; if a global regression still remains, the
    whole smoothing is reverted with a loud note. This removes the V17-D1
    trade (plain Winslow lowered peak sj beside snapped curved rims);
    ``guard_quality=False`` reproduces the V17 behaviour exactly.
    """
    if not max_iter > 0:
        raise ValueError(f"max_iter must be positive, got {max_iter}")
    if not tol > 0:
        raise ValueError(f"tol must be positive, got {tol}")
    if not 0 < omega <= 1:
        raise ValueError(f"omega must be in (0, 1], got {omega}")
    if guard_passes < 0:
        raise ValueError(f"guard_passes must be >= 0, got {guard_passes}")
    if bm.node_id.size == 0:
        raise ValueError(
            "block mesh carries no global rectilinear lattice (O-grid "
            "layouts are quality-by-construction, V26) — elliptic "
            "smoothing needs a V5 lattice mesh"
        )

    node_id = bm.node_id
    present = node_id >= 0
    # Smoothable: all 8 stencil neighbours present (5-point + corners for
    # the cross-derivative). Boundary/rim nodes miss one -> stay fixed.
    ok = np.zeros_like(present)
    ok[1:-1, 1:-1] = (
        present[1:-1, 1:-1]
        & present[:-2, 1:-1] & present[2:, 1:-1]
        & present[1:-1, :-2] & present[1:-1, 2:]
        & present[:-2, :-2] & present[2:, :-2]
        & present[:-2, 2:] & present[2:, 2:]
    )
    ii, jj = np.nonzero(ok)
    points = np.array(bm.points, dtype=float)  # copy — bm stays untouched
    if len(ii) == 0:
        return replace(
            bm,
            points=points,
            notes=[*bm.notes, "elliptic smoothing: no interior nodes to move"],
        )

    def gather(di: int, dj: int) -> np.ndarray:
        return points[node_id[ii + di, jj + dj]]

    center_ids = node_id[ii, jj]
    # Mean fine spacing for the convergence scale.
    h = float(
        np.mean(np.linalg.norm(gather(1, 0) - gather(-1, 0), axis=1) / 2.0)
    )
    if not h > 0:
        raise ValueError("degenerate lattice: zero mean spacing")

    converged_at = None
    for it in range(1, max_iter + 1):
        e, w = gather(1, 0), gather(-1, 0)
        n, s = gather(0, 1), gather(0, -1)
        ne, nw = gather(1, 1), gather(-1, 1)
        se, sw = gather(1, -1), gather(-1, -1)
        x_xi = (e - w) / 2.0
        x_eta = (n - s) / 2.0
        alpha = np.einsum("ij,ij->i", x_eta, x_eta)
        beta = np.einsum("ij,ij->i", x_xi, x_eta)
        gamma = np.einsum("ij,ij->i", x_xi, x_xi)
        denom = 2.0 * (alpha + gamma)
        # A collapsed stencil would divide by ~0 — guard loudly.
        if np.any(denom < 1e-30):
            raise ValueError(
                "elliptic smoothing hit a collapsed stencil (zero metric); "
                "the input lattice is degenerate"
            )
        target = (
            alpha[:, None] * (e + w)
            + gamma[:, None] * (n + s)
            - 0.5 * beta[:, None] * (ne - nw - se + sw)
        ) / denom[:, None]
        step = omega * (target - points[center_ids])
        points[center_ids] += step
        residual = float(np.max(np.linalg.norm(step, axis=1))) / h
        if residual < tol:
            converged_at = it
            break

    notes = list(bm.notes)
    from meshforge.meshing.project import quad_scaled_jacobians

    if guard_quality:
        sj_base = quad_scaled_jacobians(bm.points, bm.quads)
        node_quads: dict[int, list[int]] = {}
        for qi, quad in enumerate(bm.quads):
            for nid in quad:
                node_quads.setdefault(int(nid), []).append(qi)
        smoothed = points.copy()  # blend anchor: the converged Winslow state
        orig = np.asarray(bm.points)
        guarded: set[int] = set()
        moved = [int(c) for c in center_ids]
        for _pass in range(guard_passes):
            changed = False
            for nid in moved:
                qs = node_quads.get(nid, [])
                if not qs:
                    continue
                local = bm.quads[qs]
                base = float(sj_base[qs].min())
                entry = points[nid].copy()
                best_val = float(quad_scaled_jacobians(points, local).min())
                if best_val >= base - 1e-12:
                    continue
                best_pos = entry
                for t in (0.75, 0.5, 0.25, 0.0):
                    points[nid] = (1.0 - t) * orig[nid] + t * smoothed[nid]
                    val = float(quad_scaled_jacobians(points, local).min())
                    if val > best_val + 1e-15:
                        best_val, best_pos = val, points[nid].copy()
                points[nid] = best_pos
                if not np.array_equal(best_pos, entry):
                    guarded.add(nid)
                    changed = True
            if not changed:
                break
        if float(quad_scaled_jacobians(points, bm.quads).min()) < float(
            sj_base.min()
        ) - 1e-9:
            points = np.array(bm.points)
            notes.append(
                "quality guard: smoothing fully REVERTED — no improvement "
                "achievable without local sj regression"
            )
        elif guarded:
            notes.append(
                f"quality guard: {len(guarded)} node(s) blended back toward "
                f"their input positions (no local sj regression allowed)"
            )

    if converged_at is None:
        notes.append(
            f"elliptic smoothing did NOT converge in {max_iter} iterations "
            f"(residual {residual:.3g} > tol {tol:g}) — result kept, verify "
            f"quality downstream"
        )
    else:
        notes.append(
            f"elliptic smoothing converged in {converged_at} iteration(s) "
            f"(tol {tol:g})"
        )

    # Never hand a folded lattice downstream (rule 6). Both corner-split
    # triangles of every CCW quad must stay positively oriented — the total
    # signed area of a folded quad can still be positive.
    q = points[bm.quads]

    def _cross(a: int, b: int, c: int) -> np.ndarray:
        return (q[:, b, 0] - q[:, a, 0]) * (q[:, c, 1] - q[:, a, 1]) - (
            q[:, c, 0] - q[:, a, 0]
        ) * (q[:, b, 1] - q[:, a, 1])

    folded = (_cross(0, 1, 2) <= 0) | (_cross(0, 2, 3) <= 0)
    if bool(folded.any()):
        raise ValueError(
            f"elliptic smoothing folded {int(folded.sum())} quad(s) — "
            f"refusing the smoothed lattice"
        )

    # Report the quality delta openly. With guard_quality (default) sj_min
    # can never regress; without it, plain Winslow measurably lowered peak
    # sj beside snapped curved rims (0.835 -> 0.681, V17-D1).
    sj_before = float(quad_scaled_jacobians(bm.points, bm.quads).min())
    sj_after = float(quad_scaled_jacobians(points, bm.quads).min())
    notes.append(f"elliptic smoothing: sj_min {sj_before:.4g} -> {sj_after:.4g}")
    return replace(bm, points=points, notes=notes)

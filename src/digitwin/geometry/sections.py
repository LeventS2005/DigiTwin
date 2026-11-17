from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pyvista as pv


@dataclass
class Section:
    """Planar cross-section at one centerline station."""

    index: int
    s: float  # arc length along the centerline
    center: np.ndarray  # (3,) centroid of the section loop
    normal: np.ndarray  # (3,) slicing plane normal (centerline tangent)
    area: float
    d_equiv: float  # 2*sqrt(A/pi)
    d_min: float
    d_max: float


@dataclass
class Constriction:
    index: int
    s: float
    area: float
    area_ratio: float  # area / local baseline (1.0 = no constriction)


def _loop_metrics(points_2d: np.ndarray) -> tuple[float, float, float]:
    """Area + min/max opposite-sum diameters of an unordered planar loop.

    Tube sections are star-shaped around their centroid, so angle-sorting is
    a valid ordering for the shoelace formula; diameters come from radius
    sums of opposite angle bins (documented approximation, exact for
    near-elliptic sections at these bin counts).
    """
    c = points_2d.mean(axis=0)
    rel = points_2d - c
    ang = np.arctan2(rel[:, 1], rel[:, 0])
    order = np.argsort(ang)
    p = rel[order]
    x, y = p[:, 0], p[:, 1]
    area = 0.5 * abs(np.sum(x * np.roll(y, -1) - np.roll(x, -1) * y))

    r = np.linalg.norm(rel, axis=1)
    n_bins = 36
    bins = ((ang + np.pi) / (2 * np.pi) * n_bins).astype(int) % n_bins
    r_bin = np.full(n_bins, np.nan)
    for bin_id in range(n_bins):
        sel = bins == bin_id
        if sel.any():
            r_bin[bin_id] = r[sel].mean()
    half = n_bins // 2
    diam = r_bin[:half] + r_bin[half:]
    diam = diam[np.isfinite(diam)]
    if len(diam) == 0:
        return float(area), float("nan"), float("nan")
    return float(area), float(diam.min()), float(diam.max())


def compute_sections(
    surface: pv.PolyData,
    centerline,
    *,
    stations: np.ndarray | list[int] | None = None,
) -> list[Section]:
    """Cross-sections perpendicular to the centerline tangent.

    ``stations``: centerline sample indices (default: all). Each slice keeps
    only the connected component closest to the station point, so a bent
    tube doesn't pick up the far side of the lumen.
    """
    idx = np.arange(len(centerline.points)) if stations is None else np.asarray(stations)
    sections: list[Section] = []
    for i in idx:
        origin = centerline.points[i]
        normal = centerline.tangents[i]
        cut = surface.slice(normal=normal, origin=origin)
        if cut.n_points == 0:
            continue  # station outside the surface (e.g. cap tangency)
        comp = cut.connectivity(extraction_mode="closest", closest_point=origin)
        pts = comp.points
        if len(pts) < 8:
            continue  # degenerate sliver (cap graze)

        # In-plane coordinates.
        seed = np.eye(3)[np.argmin(np.abs(normal))]
        e1 = seed - np.dot(seed, normal) * normal
        e1 /= np.linalg.norm(e1)
        e2 = np.cross(normal, e1)
        rel = pts - origin
        p2 = np.column_stack([rel @ e1, rel @ e2])

        area, d_min, d_max = _loop_metrics(p2)
        center = pts.mean(axis=0)
        sections.append(
            Section(
                index=int(i),
                s=float(centerline.arc_lengths[i]),
                center=center,
                normal=normal.copy(),
                area=area,
                d_equiv=2.0 * float(np.sqrt(area / np.pi)),
                d_min=d_min,
                d_max=d_max,
            )
        )
    if not sections:
        raise ValueError("no valid cross-sections; centerline may not intersect the surface")
    return sections


def detect_constrictions(
    sections: list[Section],
    *,
    rel_drop: float = 0.2,
) -> list[Constriction]:
    """Local area minima at least ``rel_drop`` below the reference envelope.

    Baseline = min(running max from the left, running max from the right) —
    the stenosis-style proximal/distal reference. Unlike a rolling median it
    cannot follow a wide dip down, and a monotone taper yields ratio 1
    everywhere (no false positives).
    """
    if not 0.0 < rel_drop < 1.0:
        raise ValueError(f"rel_drop must be in (0,1), got {rel_drop}")
    areas = np.array([s.area for s in sections])
    n = len(areas)
    runmax_left = np.maximum.accumulate(areas)
    runmax_right = np.maximum.accumulate(areas[::-1])[::-1]
    baseline = np.minimum(runmax_left, runmax_right)
    ratio = areas / baseline

    found: list[Constriction] = []
    for i in range(1, n - 1):
        if ratio[i] <= 1.0 - rel_drop and areas[i] <= areas[i - 1] and areas[i] <= areas[i + 1]:
            found.append(
                Constriction(
                    index=sections[i].index,
                    s=sections[i].s,
                    area=float(areas[i]),
                    area_ratio=float(ratio[i]),
                )
            )
    # Collapse plateaus: keep the deepest per contiguous run.
    deduped: list[Constriction] = []
    for c in found:
        if deduped and abs(c.s - deduped[-1].s) < (sections[1].s - sections[0].s) * 2:
            if c.area_ratio < deduped[-1].area_ratio:
                deduped[-1] = c
        else:
            deduped.append(c)
    return deduped

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import networkx as nx
import numpy as np
import pyvista as pv
from scipy import ndimage
from scipy.interpolate import interp1d, splev, splprep

MAX_VOXELS = 40_000_000  # refuse silently absurd grids; ~0.3 GB of masks


@dataclass
class Centerline:
    """Uniform-arc-length centerline with EDT-based inscribed radii."""

    points: np.ndarray  # (N, 3)
    tangents: np.ndarray  # (N, 3) unit
    arc_lengths: np.ndarray  # (N,) 0..length
    radii: np.ndarray  # (N,) maximal inscribed sphere radius (EDT)
    length: float
    closed: bool
    voxel_size: float
    notes: list[str] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        return {
            "n_samples": len(self.points),
            "length": self.length,
            "closed": self.closed,
            "voxel_size": self.voxel_size,
            "radius_min": float(self.radii.min()),
            "radius_mean": float(self.radii.mean()),
            "radius_max": float(self.radii.max()),
            "notes": self.notes,
        }


# Field-data keys used to ride a validated centerline along with a surface
# (V40): so a centerline chosen at import time survives a .vtp save/load and is
# reused by the wall/volmesh step instead of recomputing a (possibly worse) one.
_CL_POINTS = "meshforge_centerline_points"
_CL_RADII = "meshforge_centerline_radii"
_CL_CLOSED = "meshforge_centerline_closed"


def embed_centerline(surface: pv.PolyData, cl: "Centerline") -> pv.PolyData:
    """Attach ``cl`` to ``surface`` as field data (survives .vtp round-trips).

    Only the points, radii and the closed flag are stored; tangents/arc lengths
    are re-derived on read.  Mutates and returns the surface."""
    pts = np.asarray(cl.points, dtype=np.float64)
    radii = np.asarray(cl.radii, dtype=np.float64)
    if not (np.isfinite(pts).all() and np.isfinite(radii).all()):
        raise ValueError(
            "refusing to embed a non-finite centreline (NaN/inf points or radii)"
        )
    surface.field_data[_CL_POINTS] = pts
    surface.field_data[_CL_RADII] = radii
    surface.field_data[_CL_CLOSED] = np.array([1 if cl.closed else 0], dtype=np.int64)
    return surface


def centerline_from_surface(surface: pv.PolyData) -> "Centerline | None":
    """Reconstruct a centerline embedded by :func:`embed_centerline`, or ``None``
    when the surface carries none.  Tangents/arc lengths are re-derived from the
    stored points."""
    fd = surface.field_data
    if _CL_POINTS not in fd:
        return None
    pts = np.asarray(fd[_CL_POINTS], dtype=np.float64)
    if pts.ndim != 2 or pts.shape[1] != 3 or len(pts) < 2:
        return None
    # A crafted .vtp is untrusted input: non-finite embedded points would ride
    # through the `norms <= 0` tangent guard below (NaN is False) into a NaN
    # centreline that build_wall_lattice trusts.  Reject non-finite here — the
    # caller then recomputes a real skeleton centreline (fail-SAFE, not open).
    if not np.isfinite(pts).all():
        return None
    radii = np.asarray(fd[_CL_RADII], dtype=np.float64) if _CL_RADII in fd else None
    if radii is None or len(radii) != len(pts) or not np.isfinite(radii).all():
        return None
    closed = bool(int(np.asarray(fd[_CL_CLOSED]).ravel()[0])) if _CL_CLOSED in fd else False
    seg = np.linalg.norm(np.diff(pts, axis=0), axis=1)
    arc = np.concatenate([[0.0], np.cumsum(seg)])
    deriv = np.gradient(pts, axis=0)
    norms = np.linalg.norm(deriv, axis=1)
    if np.any(norms <= 0):
        return None
    tangents = deriv / norms[:, None]
    return Centerline(
        points=pts,
        tangents=tangents,
        arc_lengths=arc,
        radii=radii,
        length=float(arc[-1]),
        closed=closed,
        voxel_size=0.0,
        notes=["centreline restored from surface field data"],
    )


def _voxelize_interior(
    surface: pv.PolyData, voxel_size: float, pad: int = 2
) -> tuple[np.ndarray, np.ndarray]:
    """Binary interior mask on a padded regular grid; returns (mask, origin)."""
    b = surface.bounds
    origin = np.array([b[0], b[2], b[4]]) - pad * voxel_size
    extent = np.array([b[1] - b[0], b[3] - b[2], b[5] - b[4]])
    dims = np.ceil(extent / voxel_size).astype(int) + 1 + 2 * pad
    if int(np.prod(dims)) > MAX_VOXELS:
        raise ValueError(
            f"voxel grid {tuple(dims)} exceeds {MAX_VOXELS} voxels; "
            f"increase voxel_size (currently {voxel_size:.4g})"
        )
    grid = pv.ImageData(dimensions=dims, spacing=(voxel_size,) * 3, origin=origin)
    from digitwin.geometry.compat import interior_point_mask

    mask = interior_point_mask(grid, surface).reshape(dims, order="F")
    return mask, origin


def _skeleton_graph(skeleton: np.ndarray, voxel_size: float) -> nx.Graph:
    """26-connectivity graph over skeleton voxels, edge weight = distance."""
    voxels = np.argwhere(skeleton)
    index = {tuple(v): i for i, v in enumerate(voxels)}
    g = nx.Graph()
    g.add_nodes_from(range(len(voxels)))
    offsets = [
        (dx, dy, dz)
        for dx in (-1, 0, 1)
        for dy in (-1, 0, 1)
        for dz in (-1, 0, 1)
        if (dx, dy, dz) > (0, 0, 0)  # forward half of the 26-neighborhood
    ]
    for i, v in enumerate(voxels):
        for off in offsets:
            nb = (v[0] + off[0], v[1] + off[1], v[2] + off[2])
            j = index.get(nb)
            if j is not None:
                g.add_edge(i, j, weight=voxel_size * math.sqrt(sum(o * o for o in off)))
    return g


def _main_path(g: nx.Graph) -> tuple[list[int], bool]:
    """Longest geodesic through the skeleton; handles closed loops.

    Open tube: double-sweep between the farthest end voxels. Closed loop
    (no endpoints): diameter path + shortest return path with the interior
    of the first path removed.
    """
    if g.number_of_nodes() == 0:
        raise ValueError("skeleton is empty; the surface may be too thin for the grid")
    main = max(nx.connected_components(g), key=len)
    sub = g.subgraph(main)

    def farthest(source: int) -> int:
        dist = nx.single_source_dijkstra_path_length(sub, source, weight="weight")
        return max(dist, key=dist.get)

    start = next(iter(main))
    u = farthest(start)
    v = farthest(u)
    path = nx.dijkstra_path(sub, u, v, weight="weight")

    endpoints = [n for n in main if sub.degree(n) == 1]
    if endpoints:
        return path, False

    # Closed loop: return along the other side of the cycle.
    pruned = sub.copy()
    pruned.remove_nodes_from(path[1:-1])
    try:
        back = nx.dijkstra_path(pruned, v, u, weight="weight")
    except nx.NetworkXNoPath as exc:
        raise ValueError(
            "skeleton has no endpoints but no return path either; "
            "cannot order a closed centerline from this surface"
        ) from exc
    return path + back[1:-1], True


def _extend_to_caps(
    surface: pv.PolyData,
    points: np.ndarray,
    tangents: np.ndarray,
    radii: np.ndarray,
    voxel_size: float,
    cap_normal_tol: float,
    cap_standoff: float,
    notes: list[str],
) -> np.ndarray:
    """Extend both open ends toward flat caps (BACKLOG #1, supersedes the
    cap-coverage part of D9).

    The medial skeleton stops ~one radius short of a flat cap. Ray-cast each
    end tangent against the surface; extend only when the hit face is
    cap-like (|cos(face normal, tangent)| >= cap_normal_tol), stopping
    ``cap_standoff`` x local radius short of the wall. Side-wall exits and
    misses keep the old endpoint with a loud note. Returns the (possibly)
    extended polyline (extension segments are straight).
    """
    cell_normals = surface.compute_normals(
        cell_normals=True, point_normals=False
    ).cell_data["Normals"]
    prepend: list[np.ndarray] = []
    append: list[np.ndarray] = []
    for label, p, d, local_r in (
        ("start", points[0], -tangents[0], float(radii[0])),
        ("end", points[-1], tangents[-1], float(radii[-1])),
    ):
        ray_len = 4.0 * local_r + 10.0 * voxel_size
        hits, cells = surface.ray_trace(p, p + d * ray_len)
        if len(hits) == 0:
            notes.append(f"cap extension ({label}): no surface hit — endpoint kept")
            continue
        hit, cell = hits[0], int(cells[0])
        cos = abs(float(np.dot(cell_normals[cell], d)))
        dist = float(np.linalg.norm(hit - p))
        if cos < cap_normal_tol:
            notes.append(
                f"cap extension ({label}): ray exits through a side wall "
                f"(|cos|={cos:.3f} < {cap_normal_tol}) — endpoint kept"
            )
            continue
        ext = dist - cap_standoff * local_r
        if ext <= 0:
            notes.append(
                f"cap extension ({label}): already within standoff "
                f"({dist:.4g} <= {cap_standoff * local_r:.4g})"
            )
            continue
        target = p + d * ext
        notes.append(f"cap extension ({label}): +{ext:.4g} toward cap (|cos|={cos:.3f})")
        (prepend if label == "start" else append).append(target)

    parts = prepend + [points] + append
    return np.vstack([np.atleast_2d(x) for x in parts])


def _resample_polyline(
    poly: np.ndarray, n_samples: int
) -> tuple[np.ndarray, np.ndarray, np.ndarray, float]:
    """Uniform arc-length resample of a dense polyline; tangents by central
    differences. Returns (points, tangents, arc_lengths, length)."""
    seg = np.linalg.norm(np.diff(poly, axis=0), axis=1)
    cum = np.concatenate([[0.0], np.cumsum(seg)])
    length = float(cum[-1])
    targets = np.linspace(0.0, length, n_samples)
    points = np.column_stack(
        [interp1d(cum, poly[:, k])(targets) for k in range(3)]
    )
    deriv = np.gradient(points, targets, axis=0)
    tangents = deriv / np.linalg.norm(deriv, axis=1)[:, None]
    return points, tangents, targets, length


def compute_centerline(
    surface: pv.PolyData,
    *,
    voxel_size: float | None = None,
    smoothing: float | None = None,
    n_samples: int = 200,
    extend_to_caps: bool = True,
    cap_normal_tol: float = 0.8,
    cap_standoff: float = 0.02,
) -> Centerline:
    """Voxel/EDT/skeleton centerline, spline-smoothed, uniform in arc length.

    ``smoothing`` is the target RMS path deviation in PHYSICAL units
    (default: 1 voxel — enough to kill the voxel staircase without eating
    real curvature; resolution-independent by construction).

    ``extend_to_caps`` (default True) extends open ends to flat end caps by
    ray casting (no-op on closed loops); ``cap_normal_tol`` is the minimum
    |cos| between the hit face normal and the end tangent for the hit to
    count as a cap; ``cap_standoff`` is the gap kept to the cap as a
    fraction of the local radius.
    """
    notes: list[str] = []
    if surface.n_open_edges > 0:
        raise ValueError(
            f"surface has {surface.n_open_edges} open edges; "
            "centerline needs a watertight surface (run 'meshforge check')"
        )

    if voxel_size is None:
        diag = math.dist(surface.bounds[::2], surface.bounds[1::2])
        voxel_size = diag / 150.0
        notes.append(f"voxel_size defaulted to bbox_diag/150 = {voxel_size:.4g}")
    if smoothing is None:
        smoothing = voxel_size

    mask, origin = _voxelize_interior(surface, voxel_size)
    if not mask.any():
        raise ValueError("voxelization produced an empty interior; check the surface")

    edt = ndimage.distance_transform_edt(mask, sampling=voxel_size)

    from skimage.morphology import skeletonize

    skeleton = skeletonize(mask)
    graph = _skeleton_graph(skeleton, voxel_size)
    path, closed = _main_path(graph)

    voxels = np.argwhere(skeleton)
    path_vox = voxels[path]
    path_pts = origin + path_vox * voxel_size
    path_radii = edt[tuple(path_vox.T)]

    if len(path_pts) < 4:
        raise ValueError(f"centerline path has only {len(path_pts)} voxels; grid too coarse")

    # Spline with smoothing expressed as RMS deviation: splprep's s is the
    # sum of squared residuals, so s = N * rms².
    s_param = len(path_pts) * smoothing**2
    tck, _ = splprep(path_pts.T, s=s_param, per=closed, k=3)

    # Uniform arc-length resample via a dense parameter sweep.
    u_dense = np.linspace(0.0, 1.0, max(20 * n_samples, 2000))
    dense = np.column_stack(splev(u_dense, tck))
    seg = np.linalg.norm(np.diff(dense, axis=0), axis=1)
    cum = np.concatenate([[0.0], np.cumsum(seg)])
    length = float(cum[-1])
    targets = np.linspace(0.0, length, n_samples)
    u_uniform = interp1d(cum, u_dense)(targets)

    points = np.column_stack(splev(u_uniform, tck))
    deriv = np.column_stack(splev(u_uniform, tck, der=1))
    tangents = deriv / np.linalg.norm(deriv, axis=1)[:, None]

    # Radii: EDT along the raw path, mapped by arc length onto the resample.
    raw_cum = np.concatenate(
        [[0.0], np.cumsum(np.linalg.norm(np.diff(path_pts, axis=0), axis=1))]
    )
    raw_cum *= length / raw_cum[-1] if raw_cum[-1] > 0 else 1.0
    radii = interp1d(raw_cum, path_radii, bounds_error=False, fill_value=(path_radii[0], path_radii[-1]))(targets)

    if extend_to_caps and not closed:
        poly = _extend_to_caps(
            surface,
            points,
            tangents,
            np.asarray(radii),
            voxel_size,
            cap_normal_tol,
            cap_standoff,
            notes,
        )
        if len(poly) != len(points):  # at least one end was extended
            start_ext = float(np.linalg.norm(poly[0] - points[0]))
            old_positions = targets + start_ext
            old_radii = np.asarray(radii)
            points, tangents, targets, length = _resample_polyline(poly, n_samples)
            # Radii shift by the start extension; extended stretches carry
            # the end values (EDT radius is undefined past the skeleton).
            radii = interp1d(
                old_positions,
                old_radii,
                bounds_error=False,
                fill_value=(old_radii[0], old_radii[-1]),
            )(targets)

    notes.append(
        f"skeleton voxels={int(skeleton.sum())}, path voxels={len(path_pts)}, "
        f"smoothing_rms={smoothing:.4g}, closed={closed}"
    )
    return Centerline(
        points=points,
        tangents=tangents,
        arc_lengths=targets,
        radii=np.asarray(radii),
        length=length,
        closed=closed,
        voxel_size=voxel_size,
        notes=notes,
    )

"""Segmentation agreement metrics (M1, D83): voxel Dice, volume, surface distance.

Two closed surfaces are compared on a given voxel grid (shape + 4x4 voxel ->
world affine, rotated or flipped allowed): each is rasterised by its voxel
CENTRES (VTK's polydata -> image stencil, slice-by-slice scan conversion in
the grid's own index space, so an oblique grid needs no resampling), the
masks can be restricted to an axial slab, and Dice / volumes follow.
Surface distance is measured between area-uniform random samples (fixed
seed) and the other surface: ``assd`` = mean of the two directed means,
``hd95`` = max of the two directed 95th percentiles.
"""
from __future__ import annotations

from typing import Any

import numpy as np


def voxelize_surface(surface, shape: tuple[int, int, int], affine) -> np.ndarray:
    """Boolean mask of the grid voxels whose centre lies inside ``surface``.

    ``surface`` must be closed (a scan line through an open surface has no
    defined inside); only the surface's bounding box is rasterised.
    """
    import pyvista as pv
    from vtkmodules.util.numpy_support import vtk_to_numpy
    from vtkmodules.vtkImagingStencil import (
        vtkImageStencilToImage,
        vtkPolyDataToImageStencil,
    )

    from digitwin.io.image import check_affine

    a = check_affine(affine)
    shape = tuple(int(s) for s in shape)
    out = np.zeros(shape, dtype=bool)
    if surface.n_cells == 0:
        return out
    inv = np.linalg.inv(a)
    pts = np.asarray(surface.points, dtype=float) @ inv[:3, :3].T + inv[:3, 3]
    lo = np.maximum(np.floor(pts.min(axis=0)).astype(int), 0)
    hi = np.minimum(np.ceil(pts.max(axis=0)).astype(int), np.array(shape) - 1)
    if np.any(hi < lo):
        return out  # entirely outside the grid
    poly = pv.PolyData(pts, np.asarray(surface.faces))
    stencil = vtkPolyDataToImageStencil()
    stencil.SetInputData(poly)
    stencil.SetOutputOrigin(0.0, 0.0, 0.0)
    stencil.SetOutputSpacing(1.0, 1.0, 1.0)
    stencil.SetOutputWholeExtent(
        int(lo[0]), int(hi[0]), int(lo[1]), int(hi[1]), int(lo[2]), int(hi[2])
    )
    to_image = vtkImageStencilToImage()
    to_image.SetInputConnection(stencil.GetOutputPort())
    to_image.SetInsideValue(1)
    to_image.SetOutsideValue(0)
    to_image.SetOutputScalarTypeToUnsignedChar()
    to_image.Update()
    img = to_image.GetOutput()
    dims = tuple(int(v) for v in (hi - lo + 1))
    arr = vtk_to_numpy(img.GetPointData().GetScalars()).reshape(dims, order="F")
    out[lo[0] : hi[0] + 1, lo[1] : hi[1] + 1, lo[2] : hi[2] + 1] = arr.astype(bool)
    return out


def slab_mask(shape: tuple[int, int, int], affine, z0: float, z1: float) -> np.ndarray:
    """Voxels whose centre has world z in ``[z0, z1]``."""
    from digitwin.io.image import check_affine

    a = check_affine(affine)
    i, j, k = (np.arange(n, dtype=np.float64) for n in shape)
    z = (
        a[2, 0] * i[:, None, None]
        + a[2, 1] * j[None, :, None]
        + a[2, 2] * k[None, None, :]
        + a[2, 3]
    )
    return (z >= z0) & (z <= z1)


def dice(a: np.ndarray, b: np.ndarray) -> float:
    """Sørensen-Dice of two boolean masks (both empty -> refused)."""
    na, nb = int(np.count_nonzero(a)), int(np.count_nonzero(b))
    if na + nb == 0:
        raise ValueError("Dice of two empty masks is undefined")
    return 2.0 * int(np.count_nonzero(a & b)) / (na + nb)


def sample_surface(
    surface, n: int, *, seed: int = 0, z_range: tuple[float, float] | None = None
) -> np.ndarray:
    """``n`` area-uniform random points on a triangle surface (fixed seed),
    then those with world z inside ``z_range`` (when given)."""
    tri_mesh = surface.triangulate()
    p = np.asarray(tri_mesh.points, dtype=float)
    tri = tri_mesh.faces.reshape(-1, 4)[:, 1:]
    v0, v1, v2 = p[tri[:, 0]], p[tri[:, 1]], p[tri[:, 2]]
    area = 0.5 * np.linalg.norm(np.cross(v1 - v0, v2 - v0), axis=1)
    if area.sum() <= 0:
        raise ValueError("cannot sample a surface with zero area")
    rng = np.random.default_rng(seed)
    pick = rng.choice(len(tri), size=int(n), p=area / area.sum())
    r1, r2 = rng.random(int(n)), rng.random(int(n))
    s = np.sqrt(r1)
    pts = (
        (1.0 - s)[:, None] * v0[pick]
        + (s * (1.0 - r2))[:, None] * v1[pick]
        + (s * r2)[:, None] * v2[pick]
    )
    if z_range is not None:
        pts = pts[(pts[:, 2] >= z_range[0]) & (pts[:, 2] <= z_range[1])]
    return pts


def point_to_surface(points: np.ndarray, surface) -> np.ndarray:
    """Unsigned distance from each point to the nearest point of ``surface``."""
    if len(points) == 0:
        return np.zeros(0)
    _, closest = surface.find_closest_cell(points, return_closest_point=True)
    return np.linalg.norm(np.asarray(points) - np.asarray(closest), axis=1)


def surface_distance(
    a,
    b,
    *,
    n_samples: int = 20000,
    seed: int = 0,
    z_range: tuple[float, float] | None = None,
) -> dict[str, Any]:
    """Symmetric surface distance between surfaces ``a`` and ``b``.

    Samples on each (restricted to ``z_range``) measured to the other.
    Returns ``assd`` (mean of the two directed means), ``hd95`` (max of the
    two directed 95th percentiles), the directed values and sample counts.
    """
    pa = sample_surface(a, n_samples, seed=seed, z_range=z_range)
    pb = sample_surface(b, n_samples, seed=seed + 1, z_range=z_range)
    if len(pa) == 0 or len(pb) == 0:
        raise ValueError(
            f"no surface samples inside z range {z_range} — the surfaces do "
            f"not overlap there"
        )
    dab, dba = point_to_surface(pa, b), point_to_surface(pb, a)
    return {
        "assd": float((dab.mean() + dba.mean()) / 2.0),
        "hd95": float(max(np.percentile(dab, 95), np.percentile(dba, 95))),
        "mean_a_to_b": float(dab.mean()),
        "mean_b_to_a": float(dba.mean()),
        "p95_a_to_b": float(np.percentile(dab, 95)),
        "p95_b_to_a": float(np.percentile(dba, 95)),
        "n_a": int(len(pa)),
        "n_b": int(len(pb)),
    }


def compare_to_regions(
    surface,
    regions: dict[str, Any],
    walls: dict[str, Any],
    shape: tuple[int, int, int],
    affine,
    z0: float,
    z1: float,
    *,
    end_margin: float = 2.0,
    n_samples: int = 20000,
    seed: int = 0,
) -> dict[str, Any]:
    """Compare ``surface`` with each closed reference region on one grid.

    Dice and volumes use the voxels whose centre lies in the slab
    ``z0 <= z <= z1``. Surface distance uses the region's open lateral
    surface ``walls[name]`` (so artificial end caps never count), sampled
    in the slab shrunk by ``end_margin`` at each end. ``best`` = highest
    Dice.
    """
    from digitwin.io.image import check_affine

    a = check_affine(affine)
    if not z1 > z0:
        raise ValueError(f"empty slab: z0={z0} >= z1={z1}")
    slab = slab_mask(shape, a, z0, z1)
    voxel_ml = abs(float(np.linalg.det(a[:3, :3]))) / 1000.0
    seg = voxelize_surface(surface, shape, a) & slab
    seg_ml = int(np.count_nonzero(seg)) * voxel_ml
    zr = (z0 + end_margin, z1 - end_margin)
    out: dict[str, Any] = {
        "slab_z_mm": [float(z0), float(z1)],
        "slab_voxels": int(np.count_nonzero(slab)),
        "voxel_volume_mm3": voxel_ml * 1000.0,
        "segmented_volume_ml": seg_ml,
        "distance_z_mm": [float(zr[0]), float(zr[1])],
        "regions": {},
    }
    for name, region in regions.items():
        ref = voxelize_surface(region, shape, a) & slab
        ref_ml = int(np.count_nonzero(ref)) * voxel_ml
        dist = surface_distance(
            surface, walls[name], n_samples=n_samples, seed=seed, z_range=zr
        )
        out["regions"][name] = {
            "dice": dice(seg, ref),
            "volume_ml": ref_ml,
            "volume_ratio": seg_ml / ref_ml if ref_ml > 0 else None,
            "distance": dist,
        }
    out["best"] = max(out["regions"], key=lambda n: out["regions"][n]["dice"])
    return out

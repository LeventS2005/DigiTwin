"""Zenodo AAA dataset adapter (M1, D83): a case's reference segmentation.

Dataset: "4D CT Angiography and Computational Biomechanics Dataset for
Assessing Structural Integrity of Abdominal Aortic Aneurysms in 20
Patients", Zenodo, DOI 10.5281/zenodo.19182977, CC BY 4.0. Each case
folder (``P01`` .. ``P20``) holds 4D-CTA frames (``4D_CTA/*.nrrd``) and the
authors' surfaces, a REFERENCE segmentation (AI-assisted segmentation plus
automated surface generation) — not ground truth.

Surfaces are VTK PolyData in RAS millimetres, patch id in the cell array
``SurfID`` (meanings inferred from the geometry, M0 inspection):

* ``ILTSurface.vtp`` patch 3 — lumen (blood side of the thrombus)
* ``ILTSurface.vtp`` patch 4 — thrombus outer = wall inner interface
* ``WallSurface.vtp`` patch 5 — outer wall (patch 4 offset by a constructed
  1.5 mm)
* ``WallSurface.vtp`` patch 2 / ``ILTSurface.vtp`` patch 1 — flat end
  annuli on the min-z / max-z planes

Three closed regions are built by capping each open tube on those planes:
``lumen`` (blood only), ``lumen_ilt`` (blood + thrombus) and ``full``
(blood + thrombus + the 1.5 mm wall). Comparing a segmentation with all
three says which anatomy it captured.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

PATCH_ARRAY = "SurfID"
#: region -> (file, patch id); order = inner to outer.
REGION_PATCHES: dict[str, tuple[str, int]] = {
    "lumen": ("ILTSurface.vtp", 3),
    "lumen_ilt": ("ILTSurface.vtp", 4),
    "full": ("WallSurface.vtp", 5),
}
REGION_LABELS = {
    "lumen": "R_lumen (blood lumen)",
    "lumen_ilt": "R_lumen_ilt (lumen + thrombus)",
    "full": "R_full (lumen + thrombus + 1.5 mm wall)",
}
_END_PATCH = ("WallSurface.vtp", 2)
DATASET_CREDIT = (
    "Zenodo AAA 4D-CTA dataset, DOI 10.5281/zenodo.19182977, CC BY 4.0 "
    "(reference segmentation, not ground truth)"
)


@dataclass
class ZenodoReference:
    case_dir: Path
    regions: dict[str, Any]  # closed (capped) PolyData per region
    walls: dict[str, Any]  # the open lateral patch per region
    z0: float  # lower end plane (world z, mm)
    z1: float  # upper end plane
    notes: list[str] = field(default_factory=list)


def _read(case_dir: Path, name: str):
    import pyvista as pv

    p = case_dir / name
    if not p.is_file():
        raise FileNotFoundError(f"reference surface not found: {p}")
    mesh = pv.read(str(p))
    if PATCH_ARRAY not in mesh.cell_data:
        raise ValueError(
            f"{p.name} has no '{PATCH_ARRAY}' cell array — not a Zenodo AAA "
            f"reference surface"
        )
    return mesh


def _patch(mesh, sid: int, label: str):
    import pyvista as pv

    ids = np.asarray(mesh.cell_data[PATCH_ARRAY])
    sel = np.flatnonzero(ids == sid)
    if sel.size == 0:
        raise ValueError(f"{label}: no cells with {PATCH_ARRAY} == {sid}")
    sub = mesh.extract_cells(sel)
    if not isinstance(sub, pv.PolyData):
        sub = sub.extract_surface(algorithm="dataset_surface")
    sub = sub.triangulate().clean()
    for name in list(sub.point_data.keys()):
        del sub.point_data[name]
    for name in list(sub.cell_data.keys()):
        del sub.cell_data[name]
    return sub


def _cap(tube, z0: float, z1: float, label: str, tol: float):
    """Close an open tube whose two rims lie on the planes z0 / z1."""
    rims = tube.extract_feature_edges(
        boundary_edges=True,
        feature_edges=False,
        manifold_edges=False,
        non_manifold_edges=False,
    )
    if rims.n_points == 0:
        raise ValueError(f"{label}: the patch is already closed — expected an open tube")
    z = np.asarray(rims.points)[:, 2]
    off = np.minimum(np.abs(z - z0), np.abs(z - z1))
    if float(off.max()) > tol:
        raise ValueError(
            f"{label}: a rim point lies {off.max():.3g} mm off the end planes "
            f"z={z0:.3f}/{z1:.3f} — cannot cap it flat"
        )
    span = float(np.linalg.norm(np.ptp(np.asarray(tube.points), axis=0)))
    capped = tube.fill_holes(10.0 * span).clean()
    nonmanifold = capped.extract_feature_edges(
        boundary_edges=False,
        feature_edges=False,
        manifold_edges=False,
        non_manifold_edges=True,
    ).n_cells
    if capped.n_open_edges or nonmanifold:
        raise ValueError(
            f"{label}: capping left {capped.n_open_edges} open and "
            f"{nonmanifold} non-manifold edge(s) — the reference patch is not a "
            f"clean tube"
        )
    # The fill polygons are not wound like the tube: re-orient every face
    # consistently and outward (a closed manifold now), then drop the arrays.
    capped = capped.compute_normals(
        cell_normals=True,
        point_normals=False,
        consistent_normals=True,
        auto_orient_normals=True,
        split_vertices=False,
    )
    for name in list(capped.point_data.keys()):
        del capped.point_data[name]
    for name in list(capped.cell_data.keys()):
        del capped.cell_data[name]
    return capped


def _end_planes(z: np.ndarray, tol: float, case: str) -> tuple[float, float]:
    """The two end-plane heights of the annuli's points.

    Levels are the points' z rounded to 1e-3 mm; levels closer than ``tol``
    form one plane (D86: P05 and P16 each carry ONE rim vertex 0.009 / 0.002
    mm off its plane — float32 rounding — which the strict count refused).
    Each plane's height is its most populated level. Anything other than
    exactly two planes is refused.
    """
    levels, counts = np.unique(np.round(z, 3), return_counts=True)
    groups = np.split(np.arange(levels.size), np.flatnonzero(np.diff(levels) > tol) + 1)
    if len(groups) != 2:
        raise ValueError(
            f"{case}: end annuli lie on {len(groups)} z level(s), expected "
            f"exactly 2 (the min-z and max-z planes)"
        )
    lo, hi = (float(levels[g[int(np.argmax(counts[g]))]]) for g in groups)
    return lo, hi


def load_reference(case_dir: str | Path) -> ZenodoReference:
    """The three closed reference regions of one Zenodo AAA case (RAS, mm).

    Every structural surprise (missing file or patch, end annuli not on two
    planes, a rim off its plane, a cap that does not close) is a loud
    ValueError naming the case and region.
    """
    case_dir = Path(case_dir)
    if not case_dir.is_dir():
        raise FileNotFoundError(f"Zenodo case directory not found: {case_dir}")
    meshes: dict[str, Any] = {}

    def mesh(name: str):
        if name not in meshes:
            meshes[name] = _read(case_dir, name)
        return meshes[name]

    ends = _patch(mesh(_END_PATCH[0]), _END_PATCH[1], f"{case_dir.name} end annuli")
    tol = 1e-2  # mm; the annuli sit on their planes (rounded 1e-3)
    z0, z1 = _end_planes(np.asarray(ends.points)[:, 2], tol, case_dir.name)
    regions, walls = {}, {}
    for region, (fname, sid) in REGION_PATCHES.items():
        label = f"{case_dir.name} {region} ({fname} {PATCH_ARRAY} {sid})"
        tube = _patch(mesh(fname), sid, label)
        walls[region] = tube
        regions[region] = _cap(tube, z0, z1, label, tol)
    return ZenodoReference(
        case_dir=case_dir,
        regions=regions,
        walls=walls,
        z0=z0,
        z1=z1,
        notes=[DATASET_CREDIT],
    )


def validate_against_reference(
    surface,
    case_dir: str | Path,
    grid_shape: tuple[int, int, int],
    grid_affine,
    *,
    end_margin: float = 2.0,
    n_samples: int = 20000,
) -> dict[str, Any]:
    """Dice / volume / surface distance of ``surface`` against the case's three
    reference regions, on the CT grid (``grid_shape`` + RAS-mm affine),
    restricted to the slab between the reference end planes."""
    from digitwin.quality.overlap import compare_to_regions

    ref = load_reference(case_dir)
    result = compare_to_regions(
        surface,
        ref.regions,
        ref.walls,
        grid_shape,
        grid_affine,
        ref.z0,
        ref.z1,
        end_margin=end_margin,
        n_samples=n_samples,
    )
    result["case"] = Path(case_dir).name
    result["reference"] = DATASET_CREDIT
    result["region_labels"] = dict(REGION_LABELS)
    return result

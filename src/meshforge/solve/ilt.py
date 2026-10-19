"""Intraluminal thrombus (ILT) as a solid in the AAA wall-stress model (D91).

The wall-only model loads the wall's inner surface with the full blood
pressure. In an aneurysm the pressure acts on the LUMEN, and the thrombus
between lumen and wall carries part of it. Leaving it out overstates wall
stress: on a native AAA, 674 kPa without it vs 278 kPa with it. Against
the Zenodo AAA dataset's published FE result, which includes thrombus, the
wall-only tension read 1.42x high, and the gap tracked thrombus thickness.

The thrombus is the region between the blood-lumen surface and the sac
surface, the wall's inner surface. Both are cropped with the same two cap
planes (D85).
1. The lumen is re-cut ``inset`` mm inside each cap plane, so the hole
   never touches the outer boundary: no coplanar facets for gmsh.
2. Both surfaces are remeshed by gmsh at ``size``. Marching-cubes needles
   would otherwise become sliver tets (D90).
3. The region is filled with linear tets, oriented positive, then made
   C3D10 with STRAIGHT mid-edge nodes. That matches the wall's
   straight-edged C3D20R; node order is 1-2, 2-3, 3-1, 1-4, 2-4, 3-4.

Boundary faces closer to the lumen than to the sac are the pressure
surface; the rest are tied to the wall (``*TIE ... ADJUST=NO``: moving
slave nodes onto the wall inverted thin tets). The thrombus ends on the cap
planes stay free; the wall carries the clamp.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pyvista as pv

DEFAULT_E_ILT_MPA = 5.0  # the Zenodo AAA dataset's FE deck (wall 100 MPa)
DEFAULT_NU_ILT = 0.49
DEFAULT_ILT_SIZE_MM = 3.0
DEFAULT_INSET_MM = 0.5
_TET10_EDGES = ((0, 1), (1, 2), (2, 0), (0, 3), (1, 3), (2, 3))
_TET_FACES = {"S1": (0, 1, 2), "S2": (0, 3, 1), "S3": (1, 3, 2), "S4": (2, 3, 0)}


@dataclass
class IltMesh:
    points: np.ndarray  # (n, 3): corner nodes, then straight mid-edge nodes
    tets: np.ndarray  # (m, 10) C3D10 connectivity, 0-based into ``points``
    inner_faces: list[tuple[int, str]]  # (tet index, S1..S4) on the lumen: pressure
    outer_faces: list[tuple[int, str]]  # on the sac: tied to the wall's inner faces
    volume_ml: float
    expected_volume_ml: float
    sj_min: float
    notes: list[str] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        return {
            "n_tets": int(len(self.tets)),
            "n_nodes": int(len(self.points)),
            "volume_ml": round(self.volume_ml, 3),
            "expected_volume_ml": round(self.expected_volume_ml, 3),
            "sj_min": round(self.sj_min, 4),
            "n_pressure_faces": len(self.inner_faces),
            "n_tied_faces": len(self.outer_faces),
            "notes": list(self.notes),
        }


def _gmsh():
    try:
        import gmsh
    except ImportError as exc:  # pragma: no cover - gmsh ships with the env
        raise RuntimeError(
            "the thrombus mesh needs gmsh — install with: pip install 'meshforge[meshing]'"
        ) from exc
    return gmsh


def _remesh(surface: pv.PolyData, size: float) -> pv.PolyData:
    """gmsh reparametrize (40 deg creases) + remesh a closed surface at ``size``."""
    gmsh = _gmsh()
    tri = surface.triangulate().clean()
    faces = tri.faces.reshape(-1, 4)[:, 1:]
    gmsh.initialize()
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        gmsh.model.add("meshforge_ilt_remesh")
        gmsh.model.addDiscreteEntity(2, 1)
        gmsh.model.mesh.addNodes(2, 1, np.arange(1, tri.n_points + 1), np.asarray(tri.points).ravel())
        gmsh.model.mesh.addElementsByType(1, 2, [], (faces + 1).ravel())
        gmsh.model.mesh.classifySurfaces(math.radians(40.0), True, True, math.pi)
        gmsh.model.mesh.createGeometry()
        for name, value in (("Mesh.MeshSizeMax", size), ("Mesh.MeshSizeMin", size),
                            ("Mesh.MeshSizeFromPoints", 0), ("Mesh.MeshSizeExtendFromBoundary", 0)):
            gmsh.option.setNumber(name, value)
        gmsh.model.mesh.generate(2)
        tags, xyz, _ = gmsh.model.mesh.getNodes()
        index = np.zeros(int(tags.max()) + 1, dtype=np.int64)
        index[np.asarray(tags, dtype=np.int64)] = np.arange(len(tags))
        tris = []
        for dim, tag in gmsh.model.getEntities(2):
            types, _, nodes = gmsh.model.mesh.getElements(dim, tag)
            for etype, enodes in zip(types, nodes):
                if etype == 2:
                    tris.append(index[np.asarray(enodes, dtype=np.int64)].reshape(-1, 3))
        points = np.asarray(xyz, dtype=float).reshape(-1, 3)
    finally:
        gmsh.finalize()
    tris = np.vstack(tris)
    out = pv.PolyData(points, np.column_stack([np.full(len(tris), 3), tris]).ravel()).clean()
    if out.n_open_edges:
        raise ValueError(f"the remeshed surface is not closed ({out.n_open_edges} open edges)")
    return out


def _fill_between(outer: pv.PolyData, inner: pv.PolyData, size: float):
    """Linear tets of inside(outer) minus inside(inner)."""
    gmsh = _gmsh()
    gmsh.initialize()
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        gmsh.model.add("meshforge_ilt")
        offset = 0
        for tag, surf in ((1, outer), (2, inner)):
            faces = surf.faces.reshape(-1, 4)[:, 1:]
            gmsh.model.addDiscreteEntity(2, tag)
            gmsh.model.mesh.addNodes(
                2, tag, np.arange(offset + 1, offset + surf.n_points + 1),
                np.asarray(surf.points).ravel(),
            )
            gmsh.model.mesh.addElementsByType(tag, 2, [], (faces + offset + 1).ravel())
            offset += surf.n_points
        loops = [gmsh.model.geo.addSurfaceLoop([1]), gmsh.model.geo.addSurfaceLoop([2])]
        gmsh.model.geo.addVolume(loops)
        gmsh.model.geo.synchronize()
        gmsh.option.setNumber("Mesh.MeshSizeMax", size)
        gmsh.option.setNumber("Mesh.MeshSizeMin", 0.5 * size)
        try:
            gmsh.model.mesh.generate(3)
        except Exception as exc:  # gmsh raises a bare Exception
            raise RuntimeError(
                f"thrombus tet fill failed — gmsh: {exc}. The lumen probably touches or "
                f"crosses the sac surface"
            ) from None
        try:
            gmsh.model.mesh.optimize("Netgen")
        except Exception:  # pragma: no cover - optimization is best effort
            pass
        tags, xyz, _ = gmsh.model.mesh.getNodes()
        index = np.zeros(int(tags.max()) + 1, dtype=np.int64)
        index[np.asarray(tags, dtype=np.int64)] = np.arange(len(tags))
        _, tet_nodes = gmsh.model.mesh.getElementsByType(4)
        tets = index[np.asarray(tet_nodes, dtype=np.int64)].reshape(-1, 4)
        points = np.asarray(xyz, dtype=float).reshape(-1, 3)
    finally:
        gmsh.finalize()
    if len(tets) == 0:
        raise RuntimeError("gmsh produced no thrombus tetrahedra")
    return points, tets


def _orient(points: np.ndarray, tets: np.ndarray) -> np.ndarray:
    a, b, c, d = (points[tets[:, i]] for i in range(4))
    neg = np.einsum("ij,ij->i", np.cross(b - a, c - a), d - a) < 0
    tets = tets.copy()
    tets[neg] = tets[neg][:, [0, 2, 1, 3]]
    return tets


def _tet10(points: np.ndarray, tets: np.ndarray):
    ends = np.sort(tets[:, _TET10_EDGES], axis=2).reshape(-1, 2)
    uniq, inv = np.unique(ends, axis=0, return_inverse=True)
    mids = (len(points) + inv.ravel()).reshape(len(tets), 6)
    allpts = np.vstack([points, 0.5 * (points[uniq[:, 0]] + points[uniq[:, 1]])])
    return allpts, np.hstack([tets, mids])


def _boundary_faces(points, tets, inner: pv.PolyData, outer: pv.PolyData):
    from scipy.spatial import cKDTree

    grid = pv.UnstructuredGrid(
        np.column_stack([np.full(len(tets), 4), tets]).ravel(),
        np.full(len(tets), pv.CellType.TETRA, dtype=np.uint8), points,
    )
    surf = grid.extract_surface(algorithm="dataset_surface")
    faces = surf.faces.reshape(-1, 4)[:, 1:]
    cell = np.asarray(surf.cell_data["vtkOriginalCellIds"], dtype=np.int64)
    pid = np.asarray(surf.point_data["vtkOriginalPointIds"], dtype=np.int64)
    centres = surf.points[faces].mean(axis=1)
    d_in = cKDTree(inner.points).query(centres)[0]
    d_out = cKDTree(outer.points).query(centres)[0]
    inner_faces, outer_faces = [], []
    for k, f in enumerate(faces):
        nodes = set(pid[f].tolist())
        row = tets[cell[k]]
        label = next(lab for lab, loc in _TET_FACES.items() if {int(row[i]) for i in loc} == nodes)
        (inner_faces if d_in[k] < d_out[k] else outer_faces).append((int(cell[k]), label))
    return inner_faces, outer_faces


def build_ilt_mesh(lumen: pv.PolyData, sac: pv.PolyData, planes, *,
                   size: float = DEFAULT_ILT_SIZE_MM,
                   inset: float = DEFAULT_INSET_MM,
                   remesh_sac: bool = True) -> IltMesh:
    """The thrombus solid between ``lumen`` and ``sac``, which share the two cut
    ``planes`` (``geometry.crop.planes_from_report``). Refuses a lumen that
    pokes through the sac, and a fill whose volume misses sac - lumen by
    more than 2%. ``remesh_sac=False`` keeps the sac triangulation as given:
    right for a sac rebuilt from a structured wall's inner faces (no needles,
    and the thrombus's outer nodes then sit exactly on the wall's)."""
    size, inset = float(size), float(inset)
    if not (np.isfinite(size) and size > 0 and np.isfinite(inset) and inset > 0):
        raise ValueError(f"thrombus mesh size and inset must be positive, got {size}, {inset}")
    # re-cut the lumen inset mm inside each cap plane with the crop's own
    # planar cut + snapped flat caps (no centreline needed). Not
    # clip_closed_surface: its caps carried needles of aspect ~4000 on a real
    # lumen, and gmsh's remesh then hung.
    from meshforge.geometry.crop import CutPlane, cut_and_cap

    inside = [
        CutPlane(name=pl.name, origin=np.asarray(pl.origin, dtype=float)
                 - inset * np.asarray(pl.normal, dtype=float),
                 normal=np.asarray(pl.normal, dtype=float), height=pl.height)
        for pl in planes
    ]
    keep = 0.5 * (inside[0].origin + inside[1].origin)
    lumen_cut, _ = cut_and_cap(lumen.triangulate(), inside, keep, what="lumen",
                               require_enclosure=False)
    probe = lumen_cut.points[:: max(1, lumen_cut.n_points // 2000)]
    enclosed = pv.PolyData(probe).select_enclosed_points(sac, check_surface=False)
    outside = 1.0 - float(np.mean(enclosed.point_data["SelectedPoints"]))
    if outside > 0.01:
        raise ValueError(
            f"{100 * outside:.1f}% of the lumen surface lies outside the sac surface — "
            f"the thrombus region is not a shell between them"
        )
    lumen_r = _remesh(lumen_cut, size)
    sac_r = _remesh(sac, size) if remesh_sac else sac.triangulate().clean()
    points, tets = _fill_between(sac_r, lumen_r, size)
    tets = _orient(points, tets)
    a, b, c, d = (points[tets[:, i]] for i in range(4))
    vols = np.einsum("ij,ij->i", np.cross(b - a, c - a), d - a) / 6.0
    volume = float(vols.sum()) / 1000.0
    expected = (abs(float(sac.volume)) - abs(float(lumen_cut.volume))) / 1000.0
    if expected <= 0 or abs(volume - expected) > 0.02 * expected:
        raise ValueError(
            f"thrombus fill volume {volume:.2f} mL vs sac - lumen {expected:.2f} mL (> 2% off)"
        )
    grid = pv.UnstructuredGrid(
        np.column_stack([np.full(len(tets), 4), tets]).ravel(),
        np.full(len(tets), pv.CellType.TETRA, dtype=np.uint8), points,
    )
    sj = grid.cell_quality("scaled_jacobian")["scaled_jacobian"]
    inner_faces, outer_faces = _boundary_faces(points, tets, lumen_r, sac_r)
    pts10, tets10 = _tet10(points, tets)
    notes = [
        f"thrombus = sac - lumen, the lumen re-cut {inset:g} mm inside both cap planes; "
        f"surfaces remeshed by gmsh at {size:g} mm; {len(tets)} C3D10 (straight mid-edge nodes)",
        f"volume {volume:.2f} mL (sac - lumen {expected:.2f} mL)",
    ]
    return IltMesh(
        points=pts10, tets=tets10, inner_faces=inner_faces, outer_faces=outer_faces,
        volume_ml=volume, expected_volume_ml=expected, sj_min=float(np.min(sj)), notes=notes,
    )

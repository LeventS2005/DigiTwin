"""3-D CAD import (V16): STEP/IGES/BREP solids -> triangulated surface.

Uses gmsh's OpenCASCADE kernel — the same lazy dependency as the tet
backend, same lifecycle (initialize/finalize per call, terminal quiet).
2-D DXF/DWG stay in ``io/cad.py``; suffixes decide, nothing is guessed.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np

_SUFFIXES = {".step", ".stp", ".iges", ".igs", ".brep"}
_DEFAULT_DIAG_FRACTION = 50.0


def load_solid(
    path: str | Path,
    *,
    mesh_size: float | None = None,
    mesh_size_factor: float | None = None,
):
    """Import a STEP/IGES/BREP file and surface-mesh every face.

    Returns ``(surface, provenance)``: an all-triangle pv.PolyData (nodes
    shared across faces, so a closed solid meshes watertight) and a dict
    with n_volumes/n_surfaces/bbox/mesh_size. ``mesh_size`` is absolute;
    ``mesh_size_factor`` scales the default (bbox diagonal / 50); giving
    both is refused loudly.
    """
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"CAD file not found: {path}")
    if path.suffix.lower() not in _SUFFIXES:
        raise ValueError(
            f"unsupported 3-D CAD suffix {path.suffix!r} for {path.name}; "
            f"supported: {', '.join(sorted(_SUFFIXES))} "
            f"(2-D DXF/DWG go through import-cad)"
        )
    if mesh_size is not None and mesh_size_factor is not None:
        raise ValueError(
            "mesh_size and mesh_size_factor are mutually exclusive; "
            "give the absolute size OR the default-scaling factor"
        )
    if mesh_size is not None and not mesh_size > 0:
        raise ValueError(f"mesh_size must be positive, got {mesh_size}")
    if mesh_size_factor is not None and not mesh_size_factor > 0:
        raise ValueError(f"mesh_size_factor must be positive, got {mesh_size_factor}")
    try:
        import gmsh
    except ImportError as exc:  # pragma: no cover - dep present in CI env
        raise RuntimeError(
            "3-D CAD import needs gmsh — install with: pip install 'digitwin[meshing]'"
        ) from exc
    import pyvista as pv

    gmsh.initialize()
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        gmsh.model.add("digitwin_cad3d")
        try:
            gmsh.model.occ.importShapes(str(path))
        except Exception as exc:
            raise ValueError(f"gmsh/OCC could not import {path.name}: {exc}") from exc
        gmsh.model.occ.synchronize()
        n_volumes = len(gmsh.model.getEntities(3))
        n_surfaces = len(gmsh.model.getEntities(2))
        if n_surfaces == 0:
            raise ValueError(
                f"{path.name} contains no surfaces or volumes after OCC import"
            )
        bbox = gmsh.model.getBoundingBox(-1, -1)
        diag = float(np.linalg.norm(np.array(bbox[3:]) - np.array(bbox[:3])))
        if not diag > 0:
            raise ValueError(f"{path.name} has a degenerate (zero-size) bounding box")
        size = (
            mesh_size
            if mesh_size is not None
            else diag / _DEFAULT_DIAG_FRACTION * (mesh_size_factor or 1.0)
        )
        gmsh.option.setNumber("Mesh.MeshSizeMax", size)
        gmsh.model.mesh.generate(2)

        node_tags, node_xyz, _ = gmsh.model.mesh.getNodes()
        etypes, _etags, enodes = gmsh.model.mesh.getElements(2)
        tri_conn = None
        for etype, nodes in zip(etypes, enodes):
            if int(etype) == 2:  # 3-node triangle
                tri_conn = np.asarray(nodes, dtype=np.int64).reshape(-1, 3)
            else:
                raise ValueError(
                    f"gmsh produced non-triangle surface elements (type {int(etype)}) "
                    f"for {path.name} — unexpected for generate(2) defaults"
                )
        if tri_conn is None or len(tri_conn) == 0:
            raise ValueError(f"surface meshing of {path.name} produced no triangles")

        points = np.asarray(node_xyz, dtype=float).reshape(-1, 3)
        remap = np.full(int(np.max(node_tags)) + 1, -1, dtype=np.int64)
        remap[np.asarray(node_tags, dtype=np.int64)] = np.arange(len(points))
        tris = remap[tri_conn]
        if (tris < 0).any():
            raise ValueError(
                f"triangle references an unknown gmsh node in {path.name}"
            )
    finally:
        gmsh.finalize()

    faces = np.column_stack(
        [np.full(len(tris), 3, dtype=np.int64), tris]
    ).ravel()
    # clean() drops nodes owned by no triangle (gmsh CAD edge/vertex seeds);
    # tolerance 0 so genuinely distinct points are never merged.
    surface = pv.PolyData(points, faces).clean(tolerance=0.0, absolute=True)
    provenance = {
        "path": str(path),
        "format": path.suffix.lower().lstrip("."),
        "n_volumes": int(n_volumes),
        "n_surfaces": int(n_surfaces),
        "bbox_min": [float(v) for v in bbox[:3]],
        "bbox_max": [float(v) for v in bbox[3:]],
        "mesh_size": float(size),
        "n_points": int(surface.n_points),
        "n_triangles": int(surface.n_cells),
    }
    return surface, provenance

from __future__ import annotations

import numpy as np
import pyvista as pv
from scipy.spatial import cKDTree

from digitwin.meshing.base import BoundaryGroup, WallMesh
from digitwin.meshing.lattice import build_wall_lattice
from digitwin.quality.volume import volume_quality

_LABEL_NAMES = {0: "inner", 1: "outer", 2: "inlet", 3: "outlet"}


def mesh_shell_pair(
    inner: pv.PolyData,
    outer: pv.PolyData,
    *,
    mesh_size: float,
    optimize: bool = True,
) -> WallMesh:
    """Tetrahedralize the region between two watertight shells (V11 trees).

    The volume is declared as the outer surface loop with the inner shell
    as a cavity (the V9 lesson: one combined loop makes gmsh fill the
    lumen). Boundary faces classify by nearest source shell.
    """
    try:
        import gmsh
    except ImportError as exc:
        raise RuntimeError(
            "mesh_shell_pair needs gmsh — install with: pip install 'digitwin[meshing]'"
        ) from exc
    if not mesh_size > 0:
        raise ValueError(f"mesh_size must be positive, got {mesh_size}")

    inner = inner.triangulate()
    outer = outer.triangulate()
    for name, shell in (("inner", inner), ("outer", outer)):
        if shell.n_open_edges > 0:
            raise ValueError(f"{name} shell is not watertight ({shell.n_open_edges} open edges)")

    in_pts = np.asarray(inner.points, dtype=float)
    out_pts = np.asarray(outer.points, dtype=float)
    in_tris = inner.faces.reshape(-1, 4)[:, 1:]
    out_tris = outer.faces.reshape(-1, 4)[:, 1:] + len(in_pts)
    shell_pts = np.vstack([in_pts, out_pts])
    shell_tris = np.vstack([in_tris, out_tris])
    patch_labels = np.concatenate(
        [np.zeros(len(in_tris), dtype=np.int64), np.ones(len(out_tris), dtype=np.int64)]
    )

    gmsh.initialize()
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        gmsh.model.add("digitwin_shell_pair")
        node_tags = np.arange(1, len(shell_pts) + 1)
        gmsh.model.addDiscreteEntity(2, 1)
        gmsh.model.addDiscreteEntity(2, 2)
        gmsh.model.mesh.addNodes(2, 1, node_tags, shell_pts.ravel())
        gmsh.model.mesh.addElementsByType(1, 2, [], (out_tris + 1).ravel())
        gmsh.model.mesh.addElementsByType(2, 2, [], (in_tris + 1).ravel())
        loop_outer = gmsh.model.geo.addSurfaceLoop([1])
        loop_inner = gmsh.model.geo.addSurfaceLoop([2])
        gmsh.model.geo.addVolume([loop_outer, loop_inner])
        gmsh.model.geo.synchronize()
        gmsh.option.setNumber("Mesh.MeshSizeMax", mesh_size)
        gmsh.model.mesh.generate(3)
        opt_note = "optimization disabled (optimize=False)"
        if optimize:
            try:
                gmsh.model.mesh.optimize("Netgen")
                opt_note = "Netgen mesh optimization applied"
            except Exception as netgen_exc:
                try:
                    gmsh.model.mesh.optimize("")
                    opt_note = (
                        f"Netgen optimization failed ({netgen_exc}); "
                        "default gmsh optimizer applied"
                    )
                except Exception as default_exc:
                    opt_note = (
                        f"mesh optimization skipped (Netgen: {netgen_exc}; "
                        f"default: {default_exc})"
                    )
        tags, coords, _ = gmsh.model.mesh.getNodes()
        points = np.asarray(coords, dtype=float).reshape(-1, 3)
        tag_to_idx = np.zeros(int(tags.max()) + 1, dtype=np.int64)
        tag_to_idx[np.asarray(tags, dtype=np.int64)] = np.arange(len(tags))
        _, tet_nodes = gmsh.model.mesh.getElementsByType(4)
        tets = tag_to_idx[np.asarray(tet_nodes, dtype=np.int64)].reshape(-1, 4)
    finally:
        gmsh.finalize()

    if len(tets) == 0:
        raise ValueError("gmsh produced no tetrahedra between the shells")

    cells = np.column_stack([np.full(len(tets), 4, dtype=np.int64), tets]).ravel()
    celltypes = np.full(len(tets), pv.CellType.TETRA, dtype=np.uint8)
    grid = pv.UnstructuredGrid(cells, celltypes, points)
    grid.cell_data["region"] = np.ones(grid.n_cells, dtype=np.int32)
    quality = volume_quality(grid)
    if quality.n_inverted:
        q = grid.cell_quality(["volume"])
        flip = np.asarray(q["volume"], dtype=float) <= 0
        tets[flip] = tets[flip][:, [0, 1, 3, 2]]
        cells = np.column_stack([np.full(len(tets), 4, dtype=np.int64), tets]).ravel()
        grid = pv.UnstructuredGrid(cells, celltypes, points)
        grid.cell_data["region"] = np.ones(grid.n_cells, dtype=np.int32)
        quality = volume_quality(grid)
        quality.notes.append(f"reoriented {int(flip.sum())} negatively-wound tets from gmsh")

    boundary = TetBackend._classify_boundary(grid, shell_pts, shell_tris, patch_labels)
    notes = [
        f"tets between shell pair ({len(in_pts)}+{len(out_pts)} shell nodes, "
        f"mesh_size={mesh_size:.4g})",
        opt_note,
    ]
    notes.extend(quality.notes)
    return WallMesh(grid=grid, boundary=boundary, backend="tet", quality=quality, notes=notes)


class TetBackend:
    """Unstructured tetrahedra via gmsh on the discrete wall shell.

    The wall boundary is handed to gmsh as-is (discrete surface loop → volume)
    — no surface reparametrization, which is exactly the fragile step trap #9
    warns about. gmsh is optional: importing it happens here, at run time.
    """

    name = "tet"

    def mesh_wall(
        self,
        surface: pv.PolyData,
        thickness: float,
        *,
        centerline=None,
        n_theta: int = 32,
        n_axial: int = 60,
        mesh_size: float | None = None,
        optimize: bool = True,
    ) -> WallMesh:
        from digitwin.meshing.tetfill import fill_shells, require_gmsh

        require_gmsh("wall")  # fail before the lattice build, as before

        lattice = build_wall_lattice(
            surface,
            thickness,
            centerline=centerline,
            n_theta=n_theta,
            n_axial=n_axial,
            n_layers=1,  # only the shell is needed here
        )
        shell_pts, shell_tris, patch_labels = lattice.boundary_shell()
        # The shell references only its own boundary nodes; compact them.
        used = np.unique(shell_tris)
        remap = np.full(len(shell_pts), -1, dtype=np.int64)
        remap[used] = np.arange(len(used))
        shell_pts = shell_pts[used]
        shell_tris = remap[shell_tris]

        filled = fill_shells(
            [(shell_pts, shell_tris)], mesh_size or 0.8 * lattice.thickness,
            engine="gmsh", site="wall",
            optimize=optimize, labels=patch_labels, closed_s=lattice.closed_s,
        )
        points, tets, opt_note = filled.points, filled.tets, filled.opt_note

        cells = np.column_stack([np.full(len(tets), 4, dtype=np.int64), tets]).ravel()
        celltypes = np.full(len(tets), pv.CellType.TETRA, dtype=np.uint8)
        grid = pv.UnstructuredGrid(cells, celltypes, points)
        grid.cell_data["region"] = np.ones(grid.n_cells, dtype=np.int32)

        quality = volume_quality(grid)
        # gmsh may emit negatively-oriented tets; flip them once, loudly.
        if quality.n_inverted:
            q = grid.cell_quality(["volume"])
            flip = np.asarray(q["volume"], dtype=float) <= 0
            tets[flip] = tets[flip][:, [0, 1, 3, 2]]
            cells = np.column_stack([np.full(len(tets), 4, dtype=np.int64), tets]).ravel()
            grid = pv.UnstructuredGrid(cells, celltypes, points)
            grid.cell_data["region"] = np.ones(grid.n_cells, dtype=np.int32)
            quality = volume_quality(grid)
            quality.notes.append(f"reoriented {int(flip.sum())} negatively-wound tets from gmsh")

        boundary = self._classify_boundary(grid, shell_pts, shell_tris, patch_labels)
        notes = [
            f"gmsh tets on discrete shell ({len(shell_pts)} shell nodes, "
            f"mesh_size={mesh_size or 0.8 * lattice.thickness:.4g})",
            opt_note,
        ]
        notes.extend(lattice.notes)
        notes.extend(quality.notes)
        return WallMesh(
            grid=grid, boundary=boundary, backend=self.name, quality=quality, notes=notes
        )

    @staticmethod
    def _classify_boundary(
        grid: pv.UnstructuredGrid,
        shell_pts: np.ndarray,
        shell_tris: np.ndarray,
        patch_labels: np.ndarray,
    ) -> dict[str, BoundaryGroup]:
        """Label tet boundary faces by the nearest original shell patch."""
        shell_centroids = shell_pts[shell_tris].mean(axis=1)
        tree = cKDTree(shell_centroids)

        # algorithm pinned: the vtkOriginalCellIds/PointIds arrays below only
        # come from the dataset-surface filter, and pyvista >=0.48 warns the
        # unset default will change (kwarg exists since pyvista 0.47 — the
        # declared lower bound).
        surf = grid.extract_surface(algorithm="dataset_surface")
        face_pts = surf.points
        faces = surf.faces.reshape(-1, 4)[:, 1:]
        centroids = face_pts[faces].mean(axis=1)
        _, nearest = tree.query(centroids)
        labels = patch_labels[nearest]

        orig_cell = np.asarray(surf.cell_data["vtkOriginalCellIds"], dtype=np.int64)
        orig_point = np.asarray(surf.point_data["vtkOriginalPointIds"], dtype=np.int64)
        face_nodes = orig_point[faces]

        groups: dict[str, BoundaryGroup] = {}
        for label, name in _LABEL_NAMES.items():
            sel = labels == label
            if not sel.any():  # closed-loop shells have no inlet/outlet (V9)
                continue
            groups[name] = BoundaryGroup(
                name=name,
                cell_ids=orig_cell[sel],
                face_nodes=face_nodes[sel],
            )
        return groups

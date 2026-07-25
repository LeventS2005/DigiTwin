from __future__ import annotations

import numpy as np
import pyvista as pv

from meshforge.meshing.base import BoundaryGroup, WallMesh
from meshforge.meshing.lattice import WallLattice, build_wall_lattice
from meshforge.quality.volume import volume_quality


class SweptBackend:
    """Native structured hexahedra: stations × periodic θ ring × radial layers."""

    name = "swept"

    def mesh_wall(
        self,
        surface: pv.PolyData,
        thickness: float,
        *,
        centerline=None,
        n_theta: int = 32,
        n_axial: int = 60,
        n_layers: int = 4,
        layer_ratio: float = 1.0,
        curvature_cap: float = 0.5,
        floor_frac: float = 0.2,
    ) -> WallMesh:
        lattice = build_wall_lattice(
            surface,
            thickness,
            centerline=centerline,
            n_theta=n_theta,
            n_axial=n_axial,
            n_layers=n_layers,
            layer_ratio=layer_ratio,
            curvature_cap=curvature_cap,
            floor_frac=floor_frac,
        )
        s, t, layers = lattice.shape
        points = lattice.points()
        nid = lattice.node_id
        closed = lattice.closed_s
        n_seg = s if closed else s - 1  # periodic in s on closed loops (V9)

        cells = np.empty(n_seg * t * layers * 9, dtype=np.int64)
        k = 0
        for i in range(n_seg):
            i1 = (i + 1) % s
            for j in range(t):
                for l in range(layers):
                    # Bottom quad ordered so (p1−p0)×(p3−p0)·(p4−p0) > 0.
                    cells[k : k + 9] = [
                        8,
                        nid(i, j, l),
                        nid(i, j + 1, l),
                        nid(i1, j + 1, l),
                        nid(i1, j, l),
                        nid(i, j, l + 1),
                        nid(i, j + 1, l + 1),
                        nid(i1, j + 1, l + 1),
                        nid(i1, j, l + 1),
                    ]
                    k += 9
        celltypes = np.full(n_seg * t * layers, pv.CellType.HEXAHEDRON, dtype=np.uint8)
        grid = pv.UnstructuredGrid(cells, celltypes, points)
        grid.cell_data["region"] = np.ones(grid.n_cells, dtype=np.int32)

        def cell_id(i: int, j: int, l: int) -> int:
            return (i * t + (j % t)) * layers + l

        def group(name: str, items: list[tuple[int, tuple[int, int, int, int]]]) -> BoundaryGroup:
            return BoundaryGroup(
                name=name,
                cell_ids=np.array([c for c, _ in items], dtype=np.int64),
                face_nodes=np.array([f for _, f in items], dtype=np.int64),
            )

        inner = [
            (
                cell_id(i, j, 0),
                (nid(i, j, 0), nid(i, j + 1, 0), nid((i + 1) % s, j + 1, 0), nid((i + 1) % s, j, 0)),
            )
            for i in range(n_seg)
            for j in range(t)
        ]
        outer = [
            (
                cell_id(i, j, layers - 1),
                (
                    nid(i, j, layers),
                    nid((i + 1) % s, j, layers),
                    nid((i + 1) % s, j + 1, layers),
                    nid(i, j + 1, layers),
                ),
            )
            for i in range(n_seg)
            for j in range(t)
        ]

        boundary = {"inner": group("inner", inner), "outer": group("outer", outer)}
        if not closed:  # closed loops have no end faces
            inlet = [
                (cell_id(0, j, l), (nid(0, j, l), nid(0, j + 1, l), nid(0, j + 1, l + 1), nid(0, j, l + 1)))
                for j in range(t)
                for l in range(layers)
            ]
            outlet = [
                (
                    cell_id(s - 2, j, l),
                    (nid(s - 1, j, l), nid(s - 1, j, l + 1), nid(s - 1, j + 1, l + 1), nid(s - 1, j + 1, l)),
                )
                for j in range(t)
                for l in range(layers)
            ]
            boundary["inlet"] = group("inlet", inlet)
            boundary["outlet"] = group("outlet", outlet)
        quality = volume_quality(grid)
        notes = [
            f"lattice {s} stations × {t} azimuth × {layers} layers"
            f"{' (periodic s)' if closed else ''}, "
            f"nominal thickness {lattice.thickness:.4g}, layer_ratio {layer_ratio:g}"
        ]
        notes.extend(lattice.notes)
        notes.extend(quality.notes)
        return WallMesh(
            grid=grid, boundary=boundary, backend=self.name, quality=quality, notes=notes
        )

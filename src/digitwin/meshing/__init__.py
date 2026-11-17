"""Volume meshing: swept/tet wall backends + rectilinear multi-block (V5)."""
from __future__ import annotations

from digitwin.meshing.base import (
    BoundaryGroup,
    WallMesh,
    get_backend,
    register_backend,
    split_boundary_group,
    valid_group_name,
)
from digitwin.meshing.blocks import BlockMesh2D, build_block_mesh, extrude_blocks
from digitwin.meshing.lattice import WallLattice, build_wall_lattice
from digitwin.meshing.ogrid import ogrid_disk
from digitwin.meshing.project import project_profile, quad_scaled_jacobians
from digitwin.meshing.swept import SweptBackend
from digitwin.meshing.tet import TetBackend

register_backend("swept", SweptBackend)
register_backend("tet", TetBackend)

__all__ = [
    "BlockMesh2D",
    "BoundaryGroup",
    "SweptBackend",
    "TetBackend",
    "WallLattice",
    "WallMesh",
    "build_block_mesh",
    "build_wall_lattice",
    "extrude_blocks",
    "get_backend",
    "ogrid_disk",
    "project_profile",
    "quad_scaled_jacobians",
    "register_backend",
    "split_boundary_group",
    "valid_group_name",
]

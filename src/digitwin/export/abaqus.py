from __future__ import annotations

from pathlib import Path

import numpy as np
import pyvista as pv

from digitwin.export.neutral import guard_output_path
from digitwin.meshing.base import WallMesh

# Element formulations. Default hybrid: fully-integrated C3D8/C3D4
# volumetrically LOCK at near-incompressible nu (trap #11). Caveats baked in:
# CalculiX has no hybrid library (solve there with full/reduced), and meshio
# cannot read a C3D4H deck back (validate hybrid decks as text).
_ELEMENT_TYPES: dict[tuple[str, str], str] = {
    ("hexahedron", "full"): "C3D8",
    ("hexahedron", "reduced"): "C3D8R",
    ("hexahedron", "hybrid"): "C3D8H",
    ("tetra", "full"): "C3D4",
    ("tetra", "hybrid"): "C3D4H",
}

# Abaqus face labels by LOCAL node ids (0-based here, deck is 1-based).
# Node ordering of VTK and Abaqus solid elements coincides.
_HEX_FACES: dict[str, tuple[int, ...]] = {
    "S1": (0, 1, 2, 3),
    "S2": (4, 7, 6, 5),
    "S3": (0, 4, 5, 1),
    "S4": (1, 5, 6, 2),
    "S5": (2, 6, 7, 3),
    "S6": (3, 7, 4, 0),
}
_TET_FACES: dict[str, tuple[int, ...]] = {
    "S1": (0, 1, 2),
    "S2": (0, 3, 1),
    "S3": (1, 3, 2),
    "S4": (2, 3, 0),
}


def _element_kind(grid: pv.UnstructuredGrid) -> tuple[str, np.ndarray]:
    celltypes = set(int(t) for t in np.unique(grid.celltypes))
    if celltypes == {int(pv.CellType.HEXAHEDRON)}:
        return "hexahedron", grid.cells.reshape(-1, 9)[:, 1:]
    if celltypes == {int(pv.CellType.TETRA)}:
        return "tetra", grid.cells.reshape(-1, 5)[:, 1:]
    raise ValueError(
        f"Abaqus export supports single-type hex or tet meshes, got cell types {celltypes}"
    )


def _face_label(conn_row: np.ndarray, face_nodes: np.ndarray, kind: str) -> str:
    """Match a boundary face to its Abaqus face label by node SET —
    winding-robust on purpose (trap #12)."""
    target = set(int(n) for n in face_nodes)
    faces = _HEX_FACES if kind == "hexahedron" else _TET_FACES
    for label, local in faces.items():
        if {int(conn_row[i]) for i in local} == target:
            return label
    raise ValueError(
        f"boundary face {sorted(target)} is not a face of its parent element "
        f"{[int(n) for n in conn_row]}; boundary group is inconsistent"
    )


def mesh_lines(wall: WallMesh, formulation: str) -> tuple[list[str], str, str, np.ndarray]:
    """Shared *NODE/*ELEMENT/ELSET/NSET/*SURFACE deck block.

    Used by both ``export_abaqus`` and the V2 deck builder — the block must
    stay identical for both callers. Returns (lines, eltype, kind, conn).
    """
    grid = wall.grid
    kind, conn = _element_kind(grid)
    try:
        eltype = _ELEMENT_TYPES[(kind, formulation)]
    except KeyError:
        raise ValueError(
            f"no {formulation!r} formulation for {kind} elements; "
            f"available: {sorted(f for k, f in _ELEMENT_TYPES if k == kind)}"
        ) from None

    lines: list[str] = ["*NODE"]
    for i, (x, y, z) in enumerate(np.asarray(grid.points, dtype=float), start=1):
        lines.append(f"{i}, {x:.10g}, {y:.10g}, {z:.10g}")

    lines.append(f"*ELEMENT, TYPE={eltype}, ELSET=WALL")
    for eid, row in enumerate(conn, start=1):
        lines.append(f"{eid}, " + ", ".join(str(int(n) + 1) for n in row))

    region = np.asarray(grid.cell_data["region"])
    for tag in np.unique(region):
        ids = np.nonzero(region == tag)[0] + 1
        lines.append(f"*ELSET, ELSET=REGION{int(tag)}")
        lines.extend(_chunk_ids(ids))

    for name, group in wall.boundary.items():
        nodes = np.unique(group.face_nodes) + 1
        lines.append(f"*NSET, NSET={name.upper()}")
        lines.extend(_chunk_ids(nodes))

    for name, group in wall.boundary.items():
        lines.append(f"*SURFACE, NAME={name.upper()}_SURF, TYPE=ELEMENT")
        for cid, fnodes in zip(group.cell_ids, group.face_nodes):
            label = _face_label(conn[cid], fnodes, kind)
            lines.append(f"{int(cid) + 1}, {label}")
    return lines, eltype, kind, conn


def export_abaqus(
    wall: WallMesh,
    out_path: str | Path,
    *,
    formulation: str = "hybrid",
    material: dict | None = None,
    uniaxial_benchmark: dict | None = None,
    input_path: str | Path | None = None,
) -> Path:
    """Write an Abaqus .inp: nodes, elements, ELSET/NSETs, and hand-written
    element-based *SURFACE blocks (meshio writes none — trap #12).

    ``material``: {"name","E","nu"} adds *MATERIAL/*ELASTIC/*SOLID SECTION.
    ``uniaxial_benchmark``: {"sigma": s} adds a *STEP applying tension s via
    *DSLOAD on the outlet surface, with inlet held axially plus minimal
    rigid-body constraints (a pure uniaxial state: tip u_axial = sigma·L/E).
    """
    grid = wall.grid
    body, eltype, _kind, _conn = mesh_lines(wall, formulation)

    out = Path(out_path).with_suffix(".inp")
    guard_output_path(out, input_paths=[input_path])
    out.parent.mkdir(parents=True, exist_ok=True)

    lines: list[str] = ["*HEADING", f"meshforge wall export ({wall.backend}, {eltype})"]
    lines.extend(body)

    if material is not None:
        name = material.get("name", "MAT")
        lines += [
            f"*MATERIAL, NAME={name}",
            "*ELASTIC",
            f"{material['E']:.10g}, {material['nu']:.10g}",
            f"*SOLID SECTION, ELSET=WALL, MATERIAL={name}",
        ]

    if uniaxial_benchmark is not None:
        if material is None:
            raise ValueError("uniaxial_benchmark requires a material")
        sigma = float(uniaxial_benchmark["sigma"])
        inlet_nodes = np.unique(wall.boundary["inlet"].face_nodes)
        pts = np.asarray(grid.points)
        # Minimal rigid-body suppression: axial hold on the whole inlet,
        # lateral pins on two far-apart inlet nodes (keeps the state uniaxial;
        # a full encastre would add spurious end stiffening).
        a = int(inlet_nodes[0])
        rest = inlet_nodes[1:]
        b = int(rest[np.argmax(np.linalg.norm(pts[rest] - pts[a], axis=1))])
        lines += [
            "*STEP",
            "*STATIC",
            "*BOUNDARY",
            "INLET, 3, 3",
            f"{a + 1}, 1, 2",
            f"{b + 1}, 2, 2",
            "*DSLOAD",
            f"OUTLET_SURF, P, {-sigma:.10g}",
            "*NODE PRINT, NSET=OUTLET",
            "U",
            "*END STEP",
        ]

    out.write_text("\n".join(lines) + "\n")
    return out


def _chunk_ids(ids: np.ndarray, per_line: int = 16) -> list[str]:
    return [
        ", ".join(str(int(v)) for v in ids[i : i + per_line])
        for i in range(0, len(ids), per_line)
    ]

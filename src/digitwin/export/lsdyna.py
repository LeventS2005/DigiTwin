"""LS-DYNA keyword deck export (V33): linear-static solid decks.

Reuses the format-neutral spec dataclasses from ``deck.py`` (Material,
BoundaryCondition, Load, Step, DeckSpec with ``target="lsdyna"``);
``write_deck`` validates and dispatches here. Layout choices:

- Classic fixed small format: *NODE is I8 + 3×E16 (LS-PrePost layout),
  *ELEMENT_SOLID is ten I8 fields, control cards are I10/10-char fields.
- Tets are written as degenerate solids (n4 repeated into n5..n8 — the
  keyword manual's collapsed-hex convention).
- One *PART per region tag (pid == tag, mirrors the REGIONn ELSETs),
  all referencing SECID 1 / MID 1. *SECTION_SOLID ELFORM maps from the
  formulation: hex reduced→1 (constant stress, the LS-DYNA default),
  hex full→2 (fully integrated S/R); tet reduced→10, tet full→16.
- *MAT_ELASTIC needs RO: a missing density is written as 0.0 with a
  note (mass-free static deck), never invented.
- Zero-valued BCs become per-node *BOUNDARY_SPC_NODE flag cards.
  Nonzero enforced motion needs *BOUNDARY_PRESCRIBED_MOTION with load
  curves — not wired, refused loudly in DeckSpec.validate.
- Loads reference one unit ramp *DEFINE_CURVE (LCID 1, (0,0)→(1,1)):
  *LOAD_NODE_POINT per node for concentrated loads, *LOAD_SEGMENT per
  boundary face for pressure. Segments are wound so the n1..n4
  right-hand normal points OUT of the body; LS-DYNA's positive segment
  pressure acts opposite that normal, i.e. onto the surface — the same
  sense as *DSLOAD, so magnitudes map through unchanged.

NO LS-DYNA solver is installed: validation is format-level only
(internal re-parse + golden cards), exactly the honest V15 pre-MYSTRAN
position (D29); a solve-level benchmark is BACKLOG'd until a
consumer is available.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np

from digitwin.export.abaqus import _element_kind
from digitwin.export.neutral import guard_output_path
from digitwin.meshing.base import WallMesh

_ELFORM = {
    ("hexahedron", "reduced"): 1,
    ("hexahedron", "full"): 2,
    ("tetra", "reduced"): 10,
    ("tetra", "full"): 16,
}


def _f10(v: float) -> str:
    """A float in a 10-char control-card field. Loud when it cannot fit."""
    for prec in range(9, -1, -1):
        s = f"{v:.{prec}G}"
        if len(s) <= 10:
            return f"{s:>10s}"
    raise ValueError(f"value {v!r} does not fit an LS-DYNA 10-char field")


def _outward_face(face: np.ndarray, cell_id: int, wall: WallMesh) -> list[int]:
    """Order the face nodes so the n1..n4 right-hand normal points away
    from the parent cell's centroid (segment winding is load-bearing:
    LS-DYNA pressure sign follows it)."""
    pts = np.asarray(wall.grid.points, dtype=float)
    quad = pts[face]
    if len(face) == 4:
        avec = 0.5 * np.cross(quad[2] - quad[0], quad[3] - quad[1])
    else:
        avec = 0.5 * np.cross(quad[1] - quad[0], quad[2] - quad[0])
    center = wall.grid.get_cell(int(cell_id)).points.mean(axis=0)
    ordered = [int(n) for n in face]
    if np.dot(avec, quad.mean(axis=0) - center) < 0:
        ordered = ordered[::-1]
    return ordered


def write_lsdyna_deck(
    wall: WallMesh,
    spec,  # DeckSpec, target="lsdyna" (validated by write_deck)
    out_path: str | Path,
    *,
    input_path: str | Path | None = None,
    notes: list[str] | None = None,
) -> Path:
    grid = wall.grid
    kind, conn = _element_kind(grid)
    region = np.asarray(grid.cell_data["region"], dtype=int)
    elform = _ELFORM[(kind, spec.resolved_formulation())]

    out = Path(out_path).with_suffix(".k")
    guard_output_path(out, input_paths=[input_path])
    out.parent.mkdir(parents=True, exist_ok=True)

    notes = list(notes or [])
    lines: list[str] = [
        "*KEYWORD",
        "*TITLE",
        spec.heading
        or f"digitwin deck ({wall.backend}, {kind}, target=lsdyna)",
        "$ UNITS: length=%s, force=%s -> stress = %s/%s^2"
        % (spec.length_unit, spec.force_unit, spec.force_unit, spec.length_unit),
        "$ format-level export only: no LS-DYNA solver validated this deck (D29)",
    ]
    if spec.material.density is None:
        notes.append("density not set: *MAT_ELASTIC RO written as 0.0")
    for note in notes:
        lines.append(f"$ NOTE: {note}")

    lines.append("*NODE")
    for i, (x, y, z) in enumerate(np.asarray(grid.points, dtype=float), start=1):
        lines.append(f"{i:8d}{x:16.9G}{y:16.9G}{z:16.9G}")

    lines.append("*ELEMENT_SOLID")
    for eid, row in enumerate(conn, start=1):
        g = [int(n) + 1 for n in row]
        if kind == "tetra":
            g = g + [g[3]] * 4  # collapsed-hex tet convention
        lines.append(f"{eid:8d}{int(region[eid - 1]):8d}" + "".join(f"{n:8d}" for n in g))

    lines.append("*SECTION_SOLID")
    lines.append(f"{1:10d}{elform:10d}")
    e, nu = spec.material.params
    lines.append("*MAT_ELASTIC")
    lines.append(
        f"{1:10d}"
        + _f10(spec.material.density or 0.0)
        + _f10(e)
        + _f10(nu)
    )
    for tag in np.unique(region):
        lines.append("*PART")
        lines.append(f"region {int(tag)}")
        lines.append(f"{int(tag):10d}{1:10d}{1:10d}")

    by_upper = {n.upper(): g for n, g in wall.boundary.items()}
    if spec.boundary_conditions:
        lines.append("*BOUNDARY_SPC_NODE")
        for bc in spec.boundary_conditions:
            last = bc.first_dof if bc.last_dof is None else bc.last_dof
            flags = [1 if bc.first_dof <= d <= last else 0 for d in (1, 2, 3)]
            if isinstance(bc.target, (int, np.integer)):
                nodes = np.array([int(bc.target)])
            else:
                nodes = np.unique(by_upper[str(bc.target).upper()].face_nodes)
            for n in nodes:
                lines.append(
                    f"{int(n) + 1:10d}{0:10d}"
                    + "".join(f"{f:10d}" for f in flags)
                    + f"{0:10d}{0:10d}{0:10d}"
                )

    if spec.loads:
        lines.append("*DEFINE_CURVE")
        lines.append(f"{1:10d}")
        lines.append(f"{0.0:20.9G}{0.0:20.9G}")
        lines.append(f"{1.0:20.9G}{1.0:20.9G}")
    cloads = [ld for ld in spec.loads if ld.kind == "cload"]
    if cloads:
        lines.append("*LOAD_NODE_POINT")
        for ld in cloads:
            if isinstance(ld.target, (int, np.integer)):
                nodes = np.array([int(ld.target)])
            else:
                nodes = np.unique(by_upper[str(ld.target).upper()].face_nodes)
            for n in nodes:
                lines.append(
                    f"{int(n) + 1:10d}{ld.dof:10d}{1:10d}" + _f10(ld.magnitude)
                )
    pressures = [ld for ld in spec.loads if ld.kind == "pressure"]
    if pressures:
        lines.append("*LOAD_SEGMENT")
        for ld in pressures:
            group = by_upper[str(ld.target).upper()]
            for cid, fnodes in zip(group.cell_ids, group.face_nodes):
                seg = _outward_face(np.asarray(fnodes), int(cid), wall)
                if len(seg) == 3:
                    seg = seg + [seg[2]]  # tri segment: n4 repeats n3
                lines.append(
                    f"{1:10d}"
                    + _f10(ld.magnitude)
                    + _f10(0.0)
                    + "".join(f"{int(n) + 1:10d}" for n in seg)
                )

    lines.append("*END")
    out.write_text("\n".join(lines) + "\n")
    return out

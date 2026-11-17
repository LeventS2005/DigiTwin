"""NASTRAN bulk-data deck export (V15): SOL 101 linear statics.

Reuses the format-neutral spec dataclasses from ``deck.py`` (Material,
BoundaryCondition, Load, Step, DeckSpec with ``target="nastran"``);
``write_deck`` validates and dispatches here. Layout choices:

- ``GRID*`` large-field cards (16-char fields) so coordinates keep ~9
  significant digits; everything else is classic small-field (8-char).
- One ``PSOLID`` per region tag (mirrors the REGIONn ELSETs elsewhere),
  all referencing the single ``MAT1``.
- ``SPC1`` cards carry at most six grids and repeat the SID — legal, and
  it avoids continuation-marker bookkeeping. Nonzero enforced motion is
  a per-node ``SPC`` card.
- ``PLOAD4`` uses the solid-face form: owner element + G1 + G3 (the
  diagonally opposite corner of the quad face) for CHEXA, G1 + the
  corner OFF the face for CTETRA. Positive pressure acts INWARD on solid
  faces (MSC QRG) — the same "onto the surface" sense as *DSLOAD, so
  magnitudes map through unchanged.

No NASTRAN-compatible solver is installed, so validation is format-level
(meshio read-back + golden cards) — recorded in D29; a solve-level
benchmark is BACKLOG'd until a solver is available.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np

from digitwin.export.abaqus import _element_kind
from digitwin.export.neutral import guard_output_path
from digitwin.meshing.base import WallMesh

_HEX_QUAD_DIAGONAL = 2  # G3 is two corners along the quad's perimeter


def _f8(v: float) -> str:
    """A float in <= 8 characters that NASTRAN small field accepts
    (must contain '.' or an exponent). Loud when it cannot be done."""
    for prec in range(6, -1, -1):
        s = f"{v:.{prec}G}"
        if "E" not in s and "." not in s:
            s += "."
        if len(s) <= 8:
            return s
    raise ValueError(f"value {v!r} does not fit a NASTRAN 8-char field")


def _small(*fields: object) -> str:
    """Small-field card: 8-char columns, first field is the card name.
    Overflowing a column would silently corrupt the deck — refuse instead."""
    parts = []
    for f in fields:
        s = str(f)
        if len(s) > 8:
            raise ValueError(f"field {s!r} exceeds the NASTRAN 8-char column")
        parts.append(f"{s:<8s}")
    return "".join(parts).rstrip()


def _grid_large(nid: int, xyz: np.ndarray) -> list[str]:
    cont = f"*G{nid}"
    if len(cont) > 8:
        raise ValueError(f"node id {nid} overflows the continuation field")
    return [
        f"{'GRID*':<8s}{nid:<16d}{'':16s}{xyz[0]:<16.9G}{xyz[1]:<16.9G}{cont}",
        f"{cont:<8s}{xyz[2]:<16.9G}",
    ]


def _dof_string(first: int, last: int | None) -> str:
    return "".join(str(d) for d in range(first, (first if last is None else last) + 1))


def write_nastran_deck(
    wall: WallMesh,
    spec,  # DeckSpec, target="nastran" (validated by write_deck)
    out_path: str | Path,
    *,
    input_path: str | Path | None = None,
    notes: list[str] | None = None,
) -> Path:
    grid = wall.grid
    kind, conn = _element_kind(grid)
    region = np.asarray(grid.cell_data["region"], dtype=int)

    out = Path(out_path).with_suffix(".bdf")
    guard_output_path(out, input_paths=[input_path])
    out.parent.mkdir(parents=True, exist_ok=True)

    lines: list[str] = [
        "SOL 101",
        "CEND",
        f"TITLE = {spec.heading or f'digitwin deck ({wall.backend}, {kind}, target=nastran)'}",
        "$ UNITS: length=%s, force=%s -> stress = %s/%s^2"
        % (spec.length_unit, spec.force_unit, spec.force_unit, spec.length_unit),
        "SPC = 1",
        "LOAD = 1",
        "DISPLACEMENT(PRINT) = ALL",  # PRINT: MYSTRAN F06 read-back (V25)
        "STRESS(PLOT) = ALL",
    ]
    for note in notes or []:
        lines.append(f"$ NOTE: {note}")
    lines.append("BEGIN BULK")

    for i, p in enumerate(np.asarray(grid.points, dtype=float), start=1):
        lines.extend(_grid_large(i, p))

    if kind == "hexahedron":
        for eid, row in enumerate(conn, start=1):
            g = [int(n) + 1 for n in row]
            cont = f"+E{eid}"
            lines.append(_small("CHEXA", eid, int(region[eid - 1]), *g[:6], cont))
            lines.append(_small(cont, *g[6:]))
    else:  # tetra (single-type enforced by _element_kind)
        for eid, row in enumerate(conn, start=1):
            g = [int(n) + 1 for n in row]
            lines.append(_small("CTETRA", eid, int(region[eid - 1]), *g))

    e, nu = spec.material.params
    mat = ["MAT1", 1, _f8(e), "", _f8(nu)]
    if spec.material.density is not None:
        mat.append(_f8(spec.material.density))
    lines.append(_small(*mat))
    for tag in np.unique(region):
        lines.append(_small("PSOLID", int(tag), 1))

    for bc in spec.boundary_conditions:
        dofs = _dof_string(bc.first_dof, bc.last_dof)
        if isinstance(bc.target, (int, np.integer)):
            nodes = np.array([int(bc.target)])
        else:
            nodes = np.unique(wall.boundary[str(bc.target)].face_nodes)
        if bc.value == 0.0:
            for i in range(0, len(nodes), 6):
                lines.append(
                    _small("SPC1", 1, dofs, *(int(n) + 1 for n in nodes[i : i + 6]))
                )
        else:
            for n in nodes:
                lines.append(_small("SPC", 1, int(n) + 1, dofs, _f8(bc.value)))

    for load in spec.loads:
        if load.kind == "pressure":
            group = wall.boundary[str(load.target)]
            for cid, fnodes in zip(group.cell_ids, group.face_nodes):
                face = [int(n) for n in fnodes]
                if kind == "hexahedron":
                    # G1 + the diagonally opposite corner of the quad face.
                    g1, g3 = face[0], face[_HEX_QUAD_DIAGONAL]
                else:
                    # G1 on the face + the parent tet's corner OFF the face.
                    off = set(int(n) for n in conn[cid]) - set(face)
                    if len(off) != 1:
                        raise ValueError(
                            f"boundary face {sorted(face)} is not a face of "
                            f"tet {int(cid)}; boundary group is inconsistent"
                        )
                    g1, g3 = face[0], off.pop()
                lines.append(
                    _small(
                        "PLOAD4", 1, int(cid) + 1, _f8(load.magnitude),
                        "", "", "", g1 + 1, g3 + 1,
                    )
                )
        else:  # cload (validated upstream)
            if isinstance(load.target, (int, np.integer)):
                nodes = np.array([int(load.target)])
            else:
                nodes = np.unique(wall.boundary[str(load.target)].face_nodes)
            n_vec = ["0.", "0.", "0."]
            n_vec[load.dof - 1] = "1."
            for n in nodes:
                lines.append(
                    _small("FORCE", 1, int(n) + 1, "", _f8(load.magnitude), *n_vec)
                )

    lines.append("ENDDATA")
    out.write_text("\n".join(lines) + "\n")
    return out

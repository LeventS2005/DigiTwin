"""ANSYS MAPDL coded-database (.cdb) deck export (V44): linear-static solids.

Reuses the format-neutral spec dataclasses from ``deck.py`` (Material,
BoundaryCondition, Load, Step, DeckSpec with ``target="ansys"``);
``write_deck`` validates and dispatches here. Layout choices (all pinned
from primary evidence in the V44 probe — the ANSYS-maintained
ansys-mapdl-reader writer/reader plus real ANSYS/Workbench archives; see
the V44 design notes):

- CDWRITE,DB-style archive: ``/PREP7`` .. ``FINISH`` wrapping ``ET``/
  ``KEYOPT``, ``NBLOCK``/``EBLOCK`` fixed-format blocks, ``CMBLOCK``
  components, then plain ``MP``/``D``/``F``/``SFE`` commands (all legal in
  DB mode).
- NBLOCK is ``(3i9,6e20.13)`` — node id + two solid-model ints (0 when
  meshed directly) + x,y,z; rotation angles omitted. The format line is
  genuinely parsed by readers, not decorative. Terminator is the MAPDL
  idiom ``N,R5.3,LOC,       -1,``.
- EBLOCK is ``(19i9)`` — 11 header ints (mat, ET type ref, real, section,
  esys, birth/death, solid-model ref, shape flag, n_nodes, unused, elem id)
  then 8 node ids, one line per linear solid. The ``SOLID`` key in the
  header line is required by parsers. Terminator ``       -1``.
- One element type for hex AND tet: ``ET,1,185``. Tets are degenerate
  SOLID185 in the official collapse pattern I J K K M M M M. Formulation
  maps to KEYOPT(2): full -> 0 (B-bar, ANSYS's own default), reduced -> 1
  (uniform reduced); mixed u-P (KEYOPT(6)) is not wired — hybrid refuses.
- Region tags become element components ``CMBLOCK,REGION<t>,ELEM`` (the
  ANSYS analogue of the Abaqus REGIONn ELSETs); every boundary group
  becomes a node component. Component names live in ONE case-insensitive
  namespace — collisions and non-ANSYS names (beyond letters/digits/
  underscore, 32 chars) refuse loudly, never mangle.
- BCs are stateless per-node ``D,<node>,<UX|UY|UZ>,<value>`` lines; ANSYS
  carries enforced-displacement values natively, so nonzero BC values are
  allowed (unlike LS-DYNA). Concentrated loads are per-node ``F`` lines.
- Pressure is the native per-face card ``SFE,<elem>,<lkey>,PRES,,<value>``.
  LKEY comes from the SOLID185 face table matched by node SET
  (winding-robust — ANSYS resolves the pressure direction from its own
  face convention, so our node order is not load-bearing). Positive PRES
  acts INTO the face, the same sense as Abaqus *DSLOAD, so magnitudes map
  through unchanged. Degenerate-tet faces keep LKEY 1/2/3/5; 4 and 6
  collapse to lines/points and cannot carry load.

NO ANSYS solver is installed: validation is format-level only (internal
re-parse + golden cards), exactly the honest V15 pre-MYSTRAN position
(D29); a solve-level benchmark is BACKLOG'd until a consumer is available.
"""
from __future__ import annotations

import re
from pathlib import Path

import numpy as np

from digitwin.export.abaqus import _element_kind
from digitwin.export.neutral import guard_output_path
from digitwin.meshing.base import WallMesh

# SOLID185 KEYOPT(2): 0 = full integration with B-bar (the ANSYS default),
# 1 = uniform reduced integration.
_KEYOPT2 = {"full": 0, "reduced": 1}

# SOLID185 pressure faces, LKEY -> local 0-based corner ids (I..P = 0..7).
_ANSYS_HEX_FACES: dict[int, tuple[int, ...]] = {
    1: (0, 1, 2, 3),
    2: (0, 1, 5, 4),
    3: (1, 2, 6, 5),
    4: (2, 3, 7, 6),
    5: (3, 0, 4, 7),
    6: (4, 5, 6, 7),
}
# Degenerate tet (I J K K M M M M): the four triangular faces keep these
# LKEYs; faces 4 and 6 collapse to a line/point and take no load.
_ANSYS_TET_FACES: dict[int, tuple[int, ...]] = {
    1: (0, 1, 2),
    2: (0, 1, 3),
    3: (1, 2, 3),
    5: (0, 2, 3),
}

_DOF_LABEL = {1: "UX", 2: "UY", 3: "UZ"}
_FORCE_LABEL = {1: "FX", 2: "FY", 3: "FZ"}

# ANSYS component names are stricter than meshforge group names.
_COMPONENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,31}$")


def _e20(v: float) -> str:
    """A float in a 20-char NBLOCK field. Loud when it cannot fit (a
    3-digit exponent with a sign overflows the width — never truncate)."""
    s = f"{v:20.13E}"
    if len(s) != 20:
        raise ValueError(f"value {v!r} does not fit a 20-char .cdb float field")
    return s


def _i9(n: int) -> str:
    s = f"{int(n):9d}"
    if len(s) != 9:
        raise ValueError(f"id {n!r} does not fit a 9-char .cdb int field")
    return s


def _g(v: float) -> str:
    """A value in a free-format command field (MP/D/F/SFE)."""
    return f"{float(v):.12G}"


def _face_lkey(conn_row: np.ndarray, face_nodes: np.ndarray, kind: str) -> int:
    """Match a boundary face to its SOLID185 LKEY by node SET —
    winding-robust on purpose (the _face_label pattern, trap #12)."""
    target = set(int(n) for n in face_nodes)
    faces = _ANSYS_HEX_FACES if kind == "hexahedron" else _ANSYS_TET_FACES
    for lkey, local in faces.items():
        if {int(conn_row[i]) for i in local} == target:
            return lkey
    raise ValueError(
        f"boundary face {sorted(target)} is not a loadable face of its "
        f"parent element {[int(n) for n in conn_row]}; boundary group is "
        f"inconsistent"
    )


def _cmblock(name: str, entity: str, ids_1based: list[int]) -> list[str]:
    lines = [f"CMBLOCK,{name},{entity},{len(ids_1based):8d}", "(8i10)"]
    for k in range(0, len(ids_1based), 8):
        lines.append("".join(f"{i:10d}" for i in ids_1based[k : k + 8]))
    return lines


def write_ansys_deck(
    wall: WallMesh,
    spec,  # DeckSpec, target="ansys" (validated by write_deck)
    out_path: str | Path,
    *,
    input_path: str | Path | None = None,
    notes: list[str] | None = None,
) -> Path:
    grid = wall.grid
    kind, conn = _element_kind(grid)
    region = np.asarray(grid.cell_data["region"], dtype=int)
    keyopt2 = _KEYOPT2[spec.resolved_formulation()]

    out = Path(out_path).with_suffix(".cdb")
    guard_output_path(out, input_paths=[input_path])
    out.parent.mkdir(parents=True, exist_ok=True)

    notes = list(notes or [])
    lines: list[str] = [
        "/COM,meshforge ANSYS MAPDL archive (CDWRITE,DB style)",
        "/PREP7",
        "/TITLE,"
        + (spec.heading or f"meshforge deck ({wall.backend}, {kind}, target=ansys)"),
        "! UNITS: length=%s, force=%s -> stress = %s/%s^2"
        % (spec.length_unit, spec.force_unit, spec.force_unit, spec.length_unit),
        "! format-level export only: no ANSYS solver validated this deck (D29)",
    ]
    if spec.material.density is None:
        notes.append("density not set: MP,DENS omitted (mass-free static deck)")
    for name, group in wall.boundary.items():
        if np.asarray(group.face_nodes).size == 0:
            notes.append(f"boundary group {name!r} is empty: no component written")
    for note in notes:
        lines.append(f"! NOTE: {note}")

    lines.append("ET,1,185")
    lines.append(f"KEYOPT,1,2,{keyopt2}")

    pts = np.asarray(grid.points, dtype=float)
    lines.append(f"NBLOCK,6,SOLID,{len(pts):10d},{len(pts):10d}")
    lines.append("(3i9,6e20.13)")
    for i, (x, y, z) in enumerate(pts, start=1):
        lines.append(_i9(i) + _i9(0) + _i9(0) + _e20(x) + _e20(y) + _e20(z))
    lines.append("N,R5.3,LOC,       -1,")

    lines.append(f"EBLOCK,19,SOLID,{len(conn):10d},{len(conn):10d}")
    lines.append("(19i9)")
    for eid, row in enumerate(conn, start=1):
        g = [int(n) + 1 for n in row]
        if kind == "tetra":
            g = [g[0], g[1], g[2], g[2], g[3], g[3], g[3], g[3]]  # I J K K M M M M
        lines.append(
            "".join(_i9(v) for v in (1, 1, 1, 1, 0, 0, 0, 0, 8, 0, eid, *g))
        )
    lines.append(_i9(-1))

    # One case-insensitive namespace for node AND element components:
    # a collision would silently shadow a group inside ANSYS — refuse.
    comp_names: set[str] = set()

    def _component(name: str) -> str:
        up = str(name).upper()
        if not _COMPONENT_RE.match(up):
            raise ValueError(
                f"boundary group {name!r} cannot become an ANSYS component: "
                f"component names allow letters/digits/underscore only, "
                f"max 32 chars — rename the group"
            )
        if up in comp_names:
            raise ValueError(
                f"duplicate ANSYS component name {up!r}: node and element "
                f"components share one case-insensitive namespace — rename "
                f"the boundary group"
            )
        comp_names.add(up)
        return up

    for tag in np.unique(region):
        eids = [int(e) + 1 for e in np.nonzero(region == tag)[0]]
        lines += _cmblock(_component(f"REGION{int(tag)}"), "ELEM", eids)

    by_upper = {n.upper(): g for n, g in wall.boundary.items()}
    for name, group in wall.boundary.items():
        if np.asarray(group.face_nodes).size == 0:
            continue  # empty group: noted in the header, nothing to select
        nids = [int(n) + 1 for n in np.unique(group.face_nodes)]
        lines += _cmblock(_component(name), "NODE", nids)

    e, nu = spec.material.params
    lines.append(f"MP,EX,1,{_g(e)}")
    lines.append(f"MP,PRXY,1,{_g(nu)}")
    if spec.material.density is not None:
        lines.append(f"MP,DENS,1,{_g(spec.material.density)}")

    for bc in spec.boundary_conditions:
        last = bc.first_dof if bc.last_dof is None else bc.last_dof
        if isinstance(bc.target, (int, np.integer)):
            nodes = np.array([int(bc.target)])
        else:
            nodes = np.unique(by_upper[str(bc.target).upper()].face_nodes)
        for n in nodes:
            for dof in range(bc.first_dof, last + 1):
                lines.append(f"D,{int(n) + 1},{_DOF_LABEL[dof]},{_g(bc.value)}")

    for ld in (ld for ld in spec.loads if ld.kind == "cload"):
        if isinstance(ld.target, (int, np.integer)):
            nodes = np.array([int(ld.target)])
        else:
            nodes = np.unique(by_upper[str(ld.target).upper()].face_nodes)
        for n in nodes:
            lines.append(f"F,{int(n) + 1},{_FORCE_LABEL[ld.dof]},{_g(ld.magnitude)}")

    for ld in (ld for ld in spec.loads if ld.kind == "pressure"):
        group = by_upper[str(ld.target).upper()]
        for cid, fnodes in zip(group.cell_ids, group.face_nodes):
            lkey = _face_lkey(conn[int(cid)], np.asarray(fnodes), kind)
            lines.append(f"SFE,{int(cid) + 1},{lkey},PRES,,{_g(ld.magnitude)}")

    lines.append("FINISH")
    out.write_text("\n".join(lines) + "\n")
    return out

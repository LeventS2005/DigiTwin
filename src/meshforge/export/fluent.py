"""ANSYS FLUENT .msh export (V28): face-based ASCII mesh.

Layout per the FLUENT/TGrid mesh format: ``(2 3)`` dimension, ``(10 …)``
nodes, ``(12 …)`` cells — declaration-only for a single element type,
because a FLUENT mesh is FACE-based: the ``(13 …)`` zones carry
``count n1 … nk c0 c1`` records that define cell connectivity. ALL
indices (section headers, node ids, cell ids, bc/element types) are
HEXADECIMAL and 1-based; 0 means "no cell" on boundary faces.

Orientation convention, probe-verified against OpenFOAM's
fluent3DMeshToFoam (D42): the face nodes are ordered so the
right-hand-rule normal points from c1 to c0. Our extracted faces are
wound OUTWARD of the owner cell, so **c1 = owner, c0 = neighbour**
(0 outside); the wrong pairing converts to cells of NEGATIVE volume, so
the convention is checkable, not cosmetic. Every zone needs a ``(45 …)``
name section — fluent3DMeshToFoam crashes on a nameless CELL zone.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np

from meshforge.export.foam import _extract_faces, _patch_face_map
from meshforge.export.neutral import guard_output_path
from meshforge.meshing.base import WallMesh

# VTK cell type -> FLUENT element type
_FLUENT_CELL = {10: 2, 12: 4, 13: 6}  # tet, hex, wedge
_FLUENT_CELL_NAMES = {10: "tetrahedra", 12: "hexahedra", 13: "wedges"}

# FLUENT boundary-condition types (zone-name word -> id)
_BC_TYPES = {
    "wall": 3,
    "pressure-inlet": 4,
    "pressure-outlet": 5,
    "symmetry": 7,
    "velocity-inlet": 10,
    "outflow": 36,
    "interior": 2,
}


def _bc_word(name: str, bc_types: dict[str, str]) -> str:
    """Boundary zone type word: explicit map first, else the V14-style
    name heuristic (inlet -> velocity-inlet, outlet -> pressure-outlet,
    anything else a wall)."""
    if name in bc_types:
        word = bc_types[name]
        if word not in _BC_TYPES or word == "interior":
            allowed = sorted(w for w in _BC_TYPES if w != "interior")
            raise ValueError(
                f"unknown FLUENT bc type {word!r} for patch {name!r}; "
                f"use one of {allowed}"
            )
        return word
    lowered = name.lower()
    if "inlet" in lowered:
        return "velocity-inlet"
    if "outlet" in lowered:
        return "pressure-outlet"
    return "wall"


def write_fluent(
    wall: WallMesh,
    out_path: str | Path,
    *,
    bc_types: dict[str, str] | None = None,
    default_patch: str | None = None,
    input_path: str | Path | None = None,
) -> Path:
    """Write the wall mesh as an ASCII FLUENT .msh; returns the path.

    ``bc_types`` maps patch names to FLUENT zone types (wall,
    velocity-inlet, pressure-inlet, pressure-outlet, symmetry, outflow);
    unnamed patches follow the inlet/outlet name heuristic, else wall.
    """
    bc_types = dict(bc_types or {})
    out = Path(out_path)
    if out.suffix.lower() != ".msh":
        raise ValueError(f"FLUENT mesh files end in .msh, got {out.name!r}")

    grid = wall.grid
    if grid.n_cells == 0:
        raise ValueError("refusing to write an empty mesh")
    celltypes = set(int(t) for t in np.unique(np.asarray(grid.celltypes)))
    unknown = celltypes - set(_FLUENT_CELL)
    if unknown:
        raise ValueError(
            f"FLUENT export supports hexahedra, tetrahedra and wedges; "
            f"the grid carries VTK cell type(s) {sorted(unknown)}"
        )

    internal, boundary = _extract_faces(grid)
    patch_names, patch_faces = _patch_face_map(wall, boundary, default_patch)
    for name in bc_types:
        if name not in patch_names:
            raise ValueError(
                f"bc_types names unknown patch {name!r}; patches: {patch_names}"
            )

    guard_output_path(out, input_paths=[input_path])

    pts = np.asarray(grid.points, dtype=float)
    n_cells = grid.n_cells
    n_faces = len(internal) + sum(len(v) for v in patch_faces.values())

    lines = [
        '(0 "meshforge FLUENT mesh (V28); indices hexadecimal, 1-based")',
        "(2 3)",
        f"(10 (0 1 {len(pts):x} 0))",
        f"(12 (0 1 {n_cells:x} 0))",
        f"(13 (0 1 {n_faces:x} 0))",
        f"(10 (1 1 {len(pts):x} 1 3)(",
        *(f"{p[0]:.16g} {p[1]:.16g} {p[2]:.16g}" for p in pts),
        "))",
    ]
    if len(celltypes) == 1:
        etype = _FLUENT_CELL[next(iter(celltypes))]
        lines.append(f"(12 (2 1 {n_cells:x} 1 {etype:x}))")
    else:  # mixed element types carry a per-cell type body
        per_cell = [
            f"{_FLUENT_CELL[int(t)]:x}" for t in np.asarray(grid.celltypes)
        ]
        lines += [f"(12 (2 1 {n_cells:x} 1 0)(", " ".join(per_cell), "))"]

    def face_record(verts: tuple, c0: int, c1: int) -> str:
        idx = " ".join(f"{v + 1:x}" for v in verts)
        return f"{len(verts):x} {idx} {c0:x} {c1:x}"

    # Interior zone 3 (bc 2), mixed face-type 0 (count-prefixed records).
    fid = 1
    if internal:
        lines.append(f"(13 (3 {fid:x} {fid + len(internal) - 1:x} 2 0)(")
        # owner-outward winding: normal points from c1 (owner) to c0.
        lines += [face_record(v, n + 1, o + 1) for o, n, v in internal]
        lines.append("))")
        fid += len(internal)

    zone_words: list[tuple[int, str, str]] = []  # (zone id, type word, name)
    for zi, name in enumerate(patch_names, start=4):
        keys = sorted(patch_faces[name], key=lambda k: boundary[k][0])
        word = _bc_word(name, bc_types)
        lines.append(
            f"(13 ({zi:x} {fid:x} {fid + len(keys) - 1:x} "
            f"{_BC_TYPES[word]:x} 0)("
        )
        lines += [
            face_record(boundary[k][1], 0, boundary[k][0] + 1) for k in keys
        ]
        lines.append("))")
        zone_words.append((zi, word, name))
        fid += len(keys)

    lines.append("(45 (2 fluid fluid)())")
    if internal:
        lines.append("(45 (3 interior interior-faces)())")
    lines += [f"(45 ({zi:x} {word} {name})())" for zi, word, name in zone_words]

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n")
    return out

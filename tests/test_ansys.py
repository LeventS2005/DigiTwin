"""V44 ANSYS MAPDL .cdb deck export — FORMAT-LEVEL validation only.

No ANSYS solver exists here (the honest V15 pre-MYSTRAN position, D29):
golden cards pin the NBLOCK/EBLOCK/CMBLOCK fixed-column layout, an
internal re-parse rebuilds the mesh from the written blocks and must
reproduce the source grid exactly, SFE face keys are checked against the
SOLID185 face table. Loud refusals for everything the writer does not map.
"""
from __future__ import annotations

import numpy as np
import pytest
import pyvista as pv

from digitwin.export.deck import (
    BoundaryCondition,
    DeckSpec,
    Load,
    Material,
    Step,
    write_deck,
)

# ---------------------------------------------------------------------------
# fixtures


def _bar_wall(n_cross: int = 2, n_layers: int = 2, length: float = 2.0):
    from digitwin.meshing import build_block_mesh, extrude_blocks

    rect = np.array([[0, 0], [1, 0], [1, 1], [0, 1]], dtype=float)
    bm = build_block_mesh([rect], target_size=1.0 / n_cross)
    return extrude_blocks(bm, thickness=length, n_layers=n_layers)


def _tet_wall():
    """Tet mesh via the gmsh backend on the tube (skips without gmsh)."""
    pytest.importorskip("gmsh")
    from digitwin.geometry import compute_centerline
    from digitwin.meshing import get_backend
    from digitwin.synth import generate

    surface, _ = generate("straight_tube")
    cl = compute_centerline(surface)
    return get_backend("tet").mesh_wall(surface, 1.0, centerline=cl)


def _spec(loads=(), bcs=(BoundaryCondition("front", 1, 3),), **kw):
    return DeckSpec(
        material=kw.pop("material", Material.elastic(1000.0, 0.3, density=1e-3)),
        step=kw.pop("step", Step()),
        boundary_conditions=bcs,
        loads=loads,
        target="ansys",
        **kw,
    )


def _parse_cdb(text: str):
    """Re-parse NBLOCK/EBLOCK/CMBLOCK by the fixed columns the writer uses
    ((3i9,6e20.13), (19i9), (8i10))."""
    nodes: dict[int, list[float]] = {}
    elems: list[list[int]] = []
    comps: dict[tuple[str, str], list[int]] = {}
    section = None
    skip_format = False
    comp_key: tuple[str, str] | None = None
    comp_count = 0
    for ln in text.splitlines():
        if skip_format:  # the Fortran format line under a block header
            skip_format = False
            if section == "cmblock" and comp_count == 0:
                section = None
            continue
        if ln.startswith("NBLOCK"):
            section, skip_format = "nblock", True
            continue
        if ln.startswith("EBLOCK"):
            section, skip_format = "eblock", True
            continue
        if ln.startswith("CMBLOCK"):
            _, name, entity, count = ln.split(",")
            comp_key = (name, entity)
            comp_count = int(count)
            comps[comp_key] = []
            section, skip_format = "cmblock", True
            continue
        if section == "nblock":
            if ln.startswith("N,R5.3") or ln.lstrip().startswith("-"):
                section = None
                continue
            nodes[int(ln[:9])] = [
                float(ln[27:47]), float(ln[47:67]), float(ln[67:87])
            ]
        elif section == "eblock":
            if ln.lstrip().startswith("-"):
                section = None
                continue
            elems.append([int(ln[9 * k : 9 * (k + 1)]) for k in range(19)])
        elif section == "cmblock":
            assert comp_key is not None
            comps[comp_key] += [
                int(ln[10 * k : 10 * (k + 1)]) for k in range(len(ln) // 10)
            ]
            if len(comps[comp_key]) >= comp_count:
                section = None
    return nodes, elems, comps


def _rebuild_grid(nodes, elems):
    ids = sorted(nodes)
    assert ids == list(range(1, len(ids) + 1))
    pts = np.array([nodes[i] for i in ids])
    cells, celltypes = [], []
    for fields in elems:
        conn = fields[11:19]
        if conn[2] == conn[3] and len(set(conn[4:])) == 1:
            # degenerate SOLID185 tet: I J K K M M M M
            cells.extend([4, conn[0] - 1, conn[1] - 1, conn[2] - 1, conn[4] - 1])
            celltypes.append(pv.CellType.TETRA)
        else:
            cells.extend([8, *[n - 1 for n in conn]])
            celltypes.append(pv.CellType.HEXAHEDRON)
    return pv.UnstructuredGrid(
        np.asarray(cells), np.asarray(celltypes, dtype=np.uint8), pts
    )


# ---------------------------------------------------------------------------
# golden cards


def test_golden_cards(tmp_path):
    wall = _bar_wall()
    deck = write_deck(
        wall,
        _spec(loads=(
            Load.pressure("back", 2.5),
            Load.concentrated(0, 3, -1.5),
        )),
        tmp_path / "bar",
    )
    assert deck.suffix == ".cdb"
    text = deck.read_text()
    assert text.startswith(
        "/COM,meshforge ANSYS MAPDL archive (CDWRITE,DB style)\n/PREP7\n/TITLE,"
    )
    assert "! format-level export only: no ANSYS solver validated this deck (D29)" in text
    # the empty extrusion group is noted, never a zero-item component
    assert "! NOTE: boundary group 'hole_rims' is empty: no component written" in text
    assert "HOLE_RIMS" not in text
    assert "\nET,1,185\nKEYOPT,1,2,0\n" in text  # default formulation: full
    # NBLOCK: header carries max id + count, format line is written, node
    # line is 3i9 + 3 floats of width 20
    assert "\nNBLOCK,6,SOLID,        27,        27\n(3i9,6e20.13)\n" in text
    assert (
        "\n        1        0        0"
        " 0.0000000000000E+00 0.0000000000000E+00 0.0000000000000E+00\n" in text
    )
    assert "\nN,R5.3,LOC,       -1,\n" in text
    # EBLOCK: 11 header ints (mat=type=real=sec=1, flags 0, nnodes=8, eid)
    # then the 8 node ids = 19 i9 fields on one line
    assert "\nEBLOCK,19,SOLID,         8,         8\n(19i9)\n" in text
    assert (
        "\n        1        1        1        1        0        0        0"
        "        0        8        0        1        1        4        5"
        "        2       10       13       14       11\n" in text
    )
    assert "\n       -1\nCMBLOCK,REGION1,ELEM,       8\n(8i10)\n" in text
    assert (
        "\nCMBLOCK,FRONT,NODE,       9\n(8i10)\n"
        "         1         2         3         4         5         6"
        "         7         8\n         9\n" in text
    )
    assert "\nMP,EX,1,1000\nMP,PRXY,1,0.3\nMP,DENS,1,0.001\n" in text
    assert "\nD,1,UX,0\nD,1,UY,0\nD,1,UZ,0\n" in text
    assert "\nF,1,FZ,-1.5\n" in text
    assert "\nSFE,5,6,PRES,,2.5\n" in text  # 'back' = +z face = LKEY 6
    assert text.rstrip().endswith("FINISH")


def test_reduced_formulation_keyopt_1(tmp_path):
    wall = _bar_wall()
    deck = write_deck(
        wall,
        _spec(loads=(Load.pressure("back", 1.0),), formulation="reduced"),
        tmp_path / "bar",
    )
    assert "\nET,1,185\nKEYOPT,1,2,1\n" in deck.read_text()


def test_missing_density_mp_dens_omitted(tmp_path):
    wall = _bar_wall()
    deck = write_deck(
        wall, _spec(material=Material.elastic(1000.0, 0.3)), tmp_path / "bar"
    )
    text = deck.read_text()
    assert "! NOTE: density not set: MP,DENS omitted (mass-free static deck)" in text
    assert "MP,DENS," not in text  # the card itself; the NOTE line names it
    assert "\nMP,EX,1,1000\nMP,PRXY,1,0.3\n" in text


def test_nonzero_bc_enforced_displacement(tmp_path):
    """Unlike LS-DYNA, ANSYS D carries a nonzero value natively — allowed."""
    wall = _bar_wall()
    spec = _spec(bcs=(
        BoundaryCondition("front", 1, 3),
        BoundaryCondition("back", 3, None, 0.25),
    ))
    spec.validate()  # no refusal
    deck = write_deck(wall, spec, tmp_path / "bar")
    text = deck.read_text()
    assert "\nD,19,UZ,0.25\n" in text


# ---------------------------------------------------------------------------
# internal re-parse: the written blocks must reproduce the mesh exactly


def test_hex_reparse_roundtrip(tmp_path):
    wall = _bar_wall(n_cross=3, n_layers=4)
    deck = write_deck(wall, _spec(), tmp_path / "bar")
    nodes, elems, comps = _parse_cdb(deck.read_text())
    grid = _rebuild_grid(nodes, elems)
    assert grid.n_points == wall.grid.n_points
    assert grid.n_cells == wall.grid.n_cells
    np.testing.assert_allclose(grid.points, wall.grid.points, atol=1e-12)
    src = wall.grid.cells.reshape(-1, 9)[:, 1:]
    out = grid.cells.reshape(-1, 9)[:, 1:]
    np.testing.assert_array_equal(out, src)
    # element ids are written 1..n in grid order
    np.testing.assert_array_equal(
        [fields[10] for fields in elems], np.arange(1, len(elems) + 1)
    )
    # region tags land as REGION<t> element components covering every cell
    region = np.asarray(wall.grid.cell_data["region"], dtype=int)
    for tag in np.unique(region):
        want = (np.nonzero(region == tag)[0] + 1).tolist()
        assert comps[(f"REGION{int(tag)}", "ELEM")] == want
    # boundary groups land as node components
    for name, group in wall.boundary.items():
        if group.face_nodes.size == 0:
            assert (name.upper(), "NODE") not in comps
            continue
        want = (np.unique(group.face_nodes) + 1).tolist()
        assert comps[(name.upper(), "NODE")] == want
    assert grid.volume == pytest.approx(wall.grid.volume, rel=1e-12)


def test_tet_reparse_roundtrip(tmp_path):
    wall = _tet_wall()
    deck = write_deck(
        wall,
        _spec(bcs=(BoundaryCondition("inlet", 1, 3),)),
        tmp_path / "tube",
    )
    text = deck.read_text()
    assert "\nET,1,185\nKEYOPT,1,2,0\n" in text
    nodes, elems, _comps = _parse_cdb(text)
    for fields in elems:  # degenerate-tet convention pinned: I J K K M M M M
        conn = fields[11:19]
        assert conn[3] == conn[2]
        assert conn[5:] == [conn[4]] * 3
    grid = _rebuild_grid(nodes, elems)
    assert grid.n_cells == wall.grid.n_cells
    assert grid.volume == pytest.approx(wall.grid.volume, rel=1e-9)


def test_sfe_faces_cover_group(tmp_path):
    """One SFE per boundary face of the loaded group; each LKEY names the
    SOLID185 face whose node set is exactly the boundary face (LKEY is
    load-bearing: ANSYS applies PRES to that face)."""
    from digitwin.export.ansys import _ANSYS_HEX_FACES

    wall = _bar_wall(n_cross=3, n_layers=4, length=2.0)
    deck = write_deck(
        wall, _spec(loads=(Load.pressure("back", 2.5),)), tmp_path / "bar"
    )
    sfe = [
        ln for ln in deck.read_text().splitlines() if ln.startswith("SFE,")
    ]
    group = wall.boundary["back"]
    assert len(sfe) == len(group.face_nodes)
    conn = wall.grid.cells.reshape(-1, 9)[:, 1:]
    want_faces = {frozenset(int(n) for n in f) for f in group.face_nodes}
    got_faces = set()
    for ln in sfe:
        _, eid, lkey, lab, blank, val = ln.split(",")
        assert lab == "PRES" and blank == "" and float(val) == 2.5
        row = conn[int(eid) - 1]
        got_faces.add(
            frozenset(int(row[i]) for i in _ANSYS_HEX_FACES[int(lkey)])
        )
    assert got_faces == want_faces


# ---------------------------------------------------------------------------
# loud refusals


def test_tet_sfe_faces_cover_group(tmp_path):
    """Pressure on a tet-backend group: degenerate-SOLID185 faces keep
    LKEY 1/2/3/5 only, and each SFE names the face whose node set is
    exactly the boundary face."""
    from digitwin.export.ansys import _ANSYS_TET_FACES

    wall = _tet_wall()
    deck = write_deck(
        wall,
        _spec(
            bcs=(BoundaryCondition("inlet", 1, 3),),
            loads=(Load.pressure("inner", 1.5),),
        ),
        tmp_path / "tube",
    )
    sfe = [
        ln for ln in deck.read_text().splitlines() if ln.startswith("SFE,")
    ]
    group = wall.boundary["inner"]
    assert len(sfe) == len(group.face_nodes)
    conn = wall.grid.cells.reshape(-1, 5)[:, 1:]
    want_faces = {frozenset(int(n) for n in f) for f in group.face_nodes}
    got_faces = set()
    for ln in sfe:
        _, eid, lkey, lab, blank, val = ln.split(",")
        assert lab == "PRES" and blank == "" and float(val) == 1.5
        assert int(lkey) in _ANSYS_TET_FACES  # 4/6 collapse, never loadable
        row = conn[int(eid) - 1]
        got_faces.add(
            frozenset(int(row[i]) for i in _ANSYS_TET_FACES[int(lkey)])
        )
    assert got_faces == want_faces


def test_ansys_refusals(tmp_path):
    wall = _bar_wall()
    with pytest.raises(ValueError, match="linear-static decks only"):
        _spec(
            material=Material.elastic(1000.0, 0.0, density=1e-3),
            step=Step(kind="frequency"),
        ).validate()
    with pytest.raises(ValueError, match="linear-static decks only"):
        _spec(
            material=Material.thermal(2.0), step=Step(kind="heat")
        ).validate()
    with pytest.raises(ValueError, match="MP,EX/PRXY only"):
        _spec(
            material=Material.neo_hookean(1.0, 0.1),
            step=Step(nlgeom=True),
        ).validate()
    with pytest.raises(ValueError, match="nlgeom steps target abaqus/ccx"):
        _spec(step=Step(nlgeom=True)).validate()
    with pytest.raises(ValueError, match="mixed u-P"):
        _spec(formulation="hybrid").validate()
    with pytest.raises(ValueError, match="PRINT requests"):
        _spec(step=Step(node_print={"front": ("U",)})).validate()
    with pytest.raises(ValueError, match="unknown group"):
        write_deck(
            wall, _spec(bcs=(BoundaryCondition("nope", 1, 3),)),
            tmp_path / "bad",
        )


def test_component_name_refusals(tmp_path):
    # a meshforge-legal group name ANSYS components cannot carry
    wall = _bar_wall()
    wall.boundary["bad-name"] = wall.boundary.pop("outer_rim")
    with pytest.raises(ValueError, match="cannot become an ANSYS component"):
        write_deck(wall, _spec(), tmp_path / "bad")

    # node/element components share one case-insensitive namespace
    wall = _bar_wall()
    wall.boundary["region1"] = wall.boundary.pop("outer_rim")
    with pytest.raises(ValueError, match="duplicate ANSYS component name"):
        write_deck(wall, _spec(), tmp_path / "dup")


# ---------------------------------------------------------------------------
# CLI e2e (format-level: no solver, so `solve` must refuse .cdb loudly)


def test_cli_deck_ansys_e2e(tmp_path, capsys):
    from digitwin.cli import main
    from digitwin.synth import generate

    surface, _ = generate("straight_tube")
    src = tmp_path / "tube.vtp"
    surface.save(src)
    rc = main([
        "deck", str(src), "--thickness", "1", "--n-theta", "12",
        "--n-axial", "6", "--n-layers", "2",
        "--material", "elastic", "--E", "1000", "--nu", "0.3",
        "--fix", "inlet,1,3", "--fix", "outlet,1,3",
        "--pressure", "inner,1.0",
        "--target", "ansys", "--outputs", str(tmp_path / "outputs"),
    ])
    out = capsys.readouterr().out
    assert rc == 0
    deck_path = tmp_path / "outputs" / "tube_ansys.cdb"
    assert deck_path.is_file()
    assert "target      : ansys  formulation: full" in out
    assert str(deck_path) in out

    rc = main(["solve", str(deck_path), "--outputs", str(tmp_path / "outputs")])
    err = capsys.readouterr().err
    assert rc == 1
    assert "no ANSYS solver is wired" in err

"""V33 LS-DYNA keyword deck export — FORMAT-LEVEL validation only.

No LS-DYNA solver exists here (the honest V15 pre-MYSTRAN position,
D29): golden cards pin the fixed-column layout, an internal re-parse
rebuilds the mesh from the written cards and must reproduce the source
grid exactly, segment winding is checked against the known outward
direction. Loud refusals for everything the writer does not map.
"""
from __future__ import annotations

import numpy as np
import pytest
import pyvista as pv

from meshforge.export.deck import (
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
    from meshforge.meshing import build_block_mesh, extrude_blocks

    rect = np.array([[0, 0], [1, 0], [1, 1], [0, 1]], dtype=float)
    bm = build_block_mesh([rect], target_size=1.0 / n_cross)
    return extrude_blocks(bm, thickness=length, n_layers=n_layers)


def _tet_wall():
    """Tet mesh via the gmsh backend on the tube (skips without gmsh)."""
    pytest.importorskip("gmsh")
    from meshforge.geometry import compute_centerline
    from meshforge.meshing import get_backend
    from meshforge.synth import generate

    surface, _ = generate("straight_tube")
    cl = compute_centerline(surface)
    return get_backend("tet").mesh_wall(surface, 1.0, centerline=cl)


def _spec(loads=(), bcs=(BoundaryCondition("front", 1, 3),), **kw):
    return DeckSpec(
        material=kw.pop("material", Material.elastic(1000.0, 0.3, density=1e-3)),
        step=kw.pop("step", Step()),
        boundary_conditions=bcs,
        loads=loads,
        target="lsdyna",
        **kw,
    )


def _parse_mesh(text: str):
    """Re-parse *NODE/*ELEMENT_SOLID by the fixed columns the writer uses."""
    lines = text.splitlines()
    nodes: dict[int, list[float]] = {}
    elems: list[tuple[int, list[int]]] = []
    section = None
    for ln in lines:
        if ln.startswith("*"):
            section = ln.strip()
            continue
        if ln.startswith("$"):
            continue
        if section == "*NODE":
            nodes[int(ln[:8])] = [
                float(ln[8:24]), float(ln[24:40]), float(ln[40:56])
            ]
        elif section == "*ELEMENT_SOLID":
            pid = int(ln[8:16])
            conn = [int(ln[16 + 8 * k : 24 + 8 * k]) for k in range(8)]
            elems.append((pid, conn))
    return nodes, elems


def _rebuild_grid(nodes, elems):
    ids = sorted(nodes)
    assert ids == list(range(1, len(ids) + 1))
    pts = np.array([nodes[i] for i in ids])
    cells, celltypes = [], []
    for _pid, conn in elems:
        if conn[4:] == [conn[3]] * 4:  # collapsed-hex tet convention
            cells.extend([4, *[n - 1 for n in conn[:4]]])
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
    assert deck.suffix == ".k"
    text = deck.read_text()
    assert text.startswith("*KEYWORD\n*TITLE\n")
    assert "$ format-level export only" in text
    # node card: I8 + 3xE16 (16-char float columns)
    assert "\n       1               0               0               0\n" in text
    # element card: ten I8 fields
    assert "\n       1       1       1       4       5       2      10      13      14      11\n" in text
    assert "*SECTION_SOLID\n         1         1\n" in text  # reduced -> ELFORM 1
    assert "*MAT_ELASTIC\n         1     0.001      1000       0.3\n" in text
    assert "*PART\nregion 1\n         1         1         1\n" in text
    # SPC flags: nid, cid=0, dofx..dofz, rot flags zero
    assert "\n         1         0         1         1         1         0         0         0\n" in text
    assert "*DEFINE_CURVE\n         1\n" in text
    assert "\n                   0                   0\n" in text
    assert "\n                   1                   1\n" in text
    assert "*LOAD_NODE_POINT\n         1         3         1      -1.5\n" in text
    assert "*LOAD_SEGMENT\n" in text
    assert text.rstrip().endswith("*END")


def test_full_formulation_elform_2(tmp_path):
    wall = _bar_wall()
    deck = write_deck(
        wall,
        _spec(loads=(Load.pressure("back", 1.0),), formulation="full"),
        tmp_path / "bar",
    )
    assert "*SECTION_SOLID\n         1         2\n" in deck.read_text()


def test_missing_density_ro_zero_note(tmp_path):
    wall = _bar_wall()
    deck = write_deck(
        wall, _spec(material=Material.elastic(1000.0, 0.3)), tmp_path / "bar"
    )
    text = deck.read_text()
    assert "$ NOTE: density not set: *MAT_ELASTIC RO written as 0.0" in text
    assert "*MAT_ELASTIC\n         1         0      1000       0.3\n" in text


# ---------------------------------------------------------------------------
# internal re-parse: the written cards must reproduce the mesh exactly


def test_hex_reparse_roundtrip(tmp_path):
    wall = _bar_wall(n_cross=3, n_layers=4)
    deck = write_deck(wall, _spec(), tmp_path / "bar")
    nodes, elems = _parse_mesh(deck.read_text())
    grid = _rebuild_grid(nodes, elems)
    assert grid.n_points == wall.grid.n_points
    assert grid.n_cells == wall.grid.n_cells
    np.testing.assert_allclose(grid.points, wall.grid.points, atol=1e-7)
    src = wall.grid.cells.reshape(-1, 9)[:, 1:]
    out = grid.cells.reshape(-1, 9)[:, 1:]
    np.testing.assert_array_equal(out, src)
    # part ids mirror the region tags exactly
    region = np.asarray(wall.grid.cell_data["region"], dtype=int)
    np.testing.assert_array_equal([pid for pid, _ in elems], region)
    assert grid.volume == pytest.approx(wall.grid.volume, rel=1e-12)


def test_tet_reparse_roundtrip(tmp_path):
    wall = _tet_wall()
    deck = write_deck(
        wall,
        _spec(bcs=(BoundaryCondition("inlet", 1, 3),)),
        tmp_path / "tube",
    )
    text = deck.read_text()
    assert "*SECTION_SOLID\n         1        10\n" in text  # tet reduced
    nodes, elems = _parse_mesh(text)
    for _pid, conn in elems:  # collapsed-hex convention pinned
        assert conn[4:] == [conn[3]] * 4
    grid = _rebuild_grid(nodes, elems)
    assert grid.n_cells == wall.grid.n_cells
    assert grid.volume == pytest.approx(wall.grid.volume, rel=1e-9)


def test_segments_cover_group_outward(tmp_path):
    """One *LOAD_SEGMENT per boundary face, nodes from the group, wound so
    the right-hand normal points OUT of the body (winding is load-bearing:
    LS-DYNA positive pressure acts opposite the segment normal)."""
    wall = _bar_wall(n_cross=3, n_layers=4, length=2.0)
    deck = write_deck(
        wall, _spec(loads=(Load.pressure("back", 2.5),)), tmp_path / "bar"
    )
    text = deck.read_text()
    seg_lines = []
    in_seg = False
    for ln in text.splitlines():
        if ln.startswith("*"):
            in_seg = ln == "*LOAD_SEGMENT"
            continue
        if in_seg:
            seg_lines.append(ln)
    group = wall.boundary["back"]
    assert len(seg_lines) == len(group.face_nodes)
    group_nodes = set(int(n) + 1 for n in np.unique(group.face_nodes))
    pts = np.asarray(wall.grid.points)
    for ln in seg_lines:
        assert float(ln[10:20]) == 2.5  # sf column carries the magnitude
        seg = [int(ln[30 + 10 * k : 40 + 10 * k]) for k in range(4)]
        assert set(seg) <= group_nodes
        quad = pts[[n - 1 for n in seg]]
        normal = 0.5 * np.cross(quad[2] - quad[0], quad[3] - quad[1])
        assert normal[2] > 0  # 'back' is the +z face of the bar


# ---------------------------------------------------------------------------
# loud refusals


def test_lsdyna_refusals(tmp_path):
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
    with pytest.raises(ValueError, match="MAT_ELASTIC only"):
        _spec(
            material=Material.neo_hookean(1.0, 0.1),
            step=Step(nlgeom=True),
        ).validate()
    with pytest.raises(ValueError, match="no hybrid solid"):
        _spec(formulation="hybrid").validate()
    with pytest.raises(ValueError, match="BOUNDARY_PRESCRIBED_MOTION"):
        _spec(bcs=(BoundaryCondition("front", 3, None, 0.5),)).validate()
    with pytest.raises(ValueError, match="PRINT requests"):
        _spec(step=Step(node_print={"front": ("U",)})).validate()
    with pytest.raises(ValueError, match="unknown group"):
        write_deck(
            wall, _spec(bcs=(BoundaryCondition("nope", 1, 3),)),
            tmp_path / "bad",
        )


# ---------------------------------------------------------------------------
# CLI e2e (format-level: no solver, so `solve` must refuse .k loudly)


def test_cli_deck_lsdyna_e2e(tmp_path, capsys):
    from meshforge.cli import main
    from meshforge.synth import generate

    surface, _ = generate("straight_tube")
    src = tmp_path / "tube.vtp"
    surface.save(src)
    rc = main([
        "deck", str(src), "--thickness", "1", "--n-theta", "12",
        "--n-axial", "6", "--n-layers", "2",
        "--material", "elastic", "--E", "1000", "--nu", "0.3",
        "--fix", "inlet,1,3", "--fix", "outlet,1,3",
        "--pressure", "inner,1.0",
        "--target", "lsdyna", "--outputs", str(tmp_path / "outputs"),
    ])
    out = capsys.readouterr().out
    assert rc == 0
    deck_path = tmp_path / "outputs" / "tube_lsdyna.k"
    assert deck_path.is_file()
    assert "target      : lsdyna  formulation: reduced" in out
    assert str(deck_path) in out

    rc = main(["solve", str(deck_path), "--outputs", str(tmp_path / "outputs")])
    err = capsys.readouterr().err
    assert rc == 1
    assert "no LS-DYNA solver is wired" in err

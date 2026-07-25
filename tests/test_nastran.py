"""V15 NASTRAN bulk-data export: golden card structure, meshio read-back,
PLOAD4 face identification, loud refusals. Format-level validation only —
no NASTRAN-compatible solver is installed (D29)."""
from __future__ import annotations

from pathlib import Path

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
from meshforge.export.nastran import _f8, _small
from meshforge.geometry import compute_centerline
from meshforge.meshing import get_backend
from meshforge.meshing.base import BoundaryGroup, WallMesh
from meshforge.quality.volume import volume_quality
from meshforge.synth import generate


@pytest.fixture(scope="module")
def hex_wall() -> WallMesh:
    surface, _ = generate("straight_tube")
    cl = compute_centerline(surface)
    return get_backend("swept").mesh_wall(
        surface, 0.6, centerline=cl, n_theta=12, n_axial=8, n_layers=2
    )


@pytest.fixture()
def tet_wall() -> WallMesh:
    """Hand-built single tet (same as the abaqus export tests)."""
    points = np.array(
        [[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]
    )
    cells = np.array([4, 0, 1, 2, 3], dtype=np.int64)
    grid = pv.UnstructuredGrid(cells, np.array([pv.CellType.TETRA], np.uint8), points)
    grid.cell_data["region"] = np.ones(1, dtype=np.int32)
    groups = {
        "inner": BoundaryGroup("inner", np.array([0]), np.array([[0, 1, 2]])),
        "outer": BoundaryGroup("outer", np.array([0]), np.array([[1, 3, 2]])),
        "inlet": BoundaryGroup("inlet", np.array([0]), np.array([[0, 3, 1]])),
        "outlet": BoundaryGroup("outlet", np.array([0]), np.array([[2, 3, 0]])),
    }
    return WallMesh(
        grid=grid, boundary=groups, backend="manual", quality=volume_quality(grid)
    )


def _spec(**over) -> DeckSpec:
    kw = dict(
        material=Material.elastic(1000.0, 0.3),
        step=Step(),
        boundary_conditions=(
            BoundaryCondition("inlet", 3),
            BoundaryCondition("outlet", 1, 3),
        ),
        loads=(Load.pressure("inner", 1.5),),
        target="nastran",
    )
    kw.update(over)
    return DeckSpec(**kw)


def _cards(text: str, name: str) -> list[str]:
    return [ln for ln in text.splitlines() if ln.startswith(name)]


# ---------------------------------------------------------------------------
# structure


def test_hex_deck_structure(hex_wall, tmp_path):
    out = write_deck(hex_wall, _spec(), tmp_path / "deck")
    assert out.suffix == ".bdf"
    text = out.read_text()
    lines = text.splitlines()

    assert lines[0] == "SOL 101"
    assert "CEND" in lines and "BEGIN BULK" in lines
    assert lines.index("CEND") < lines.index("BEGIN BULK")
    assert lines[-1] == "ENDDATA"
    for card in ("SPC = 1", "LOAD = 1", "DISPLACEMENT(PRINT) = ALL",  # PRINT since V25 (MYSTRAN F06 read-back)
                 "STRESS(PLOT) = ALL"):
        assert card in lines
    assert "$ UNITS: length=mm, force=N -> stress = N/mm^2" in lines

    n = hex_wall.grid.n_points
    grid_stars = _cards(text, "GRID*")
    assert len(grid_stars) == n
    # Each GRID* has its continuation line.
    assert sum(1 for ln in lines if ln.startswith("*G")) == n
    chexa = _cards(text, "CHEXA")
    assert len(chexa) == hex_wall.grid.n_cells
    assert sum(1 for ln in lines if ln.startswith("+E")) == hex_wall.grid.n_cells
    assert len(_cards(text, "MAT1")) == 1 and "1000." in text
    assert len(_cards(text, "PSOLID")) == len(
        np.unique(hex_wall.grid.cell_data["region"])
    )
    # SPC1 grids: inlet dof 3 only, outlet dofs 123, <=6 grids per card.
    spc1 = _cards(text, "SPC1")
    inlet_n = len(np.unique(hex_wall.boundary["inlet"].face_nodes))
    outlet_n = len(np.unique(hex_wall.boundary["outlet"].face_nodes))
    counted = sum(len(ln.split()) - 3 for ln in spc1)
    assert counted == inlet_n + outlet_n
    comps = {ln.split()[2] for ln in spc1}
    assert comps == {"3", "123"}
    assert len(_cards(text, "PLOAD4")) == hex_wall.boundary["inner"].n_faces
    # Small-field discipline: bulk columns are 8 chars wide.
    for ln in chexa:
        assert ln == ln.rstrip() and len(ln) <= 80


def test_meshio_readback(hex_wall, tmp_path):
    import meshio

    out = write_deck(hex_wall, _spec(), tmp_path / "rb")
    back = meshio.read(out)
    assert len(back.points) == hex_wall.grid.n_points
    np.testing.assert_allclose(
        back.points, np.asarray(hex_wall.grid.points), rtol=1e-8, atol=1e-12
    )
    cells = {b.type: len(b.data) for b in back.cells}
    assert cells == {"hexahedron": hex_wall.grid.n_cells}
    # Connectivity survives the continuation lines, element for element.
    conn_back = next(b.data for b in back.cells if b.type == "hexahedron")
    conn_ours = hex_wall.grid.cells.reshape(-1, 9)[:, 1:]
    np.testing.assert_array_equal(conn_back, conn_ours)


def test_pload4_identifies_hex_faces(hex_wall, tmp_path):
    out = write_deck(hex_wall, _spec(), tmp_path / "faces")
    group = hex_wall.boundary["inner"]
    by_eid = {int(c) + 1: [int(x) for x in f]
              for c, f in zip(group.cell_ids, group.face_nodes)}
    cards = _cards(out.read_text(), "PLOAD4")
    assert len(cards) == group.n_faces
    for ln in cards:
        f = ln.split()
        eid, g1, g3 = int(f[2]), int(f[4]) - 1, int(f[5]) - 1
        face = by_eid[eid]
        assert g1 in face and g3 in face
        # Diagonal, not adjacent, on the quad perimeter.
        assert abs(face.index(g1) - face.index(g3)) == 2
        assert _f8(1.5) == f[3]


def test_tet_deck_pload4_force_and_nonzero_spc(tet_wall, tmp_path):
    spec = _spec(
        boundary_conditions=(
            BoundaryCondition("inlet", 3),
            BoundaryCondition("outlet", 1, value=0.25),
        ),
        loads=(Load.pressure("inner", 2.0), Load.concentrated("outer", 2, 7.0)),
    )
    text = write_deck(tet_wall, spec, tmp_path / "tet").read_text()
    assert len(_cards(text, "CTETRA")) == 1
    # inner face is (0,1,2); the off-face corner is node 3 -> grid 4.
    (pload,) = _cards(text, "PLOAD4")
    f = pload.split()
    assert int(f[2]) == 1 and int(f[5]) == 4 and int(f[4]) - 1 in (0, 1, 2)
    # FORCE per outer node, unit vector on dof 2 (blank CID collapses in split).
    forces = _cards(text, "FORCE")
    assert len(forces) == len(np.unique(tet_wall.boundary["outer"].face_nodes))
    assert forces[0].split()[3:] == ["7.", "0.", "1.", "0."]
    # Nonzero enforced displacement -> per-node SPC with the value.
    bulk = text.split("BEGIN BULK", 1)[1]
    spcs = [ln for ln in _cards(bulk, "SPC") if not ln.startswith("SPC1")]
    assert len(spcs) == len(np.unique(tet_wall.boundary["outlet"].face_nodes))
    assert spcs[0].split()[3:] == ["1", "0.25"]


# ---------------------------------------------------------------------------
# refusals + helpers


def test_nastran_refusals(hex_wall, tmp_path):
    with pytest.raises(ValueError, match="MAT1"):
        write_deck(
            hex_wall,
            _spec(material=Material.neo_hookean(0.5, 0.06), step=Step(nlgeom=True)),
            tmp_path / "a",
        )
    with pytest.raises(ValueError, match="SOL 101"):
        write_deck(hex_wall, _spec(step=Step(nlgeom=True)), tmp_path / "b")
    with pytest.raises(ValueError, match="PRINT"):
        write_deck(
            hex_wall, _spec(step=Step(node_print={"inner": ("U",)})), tmp_path / "c"
        )
    with pytest.raises(ValueError, match="formulation"):
        write_deck(hex_wall, _spec(formulation="hybrid"), tmp_path / "d")
    with pytest.raises(ValueError, match="unknown surface"):
        write_deck(
            hex_wall, _spec(loads=(Load.pressure("nope", 1.0),)), tmp_path / "e"
        )


def test_f8_and_small_field_guards():
    assert _f8(1.0) == "1."
    assert _f8(0.3) == "0.3"
    assert _f8(200000.0) == "200000."
    assert len(_f8(1234.5678)) <= 8
    assert len(_f8(-9.87654321e-7)) <= 8
    with pytest.raises(ValueError, match="8-char column"):
        _small("CARD", "123456789")


def test_cli_deck_target_nastran(tmp_path):
    from meshforge.cli import main
    from meshforge.synth import generate as gen

    surface, _ = gen("straight_tube")
    vtp = tmp_path / "tube.vtp"
    surface.save(vtp)
    rc = main([
        "deck", str(vtp), "--thickness", "0.6", "--n-theta", "12",
        "--n-axial", "6", "--n-layers", "2",
        "--material", "elastic", "--E", "1000", "--nu", "0.3",
        "--fix", "inlet,3", "--pressure", "inner,1.0",
        "--target", "nastran", "--outputs", str(tmp_path / "out"),
    ])
    assert rc == 0
    bdf = tmp_path / "out" / "tube_nastran.bdf"
    assert bdf.is_file()
    text = bdf.read_text()
    assert text.startswith("SOL 101") and text.rstrip().endswith("ENDDATA")

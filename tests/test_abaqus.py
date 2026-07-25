from __future__ import annotations

import subprocess
from pathlib import Path

import numpy as np
import pytest
import pyvista as pv

from meshforge.export import export_abaqus
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


@pytest.fixture(scope="module")
def tet_wall() -> WallMesh:
    """Hand-built single tet — exercises tet export without needing gmsh."""
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


def test_deck_structure_default_hybrid(hex_wall: WallMesh, tmp_path: Path) -> None:
    out = export_abaqus(hex_wall, tmp_path / "wall")
    text = out.read_text()

    assert "*ELEMENT, TYPE=C3D8H, ELSET=WALL" in text  # hybrid default (trap #11)
    assert text.count("\n*SURFACE") == 4
    for name in ("INNER", "OUTER", "INLET", "OUTLET"):
        assert f"*NSET, NSET={name}" in text
        assert f"*SURFACE, NAME={name}_SURF, TYPE=ELEMENT" in text
    assert "*ELSET, ELSET=REGION1" in text

    n_element_lines = sum(
        1 for line in text.splitlines() if line.split(",")[0].strip().isdigit() and line.count(",") == 8
    )
    assert n_element_lines == hex_wall.grid.n_cells


def test_surface_face_labels_match_node_sets(hex_wall: WallMesh, tmp_path: Path) -> None:
    from meshforge.export.abaqus import _HEX_FACES

    out = export_abaqus(hex_wall, tmp_path / "wall2")
    conn = hex_wall.grid.cells.reshape(-1, 9)[:, 1:]

    section = None
    checked = 0
    for line in out.read_text().splitlines():
        if line.startswith("*SURFACE, NAME=INNER_SURF"):
            section = "inner"
            continue
        if line.startswith("*"):
            section = None
            continue
        if section == "inner" and checked < 20:
            eid_str, label = [p.strip() for p in line.split(",")]
            eid = int(eid_str) - 1
            local = _HEX_FACES[label]
            deck_face = {int(conn[eid][i]) for i in local}
            k = checked  # groups keep insertion order
            expected = set(int(n) for n in hex_wall.boundary["inner"].face_nodes[k])
            assert deck_face == expected
            checked += 1
    assert checked == 20


def test_face_matching_is_winding_robust(hex_wall: WallMesh, tmp_path: Path) -> None:
    import copy

    scrambled = copy.copy(hex_wall)
    scrambled.boundary = {
        name: BoundaryGroup(g.name, g.cell_ids, g.face_nodes[:, ::-1])  # reversed winding
        for name, g in hex_wall.boundary.items()
    }
    a = export_abaqus(hex_wall, tmp_path / "orig").read_text()
    b = export_abaqus(scrambled, tmp_path / "rev").read_text()
    assert a.split("*SURFACE", 1)[1] == b.split("*SURFACE", 1)[1]


def test_formulation_switch_and_guards(hex_wall: WallMesh, tet_wall: WallMesh, tmp_path: Path) -> None:
    assert "TYPE=C3D8," in export_abaqus(hex_wall, tmp_path / "f", formulation="full").read_text()
    assert "TYPE=C3D8R," in export_abaqus(hex_wall, tmp_path / "r", formulation="reduced").read_text()
    assert "TYPE=C3D4," in export_abaqus(tet_wall, tmp_path / "t", formulation="full").read_text()
    # meshio cannot read C3D4H back — validate hybrid tet decks as TEXT (trap #11).
    assert "TYPE=C3D4H," in export_abaqus(tet_wall, tmp_path / "th").read_text()
    with pytest.raises(ValueError, match="no 'reduced' formulation"):
        export_abaqus(tet_wall, tmp_path / "bad", formulation="reduced")


def test_meshio_readback_nonhybrid(hex_wall: WallMesh, tmp_path: Path) -> None:
    import meshio

    out = export_abaqus(hex_wall, tmp_path / "readback", formulation="full")
    back = meshio.read(out)
    assert len(back.points) == hex_wall.grid.n_points
    assert sum(len(b.data) for b in back.cells if b.type == "hexahedron") == hex_wall.grid.n_cells


def test_benchmark_requires_material(hex_wall: WallMesh, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="requires a material"):
        export_abaqus(hex_wall, tmp_path / "x", uniaxial_benchmark={"sigma": 1.0})


# V13 promoted the ccx lookup into meshforge.solve; tests share it.
from meshforge.solve import find_ccx as _find_ccx  # noqa: E402


def test_ccx_uniaxial_tension_matches_sigma_L_over_E(tmp_path: Path) -> None:
    """The end-to-end solve: annular bar in uniaxial tension via *DSLOAD on
    the element-based outlet surface; tip displacement must be σL/E."""
    ccx = _find_ccx()
    if ccx is None:
        pytest.skip("CalculiX (ccx) not available")

    surface, _ = generate("straight_tube")
    cl = compute_centerline(surface)
    wall = get_backend("swept").mesh_wall(
        surface, 0.6, centerline=cl, n_theta=16, n_axial=20, n_layers=2
    )
    assert wall.quality.n_inverted == 0

    E, nu, sigma = 200_000.0, 0.3, 50.0
    deck = export_abaqus(
        wall,
        tmp_path / "job",
        formulation="full",  # ccx has no hybrid library (trap #11)
        material={"name": "STEELISH", "E": E, "nu": nu},
        uniaxial_benchmark={"sigma": sigma},
    )

    proc = subprocess.run(
        [ccx, "-i", deck.stem],
        cwd=deck.parent,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert proc.returncode == 0, proc.stdout[-2000:] + proc.stderr[-2000:]

    # Parse nodal displacements from the .dat (*NODE PRINT, U).
    from meshforge.solve import parse_displacements

    dat = (deck.parent / f"{deck.stem}.dat").read_text()
    u = parse_displacements(dat)
    assert u, "no displacement block found in ccx .dat output"
    u3 = {nid: vec[2] for nid, vec in u.items()}

    pts = np.asarray(wall.grid.points)
    outlet_nodes = np.unique(wall.boundary["outlet"].face_nodes)
    inlet_nodes = np.unique(wall.boundary["inlet"].face_nodes)
    length = float(pts[outlet_nodes, 2].mean() - pts[inlet_nodes, 2].mean())

    expected = sigma * length / E
    got = np.mean([u3[int(n) + 1] for n in outlet_nodes])
    assert got == pytest.approx(expected, rel=0.02), (got, expected)

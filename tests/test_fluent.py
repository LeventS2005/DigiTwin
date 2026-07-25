"""V28 FLUENT .msh export: hand-built exactness (hex indices, the
c0/c1 orientation convention), per-cell divergence volumes recomputed
from the WRITTEN file, loud failure branches, session/CLI integration;
external validation through OpenFOAM's fluent3DMeshToFoam (gated)."""
from __future__ import annotations

import re
import subprocess
import tempfile
from pathlib import Path

import numpy as np
import pytest
import pyvista as pv

from meshforge.export.fluent import write_fluent
from meshforge.meshing.base import BoundaryGroup, WallMesh
from meshforge.quality.volume import volume_quality
from meshforge.solve.foam_runner import find_foam

# ---------------------------------------------------------------------------
# minimal independent reader for the file we write (all indices hex)


def _parse_msh(path: Path):
    text = path.read_text()
    m = re.search(r"\(10 \(1 1 ([0-9a-f]+) 1 3\)\(\n(.*?)\n\)\)", text, re.S)
    assert m, "node section missing"
    pts = np.array(
        [[float(v) for v in ln.split()] for ln in m.group(2).splitlines()]
    )
    assert len(pts) == int(m.group(1), 16)
    n_cells = int(re.search(r"\(12 \(0 1 ([0-9a-f]+) 0\)\)", text).group(1), 16)
    zones = []
    for zm in re.finditer(
        r"\(13 \(([0-9a-f]+) ([0-9a-f]+) ([0-9a-f]+) ([0-9a-f]+) 0\)\(\n(.*?)\n\)\)",
        text, re.S,
    ):
        zid, first, last, bc = (int(zm.group(i), 16) for i in range(1, 5))
        faces = []
        for ln in zm.group(5).splitlines():
            tok = [int(t, 16) for t in ln.split()]
            assert len(tok) == tok[0] + 3, ln
            faces.append(
                (tuple(v - 1 for v in tok[1 : 1 + tok[0]]), tok[-2], tok[-1])
            )
        assert len(faces) == last - first + 1
        zones.append({"id": zid, "bc": bc, "faces": faces})
    names = {
        int(nm.group(1), 16): (nm.group(2), nm.group(3))
        for nm in re.finditer(r"\(45 \(([0-9a-f]+) ([\w-]+) ([\w-]+)\)\(\)\)", text)
    }
    return pts, n_cells, zones, names


def _cell_volumes_from_file(pts, n_cells, zones) -> np.ndarray:
    """Divergence theorem per cell, straight from the written records.

    The right-hand-rule normal of each face points from c1 to c0
    (outward for c1, inward for c0) — only the correct convention
    reproduces every cell volume."""
    vol = np.zeros(n_cells)
    for z in zones:
        for verts, c0, c1 in z["faces"]:
            p = pts[list(verts)]
            flux = sum(
                np.linalg.det(np.stack([p[0], p[i], p[i + 1]])) / 6.0
                for i in range(1, len(verts) - 1)
            )
            if c1:
                vol[c1 - 1] += flux
            if c0:
                vol[c0 - 1] -= flux
    return vol


# ---------------------------------------------------------------------------
# hand-built fixtures (shared shapes with test_foam)


def _cube_wall() -> WallMesh:
    pts = np.array(
        [[0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 1, 0],
         [0, 0, 1], [1, 0, 1], [1, 1, 1], [0, 1, 1]], dtype=float,
    )
    grid = pv.UnstructuredGrid(
        np.array([8, 0, 1, 2, 3, 4, 5, 6, 7], np.int64),
        np.array([pv.CellType.HEXAHEDRON], np.uint8), pts,
    )
    faces = {
        "bottom": [0, 1, 2, 3], "top": [4, 5, 6, 7], "inlet": [0, 1, 5, 4],
        "right": [1, 2, 6, 5], "outlet": [2, 3, 7, 6], "left": [3, 0, 4, 7],
    }
    groups = {
        name: BoundaryGroup(name, np.array([0]), np.array([nodes]))
        for name, nodes in faces.items()
    }
    return WallMesh(grid=grid, boundary=groups, backend="manual",
                    quality=volume_quality(grid))


def _two_tet_wall(*, drop_group_face: bool = False) -> WallMesh:
    pts = np.array(
        [[0, 0, 0], [1, 0, 0], [0, 1, 0], [0, 0, 1], [1, 1, 1]], dtype=float
    )
    grid = pv.UnstructuredGrid(
        np.array([4, 0, 1, 2, 3, 4, 1, 2, 3, 4], np.int64),
        np.array([pv.CellType.TETRA, pv.CellType.TETRA], np.uint8), pts,
    )
    shell = [[0, 2, 1], [0, 1, 3], [0, 3, 2],
             [1, 2, 4], [2, 3, 4], [1, 4, 3]]
    if drop_group_face:
        shell = shell[:-1]
    groups = {"shell": BoundaryGroup("shell", np.zeros(len(shell), int),
                                     np.array(shell))}
    return WallMesh(grid=grid, boundary=groups, backend="manual",
                    quality=volume_quality(grid))


def _house_wall() -> WallMesh:
    """Mixed cell types: a unit hex with a wedge 'roof' on its top face
    (conformal via the shared quad) — exercises the per-cell type body."""
    pts = np.array(
        [[0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 1, 0],
         [0, 0, 1], [1, 0, 1], [1, 1, 1], [0, 1, 1],
         [0, 0.5, 1.5], [1, 0.5, 1.5]], dtype=float,
    )
    hexa = [8, 0, 1, 2, 3, 4, 5, 6, 7]
    for tri1, tri2 in ([(4, 7, 8), (5, 6, 9)], [(4, 8, 7), (5, 9, 6)]):
        grid = pv.UnstructuredGrid(
            np.array([*hexa, 6, *tri1, *tri2], np.int64),
            np.array([pv.CellType.HEXAHEDRON, pv.CellType.WEDGE], np.uint8),
            pts,
        )
        quality = volume_quality(grid)
        if quality.n_inverted == 0:
            break
    assert quality.n_inverted == 0, "wedge winding never positive"
    quads = [
        [0, 3, 2, 1], [0, 1, 5, 4], [1, 2, 6, 5], [2, 3, 7, 6], [3, 0, 4, 7],
        [7, 8, 9, 6], [8, 4, 5, 9],
    ]
    tris = [[4, 7, 8], [5, 6, 9]]
    groups = {
        "walls": BoundaryGroup("walls", np.zeros(len(quads), int), np.array(quads)),
        "gables": BoundaryGroup("gables", np.zeros(len(tris), int), np.array(tris)),
    }
    return WallMesh(grid=grid, boundary=groups, backend="manual",
                    quality=quality)


# ---------------------------------------------------------------------------
# exactness on hand-built meshes


def test_single_cube_msh_exact(tmp_path):
    wall = _cube_wall()
    out = write_fluent(wall, tmp_path / "cube.msh")
    pts, n_cells, zones, names = _parse_msh(out)
    assert n_cells == 1
    assert np.allclose(pts, wall.grid.points)
    assert len(zones) == 6  # no interior faces on a single cell
    # name heuristic: inlet -> velocity-inlet (0xa), outlet -> 5, else wall
    by_name = {names[z["id"]][1]: z for z in zones}
    assert names[by_name["inlet"]["id"]][0] == "velocity-inlet"
    assert by_name["inlet"]["bc"] == 10
    assert by_name["outlet"]["bc"] == 5
    assert by_name["top"]["bc"] == 3
    assert (2, "fluid", "fluid") in [(k, *v) for k, v in names.items()]
    # boundary faces carry c0 = 0, c1 = the owner
    for z in zones:
        assert all(c0 == 0 and c1 == 1 for _, c0, c1 in z["faces"])
    assert _cell_volumes_from_file(pts, n_cells, zones)[0] == pytest.approx(1.0)


def test_two_tet_internal_face_convention(tmp_path):
    wall = _two_tet_wall()
    out = write_fluent(wall, tmp_path / "tets.msh")
    pts, n_cells, zones, names = _parse_msh(out)
    interior = [z for z in zones if z["bc"] == 2]
    assert len(interior) == 1 and len(interior[0]["faces"]) == 1
    verts, c0, c1 = interior[0]["faces"][0]
    assert (c0, c1) == (2, 1)  # wound outward of the owner: c1=owner, c0=neighbour
    assert names[interior[0]["id"]] == ("interior", "interior-faces")
    vtk_vols = wall.grid.compute_cell_sizes()["Volume"]
    assert np.allclose(_cell_volumes_from_file(pts, n_cells, zones), vtk_vols)


def test_mixed_cell_type_body(tmp_path):
    wall = _house_wall()
    out = write_fluent(wall, tmp_path / "house.msh")
    text = out.read_text()
    m = re.search(r"\(12 \(2 1 2 1 0\)\(\n(.*?)\n\)\)", text, re.S)
    assert m, "mixed cell section with a per-cell type body expected"
    assert m.group(1).split() == ["4", "6"]  # hex, wedge
    pts, n_cells, zones, _ = _parse_msh(out)
    vtk_vols = wall.grid.compute_cell_sizes()["Volume"]
    assert np.allclose(_cell_volumes_from_file(pts, n_cells, zones), vtk_vols)


# ---------------------------------------------------------------------------
# real swept wall: global invariants


@pytest.fixture(scope="module")
def tube_wall() -> WallMesh:
    from meshforge.geometry import compute_centerline
    from meshforge.meshing import get_backend
    from meshforge.synth import generate

    surface, _ = generate("straight_tube")
    cl = compute_centerline(surface)
    return get_backend("swept").mesh_wall(
        surface, 1.0, centerline=cl, n_theta=16, n_axial=8, n_layers=3
    )


def test_tube_msh_invariants(tmp_path, tube_wall):
    out = write_fluent(tube_wall, tmp_path / "tube.msh")
    text = out.read_text()
    # single-type grid: declaration-only cell section (no body)
    assert re.search(r"\(12 \(2 1 [0-9a-f]+ 1 4\)\)", text)
    pts, n_cells, zones, names = _parse_msh(out)
    assert n_cells == tube_wall.grid.n_cells
    # face conservation: 6 per hex, interior shared by two
    n_int = sum(len(z["faces"]) for z in zones if z["bc"] == 2)
    n_bnd = sum(len(z["faces"]) for z in zones if z["bc"] != 2)
    assert 6 * n_cells == 2 * n_int + n_bnd
    # zone faces == boundary-group faces, zone by zone
    zone_by_name = {names[z["id"]][1]: z for z in zones if z["bc"] != 2}
    assert {k: len(z["faces"]) for k, z in zone_by_name.items()} == {
        k: g.n_faces for k, g in tube_wall.boundary.items()
    }
    assert zone_by_name["inlet"]["bc"] == 10  # 0xa in the file
    assert zone_by_name["inner"]["bc"] == 3
    # orientation: every cell volume reproduced from the face records
    vtk_vols = tube_wall.grid.compute_cell_sizes()["Volume"]
    assert np.allclose(_cell_volumes_from_file(pts, n_cells, zones), vtk_vols)


# ---------------------------------------------------------------------------
# loud failure branches


def test_refusals_are_loud(tmp_path):
    wall = _cube_wall()
    with pytest.raises(ValueError, match=r"end in \.msh"):
        write_fluent(wall, tmp_path / "cube.cas")
    with pytest.raises(ValueError, match="unknown FLUENT bc type"):
        write_fluent(wall, tmp_path / "a.msh", bc_types={"top": "freestream"})
    with pytest.raises(ValueError, match="unknown FLUENT bc type"):
        write_fluent(wall, tmp_path / "b.msh", bc_types={"top": "interior"})
    with pytest.raises(ValueError, match="unknown patch"):
        write_fluent(wall, tmp_path / "c.msh", bc_types={"nope": "wall"})
    with pytest.raises(ValueError, match="covered by no boundary group"):
        write_fluent(_two_tet_wall(drop_group_face=True), tmp_path / "d.msh")
    out = write_fluent(
        _two_tet_wall(drop_group_face=True), tmp_path / "e.msh",
        default_patch="rest",
    )
    _, _, zones, names = _parse_msh(out)
    assert "rest" in {names[z["id"]][1] for z in zones}


def test_unsupported_cell_type_is_loud(tmp_path):
    pts = np.array(
        [[0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 1, 0], [0.5, 0.5, 1]], float
    )
    grid = pv.UnstructuredGrid(
        np.array([5, 0, 1, 2, 3, 4], np.int64),
        np.array([pv.CellType.PYRAMID], np.uint8), pts,
    )
    wall = WallMesh(grid=grid, boundary={}, backend="manual",
                    quality=volume_quality(grid))
    with pytest.raises(ValueError, match="hexahedra, tetrahedra and wedges"):
        write_fluent(wall, tmp_path / "pyr.msh")


# ---------------------------------------------------------------------------
# session + CLI


def test_session_save_fluent_journaled(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from meshforge.pipeline import MeshingSession

    session = MeshingSession()
    session.generate_synth("straight_tube")
    session.compute_centerline()
    session.compute_volmesh(thickness=1.0, n_theta=12, n_axial=4, n_layers=2)
    result = session.save("fluent", tmp_path / "tube.msh")
    written = Path(result.summary["path"])
    assert written.is_file()
    pts, n_cells, zones, _ = _parse_msh(written)
    assert n_cells == session.volmesh.grid.n_cells
    assert session.journal[-1]["step"] == "save"
    assert session.journal[-1]["params"]["what"] == "fluent"


def test_cli_fluent_end_to_end(tmp_path):
    from meshforge.cli import main
    from meshforge.synth import generate

    surface, _ = generate("straight_tube")
    vtp = tmp_path / "tube.vtp"
    surface.save(vtp)
    out = tmp_path / "tube.msh"
    rc = main([
        "fluent", str(vtp), "--thickness", "1.0", "--n-theta", "12",
        "--n-axial", "4", "--n-layers", "2", "--out", str(out),
        "--bc-type", "inner,symmetry",
    ])
    assert rc == 0
    _, _, zones, names = _parse_msh(out)
    by_name = {names[z["id"]][1]: z for z in zones if z["bc"] != 2}
    assert by_name["inner"]["bc"] == 7  # symmetry override
    assert set(by_name) == {"inner", "outer", "inlet", "outlet"}


def test_cli_fluent_error_paths(tmp_path, capsys):
    from meshforge.cli import main

    rc = main(["fluent", str(tmp_path / "x.vtp")])
    assert rc == 1
    assert "either --thickness or --outer-surface" in capsys.readouterr().err
    rc = main(["fluent", str(tmp_path / "x.vtp"), "--thickness", "1",
               "--bc-type", "oops"])
    assert rc == 1
    assert "NAME,TYPE" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# external validation via OpenFOAM's converter (skips cleanly — trap #16)


_STUB = "FoamFile\n{ version 2.0; format ascii; class dictionary; object %s; }\n"


def test_fluent3dmeshtofoam_roundtrip(tube_wall):
    """Our .msh through a THIRD-PARTY reader: fluent3DMeshToFoam converts
    it and checkMesh reports Mesh OK with the exact cell count and
    volume (the wrong c0/c1 convention converts to NEGATIVE volumes —
    probe-verified, so this is a real orientation check)."""
    foam = find_foam()
    if foam is None:
        pytest.skip("OpenFOAM not available")
    with tempfile.TemporaryDirectory(prefix="meshforge_fluent_") as tmp:
        if " " in tmp:
            pytest.skip("temp dir contains a space — OpenFOAM cannot run")
        case = Path(tmp) / "case"
        (case / "system").mkdir(parents=True)
        (case / "system" / "controlDict").write_text(
            _STUB % "controlDict"
            + "application checkMesh;\nstartFrom startTime;\nstartTime 0;\n"
              "stopAt endTime;\nendTime 1;\ndeltaT 1;\n"
              "writeControl timeStep;\nwriteInterval 1;\n"
        )
        (case / "system" / "fvSchemes").write_text(
            _STUB % "fvSchemes"
            + "ddtSchemes { default steadyState; }\n"
              "gradSchemes { default Gauss linear; }\n"
              "divSchemes { default none; }\n"
              "laplacianSchemes { default Gauss linear corrected; }\n"
              "interpolationSchemes { default linear; }\n"
              "snGradSchemes { default corrected; }\n"
        )
        (case / "system" / "fvSolution").write_text(_STUB % "fvSolution")
        msh = write_fluent(tube_wall, case / "mesh.msh")

        conv = subprocess.run(
            [*foam, "fluent3DMeshToFoam", "-case", str(case), str(msh)],
            cwd=tmp, capture_output=True, text=True, timeout=300,
        )
        assert conv.returncode == 0, (conv.stdout + conv.stderr)[-2000:]
        check = subprocess.run(
            [*foam, "checkMesh", "-case", str(case)],
            cwd=tmp, capture_output=True, text=True, timeout=300,
        )
        out = check.stdout + check.stderr
        assert check.returncode == 0 and "Mesh OK" in out, out[-2000:]
        assert int(re.search(r"cells:\s*(\d+)", out).group(1)) == tube_wall.grid.n_cells
        vol = float(
            re.search(r"Total volume = (\d+\.?\d*(?:[eE][+-]?\d+)?)", out).group(1)
        )
        assert vol == pytest.approx(tube_wall.quality.total_volume, rel=1e-3)
        # zone names became patches
        boundary = (case / "constant" / "polyMesh" / "boundary").read_text()
        for name in tube_wall.boundary:
            assert name in boundary

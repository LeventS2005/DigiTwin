"""V22 boundary layers: sphere-shell analytic truth, exact geometric
growth, orientation robustness, wedge polyMesh export, loud branches,
session/CLI e2e."""
from __future__ import annotations

import json

import numpy as np
import pytest
import pyvista as pv

from meshforge.export.foam import write_polymesh
from meshforge.meshing.base import BoundaryGroup, WallMesh
from meshforge.meshing.prism import mesh_boundary_layers
from meshforge.quality.volume import volume_quality
from meshforge.synth import generate_primitive

from test_foam import (  # reuse the polyMesh readers + divergence check
    _boundary_volume,
    _read_boundary,
    _read_faces,
    _read_labels,
    _read_points,
)


@pytest.fixture(scope="module")
def sphere() -> pv.PolyData:
    surface, _truth = generate_primitive("solid_sphere", radius=1.0)
    return surface


def test_sphere_shell_volume_and_counts(sphere):
    first, growth, n = 0.02, 1.3, 5
    wall = mesh_boundary_layers(sphere, first, n_layers=n, growth=growth)
    total = first * (growth**n - 1) / (growth - 1)
    n_tris = sphere.n_cells

    assert wall.quality.n_inverted == 0
    assert wall.grid.n_cells == n_tris * n
    assert wall.grid.n_points == sphere.n_points * (n + 1)
    assert wall.boundary["body"].n_faces == n_tris
    assert wall.boundary["outer"].n_faces == n_tris
    # Faceted sphere shell: 4/3 pi ((r+T)^3 - r^3) within the polygon deficit.
    truth = 4.0 / 3.0 * np.pi * ((1.0 + total) ** 3 - 1.0**3)
    assert wall.quality.total_volume == pytest.approx(truth, rel=0.02)
    assert any("boundary layers" in note for note in wall.notes)
    assert any("self-intersection" in note for note in wall.notes)


def test_layer_distances_follow_geometric_growth_exactly(sphere):
    first, growth, n = 0.05, 1.4, 4
    wall = mesh_boundary_layers(sphere, first, n_layers=n, growth=growth)
    pts = np.asarray(wall.grid.points)
    n_verts = sphere.n_points
    base = pts[:n_verts]
    # Offsets are along the smoothed normals: per-layer distance from the
    # base vertex is EXACTLY the cumulative geometric sum, by construction.
    for k in range(1, n + 1):
        d = np.linalg.norm(pts[k * n_verts : (k + 1) * n_verts] - base, axis=1)
        want = first * (growth**k - 1) / (growth - 1)
        np.testing.assert_allclose(d, want, rtol=1e-9)


def test_inward_oriented_input_gives_identical_mesh(sphere):
    flipped = pv.PolyData(
        np.asarray(sphere.points).copy(),
        np.column_stack(
            [np.full(sphere.n_cells, 3), sphere.faces.reshape(-1, 4)[:, 3:0:-1]]
        ).ravel(),
    )
    a = mesh_boundary_layers(sphere, 0.02, n_layers=2)
    b = mesh_boundary_layers(flipped, 0.02, n_layers=2)
    assert a.quality.total_volume == pytest.approx(b.quality.total_volume, rel=1e-9)
    assert a.grid.n_cells == b.grid.n_cells


def test_loud_branches(sphere):
    with pytest.raises(ValueError, match="first_thickness"):
        mesh_boundary_layers(sphere, 0.0)
    with pytest.raises(ValueError, match="n_layers"):
        mesh_boundary_layers(sphere, 0.1, n_layers=0)
    with pytest.raises(ValueError, match="growth"):
        mesh_boundary_layers(sphere, 0.1, growth=0.9)
    with pytest.raises(ValueError, match="normal_smoothing"):
        mesh_boundary_layers(sphere, 0.1, normal_smoothing=-1)
    plane = pv.Plane().triangulate()
    with pytest.raises(ValueError, match="closed manifold"):
        mesh_boundary_layers(plane, 0.1)
    quads = pv.Plane()  # quad faces
    with pytest.raises(ValueError, match="all-triangle"):
        mesh_boundary_layers(quads, 0.1)


# ---------------------------------------------------------------------------
# wedge polyMesh export


def _two_wedge_cube() -> WallMesh:
    """Unit cube split into two wedges along the x=y diagonal."""
    pts = np.array(
        [[0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 1, 0],
         [0, 0, 1], [1, 0, 1], [1, 1, 1], [0, 1, 1]], dtype=float,
    )
    # Positive wedges (bottom wound away from top).
    cells = np.array([6, 0, 2, 1, 4, 6, 5, 6, 0, 3, 2, 4, 7, 6], np.int64)
    grid = pv.UnstructuredGrid(
        cells, np.array([pv.CellType.WEDGE] * 2, np.uint8), pts
    )
    grid.cell_data["region"] = np.ones(2, dtype=np.int32)
    return WallMesh(grid=grid, boundary={}, backend="manual",
                    quality=volume_quality(grid))


def test_two_wedge_cube_polymesh(tmp_path):
    wall = _two_wedge_cube()
    assert wall.quality.n_inverted == 0
    assert wall.quality.total_volume == pytest.approx(1.0)
    polymesh = write_polymesh(wall, tmp_path / "case", default_patch="walls")
    pts = _read_points(polymesh / "points")
    faces = _read_faces(polymesh / "faces")
    neighbour = _read_labels(polymesh / "neighbour")
    owner = _read_labels(polymesh / "owner")
    assert len(faces) == 9 and neighbour == [1] and owner[0] == 0
    assert len(faces[0]) == 4  # the shared diagonal quad is the internal face
    assert _boundary_volume(pts, faces, 1) == pytest.approx(1.0)
    assert _read_boundary(polymesh / "boundary")["walls"]["nFaces"] == "8"


def test_sphere_blayers_polymesh_divergence(tmp_path, sphere):
    wall = mesh_boundary_layers(sphere, 0.03, n_layers=3, growth=1.2)
    polymesh = write_polymesh(wall, tmp_path / "case")
    pts = _read_points(polymesh / "points")
    faces = _read_faces(polymesh / "faces")
    neighbour = _read_labels(polymesh / "neighbour")
    patches = _read_boundary(polymesh / "boundary")
    assert set(patches) == {"body", "outer"}
    assert int(patches["body"]["nFaces"]) == sphere.n_cells
    assert _boundary_volume(pts, faces, len(neighbour)) == pytest.approx(
        wall.quality.total_volume, rel=1e-6
    )


# ---------------------------------------------------------------------------
# session + CLI


def test_session_blayers_then_foam(tmp_path, monkeypatch, sphere):
    monkeypatch.chdir(tmp_path)
    from meshforge.pipeline import MeshingSession, load_workfile, replay

    session = MeshingSession()
    session.generate_synth("straight_tube")
    with pytest.raises(ValueError, match="first layer thickness"):
        session.compute_blayers()
    result = session.compute_blayers(first=0.05, n_layers=3)
    assert result.summary["quality"]["n_inverted"] == 0
    assert session.volmesh.backend == "prism"
    # The CFD chain completes: wedge layers -> polyMesh.
    saved = session.save("foam", tmp_path / "case")
    assert (tmp_path / "case" / "constant" / "polyMesh" / "boundary").is_file()
    assert session.journal[-1]["step"] == "save"

    wf = session.save_workfile(tmp_path / "s.mfwork")
    fresh = load_workfile(wf)
    results = replay(fresh)
    assert [r.step for r in results][1] == "blayers"
    _ = saved


def test_cli_blayers_e2e(tmp_path, sphere):
    from meshforge.cli import main

    vtp = tmp_path / "sphere.vtp"
    sphere.save(vtp)
    rc = main([
        "blayers", str(vtp), "--first", "0.02", "--n-layers", "4",
        "--growth", "1.3", "--outputs", str(tmp_path / "out"),
    ])
    assert rc == 0
    summary = json.loads(
        (tmp_path / "out" / "sphere_blayers.summary.json").read_text()
    )
    assert summary["quality"]["n_inverted"] == 0
    assert summary["boundary_faces"] == {
        "body": sphere.n_cells, "outer": sphere.n_cells
    }

    rc = main(["blayers", str(vtp), "--first", "-1",
               "--outputs", str(tmp_path / "out")])
    assert rc == 1

"""REAL TetGen at the two sites the other tests fake or skip (P4 review fix 2).

* the thrombus (``build_ilt_mesh(..., tet_engine="tetgen")``) on an eccentric,
  kinked sac with an off-centre lumen, and on a bent (C-shaped) tube pair;
* the closed-loop wall (``TetBackend.mesh_wall`` on a torus: the closed-S
  branch of ``fill_shells``, whose cavity marker is an interior point of the
  inner-torus triangles);
* the production hole-point helpers (``ilt._interior_point``,
  ``tetfill.interior_point``) on concave shells.

Everything here runs the real ``tetgen`` child process; the module skips
without it (and the sites that remesh with gmsh skip without that).
"""
from __future__ import annotations

import math

import numpy as np
import pytest
import pyvista as pv

pytest.importorskip("tetgen")
pytest.importorskip("gmsh")

import digitwin.meshing.tetfill as tetfill  # noqa: E402
import digitwin.solve.ilt as ilt  # noqa: E402
from digitwin.geometry.crop import planes_from_report  # noqa: E402
from digitwin.meshing import get_backend  # noqa: E402
from digitwin.meshing.lattice import build_wall_lattice  # noqa: E402
from digitwin.synth import generate  # noqa: E402
from digitwin.synth.generators import eccentric_sac  # noqa: E402

_EDGES = ((0, 1), (1, 2), (2, 0), (0, 3), (1, 3), (2, 3))  # C3D10 mid-edge nodes 5..10


def _winding(point, surface) -> float:
    tri = surface.triangulate().clean()
    return tetfill._solid_angle_winding(point, np.asarray(tri.points, dtype=float),
                                        tri.faces.reshape(-1, 4)[:, 1:])


def _depth(point, surface) -> float:
    """Signed depth of ``point`` inside the closed ``surface`` (negative = outside)."""
    q = pv.PolyData(np.asarray(point, dtype=float).reshape(1, 3)).compute_implicit_distance(
        surface.triangulate().clean())
    d = abs(float(q.point_data["implicit_distance"][0]))
    return d if abs(_winding(point, surface)) > 0.5 else -d


# ---------------------------------------------------------------- the thrombus

Z1 = 40.0
_ECC = dict(length=Z1, shoulder=(8.0, 30.0), kink=6.0, kink_span=(22.0, 34.0),
            z0=0.0, z1=Z1, n_axial=81)


def _eccentric_pair():
    """A sac (r 12 -> 7, 4 mm off the neck axis, 6 mm kink) and a lumen (r 5 -> 3)
    whose bulge sits 6 mm off the neck axis: off-centre in the sac, with a thin
    side, between two parallel flat caps."""
    sac, _ = eccentric_sac(sac_radius=12.0, neck_radius=7.0, bulge=4.0, **_ECC)
    lumen, _ = eccentric_sac(sac_radius=5.0, neck_radius=3.0, bulge=6.0, **_ECC)
    report = {"planes": {
        "distal": {"origin": [0.0, 0.0, 0.0], "normal_out": [0.0, 0.0, -1.0], "height_mm": 0.0},
        "proximal": {"origin": [0.0, 0.0, Z1], "normal_out": [0.0, 0.0, 1.0], "height_mm": Z1},
    }, "landmarks": None}
    return lumen, sac, planes_from_report(report), 3.0


def _bent_pair():
    """Two concentric 90-degree bent tubes (r 6 sac, r 3 lumen, bend radius 14):
    concave, with end caps that are NOT parallel."""
    bend, angle = 14.0, math.pi / 2.0
    sac, _ = generate("curved_tube", radius=6.0, bend_radius=bend, angle=angle,
                      n_theta=64, n_axial=61)
    lumen, _ = generate("curved_tube", radius=3.0, bend_radius=bend, angle=angle,
                        n_theta=64, n_axial=61)
    end = np.array([bend * math.sin(angle), 0.0, bend * (1.0 - math.cos(angle))])
    report = {"planes": {
        "distal": {"origin": [0.0, 0.0, 0.0], "normal_out": [-1.0, 0.0, 0.0], "height_mm": 0.0},
        "proximal": {"origin": end.tolist(),
                     "normal_out": [math.cos(angle), 0.0, math.sin(angle)],
                     "height_mm": bend * angle},
    }, "landmarks": None}
    return lumen, sac, planes_from_report(report), 2.5


@pytest.fixture(scope="module", params=["eccentric", "bent"])
def thrombus(request):
    lumen, sac, planes, size = _eccentric_pair() if request.param == "eccentric" else _bent_pair()
    m = ilt.build_ilt_mesh(lumen, sac, planes, size=size, tet_engine="tetgen")
    return request.param, m, lumen, sac, planes


def _corner_tets(m):
    return m.points[m.tets[:, :4]]  # (n, 4, 3)


def test_thrombus_tetgen_every_tet_positive_in_c3d10_order(thrombus):
    _, m, _, _, _ = thrombus
    assert m.tets.shape[1] == 10 and len(m.tets) > 500
    a, b, c, d = np.moveaxis(_corner_tets(m), 1, 0)
    vol = np.einsum("ij,ij->i", np.cross(b - a, c - a), d - a) / 6.0
    assert vol.min() > 0, f"{int((vol <= 0).sum())} non-positive tets"
    # straight mid-edge nodes in CalculiX order: 5=1-2, 6=2-3, 7=3-1, 8=1-4, 9=2-4, 10=3-4
    for k, (i, j) in enumerate(_EDGES):
        assert np.allclose(m.points[m.tets[:, 4 + k]],
                           0.5 * (m.points[m.tets[:, i]] + m.points[m.tets[:, j]]))


def test_thrombus_tetgen_volume_within_the_codes_own_two_percent(thrombus):
    _, m, _, _, _ = thrombus
    assert abs(m.volume_ml - m.expected_volume_ml) <= 0.02 * m.expected_volume_ml
    a, b, c, d = np.moveaxis(_corner_tets(m), 1, 0)
    measured = float((np.einsum("ij,ij->i", np.cross(b - a, c - a), d - a) / 6.0).sum()) / 1000.0
    assert measured == pytest.approx(m.volume_ml, rel=1e-9)
    assert m.sj_min > 0.02


def test_thrombus_tetgen_every_boundary_face_is_claimed_exactly_once(thrombus):
    _, m, _, _, _ = thrombus
    pressure, tied, free = m.inner_faces, m.outer_faces, m.free_faces
    assert pressure and tied and free  # all three groups non-empty
    claimed = pressure + tied + free
    assert len(set(claimed)) == len(claimed), "a face is claimed by two groups (or twice)"
    # independently: the faces of the tet mesh that belong to exactly one tet
    nodes = m.tets[:, :4]
    seen: dict[tuple, list] = {}
    for t, row in enumerate(nodes):
        for label, loc in ilt._TET_FACES.items():
            seen.setdefault(tuple(sorted(int(row[i]) for i in loc)), []).append((t, label))
    boundary = {tl[0] for tl in seen.values() if len(tl) == 1}
    assert set(claimed) == boundary


def test_thrombus_tetgen_has_nothing_inside_the_lumen_and_stays_in_the_sac(thrombus):
    _, m, lumen, sac, planes = thrombus
    cen = _corner_tets(m).mean(axis=1)
    pick = cen[:: max(1, len(cen) // 250)]
    # The thrombus is bounded by the REMESHED surfaces, whose chords cut the
    # original circle by up to size^2 / (8 r) (~0.4 mm here); a cavity filled
    # by mistake would put centroids >= 2 mm deep.
    slack = 0.5
    # the lumen is re-cut 0.5 mm inside each cap plane, so skip the 1 mm next to them
    away = pick
    for pl in planes:
        away = away[np.abs((away - pl.origin) @ pl.normal) > 1.0]
    assert len(away) > 100
    in_lumen = max(_depth(p, lumen) for p in away)
    assert in_lumen < slack, f"a thrombus tet centroid is {in_lumen:.2f} mm inside the lumen"
    out_sac = min(_depth(p, sac) for p in pick)
    assert out_sac > -slack, f"a thrombus tet centroid is {-out_sac:.2f} mm outside the sac"


def test_thrombus_tetgen_engine_is_recorded(thrombus):
    _, m, _, _, _ = thrombus
    assert m.tet_engine == "tetgen"
    assert m.summary()["tet_engine"] == "tetgen"
    assert "thrombus fill engine: tetgen" in m.notes
    assert not any("gmsh fill" in n for n in m.notes)


def test_thrombus_hole_point_given_to_tetgen_is_in_the_lumen(monkeypatch):
    """The production hole point (``_interior_point(inner)``) reaches the real
    TetGen child: it is one point, and it lies inside the lumen."""
    lumen, sac, planes, size = _eccentric_pair()
    seen = []
    real = tetfill._run_tetgen_child

    def spy(points, tris, markers, holes, *rest):
        seen.append(np.asarray(holes, dtype=float).reshape(-1, 3))
        return real(points, tris, markers, holes, *rest)

    monkeypatch.setattr(tetfill, "_run_tetgen_child", spy)
    ilt.build_ilt_mesh(lumen, sac, planes, size=size, tet_engine="tetgen")
    assert seen and all(h.shape == (1, 3) for h in seen)
    for h in seen:
        assert abs(_winding(h[0], lumen)) > 0.5
        assert abs(_winding(h[0], sac)) > 0.5


# ---------------------------------------------------------- the closed-S wall

TORUS = dict(tube_radius=1.5, ring_radius=6.0)
THICKNESS = 0.3
N_THETA, N_AXIAL = 12, 24


@pytest.fixture(scope="module")
def closed_wall():
    surface, truth = generate("torus", **TORUS)
    holes, flags = [], []
    real_child, real_fill = tetfill._run_tetgen_child, tetfill.fill_shells

    def child(points, tris, markers, hole_pts, *rest):
        holes.append(np.asarray(hole_pts, dtype=float).reshape(-1, 3))
        return real_child(points, tris, markers, hole_pts, *rest)

    def fill(shells, size, **kw):
        flags.append({"closed_s": kw.get("closed_s"), "holes": kw.get("holes", ()),
                      "n_shells": len(shells), "engine": kw.get("engine")})
        return real_fill(shells, size, **kw)

    tetfill._run_tetgen_child, tetfill.fill_shells = child, fill
    try:
        wall = get_backend("tet").mesh_wall(surface, THICKNESS, n_theta=N_THETA,
                                            n_axial=N_AXIAL, tet_engine="tetgen")
    finally:
        tetfill._run_tetgen_child, tetfill.fill_shells = real_child, real_fill
    lattice = build_wall_lattice(surface, THICKNESS, n_theta=N_THETA, n_axial=N_AXIAL, n_layers=1)
    _, _, labels = lattice.boundary_shell()
    return wall, holes, flags, labels


def _tube_distance(xyz):
    """Distance of points from the torus's ring circle (the centreline)."""
    r = np.hypot(xyz[:, 0], xyz[:, 1])
    return np.hypot(r - TORUS["ring_radius"], xyz[:, 2])


def test_closed_s_wall_takes_the_closed_branch_and_marks_the_cavity(closed_wall):
    wall, holes, flags, _ = closed_wall
    assert flags and flags[0]["closed_s"] is True and flags[0]["engine"] == "tetgen"
    assert flags[0]["n_shells"] == 1 and len(flags[0]["holes"]) == 0  # derived inside fill_shells
    # TetGen got exactly one hole marker, and it is in the torus's lumen (rho < 1.5)
    assert len(holes) == 1 and holes[0].shape == (1, 3)
    assert _tube_distance(holes[0])[0] < TORUS["tube_radius"] - 0.2


def test_closed_s_wall_has_no_tet_inside_the_cavity(closed_wall):
    wall, _, _, _ = closed_wall
    surface, _ = generate("torus", **TORUS)
    lattice_shell = build_wall_lattice(surface, THICKNESS, n_theta=N_THETA, n_axial=N_AXIAL,
                                       n_layers=1).boundary_shell()
    pts = np.asarray(wall.grid.points)
    cells = np.asarray(wall.grid.cells).reshape(-1, 5)[:, 1:]
    rho = _tube_distance(pts[cells].mean(axis=1))
    # the lumen is r 1.5 (faceted: apothem 1.45 at 12 around), the wall to r 1.8
    assert rho.min() > 1.40, f"a tet centroid at rho {rho.min():.3f} is in the cavity"
    assert rho.max() < TORUS["tube_radius"] + THICKNESS + 1e-6
    # and the ring axis itself is in no cell
    u = np.linspace(0, 2 * math.pi, 40, endpoint=False)
    axis = np.column_stack([6 * np.cos(u), 6 * np.sin(u), np.zeros_like(u)])
    assert (wall.grid.find_containing_cell(axis) == -1).all()
    # the tets fill the faceted shell exactly: volume = outer - inner of the lattice
    # shell (the smooth-torus formula is ~6% off at 12 x 24 facets, so not used)
    sp, st, lab = lattice_shell
    v = sp[st]
    signed = np.einsum("ij,ij->i", v[:, 0], np.cross(v[:, 1], v[:, 2])).sum() / 6.0
    assert wall.quality.total_volume == pytest.approx(abs(signed), rel=1e-6)
    assert abs(signed) == pytest.approx(2.0 * math.pi**2 * 6.0 * 0.3 * 3.3, rel=0.08)


def test_closed_s_wall_labels_inner_and_outer_by_the_input_triangles(closed_wall):
    wall, _, _, labels = closed_wall
    assert set(wall.boundary) == {"inner", "outer"}  # a closed loop has no end annuli
    n_inner, n_outer = int((labels == 0).sum()), int((labels == 1).sum())
    assert n_inner == n_outer == 2 * N_THETA * N_AXIAL
    assert wall.boundary["inner"].n_faces == n_inner
    assert wall.boundary["outer"].n_faces == n_outer
    # and by geometry: inner faces on the lumen, outer faces on the outer skin
    pts = np.asarray(wall.grid.points)
    for name, target in (("inner", 1.5), ("outer", 1.8)):
        fn = np.asarray(wall.boundary[name].face_nodes)
        rho = _tube_distance(pts[fn].reshape(-1, 3)).reshape(len(fn), 3)
        assert np.abs(rho - target).max() < 0.12, name


def test_closed_s_wall_cells_positive_and_engine_noted(closed_wall):
    wall, _, _, _ = closed_wall
    assert wall.quality.n_inverted == 0
    cs = wall.grid.compute_cell_sizes(length=False, area=False, volume=True)
    assert float(np.asarray(cs.cell_data["Volume"]).min()) > 0
    assert "tet engine: tetgen" in wall.notes
    assert any(n.startswith("tetgen ") and "tets on discrete shell" in n for n in wall.notes)
    assert any("closed-loop lattice" in n for n in wall.notes)


# ------------------------------------------------- the hole-point helpers

def _c_shape():
    """A 270-degree C-shaped tube: its centroid is on the open side, outside."""
    surf, _ = generate("curved_tube", radius=2.0, bend_radius=10.0, angle=1.5 * math.pi,
                       n_theta=48, n_axial=97)
    return surf


def _arc_distance(xyz, bend=10.0):
    """Distance from the curved_tube centreline (circle of radius ``bend`` in the
    x-z plane through (0, 0, 0), centred (0, 0, bend))."""
    x, y, z = (np.asarray(xyz, dtype=float).reshape(-1, 3)).T
    return np.hypot(np.hypot(x, z - bend) - bend, y)


@pytest.mark.parametrize("helper", ["ilt", "tetfill"])
def test_interior_point_on_a_c_shaped_tube(helper):
    surf = _c_shape()
    assert abs(_winding(surf.points.mean(axis=0), surf)) < 0.5  # the centroid fails here
    if helper == "ilt":
        p = ilt._interior_point(surf)
    else:
        tri = surf.triangulate().clean()
        p = tetfill.interior_point(np.asarray(tri.points, dtype=float),
                                   tri.faces.reshape(-1, 4)[:, 1:])
    assert abs(_winding(p, surf)) > 0.5
    assert _arc_distance(p)[0] < 2.0 - 0.3  # in the lumen, not at the skin
    # and it is not the (outside) centroid in disguise
    assert np.linalg.norm(p - surf.points.mean(axis=0)) > 1.0


def test_interior_point_on_the_eccentric_lumen_and_sac():
    lumen, sac, _, _ = _eccentric_pair()
    for surf in (lumen, sac):
        p = ilt._interior_point(surf)
        assert abs(_winding(p, surf)) > 0.5
        # well inside: farther from the skin than a skin-hugging vertex would be
        d = _depth(p, surf)
        assert d > 0.5, f"interior point only {d:.3f} mm from the surface"
    # the lumen's point is also inside the sac (it is not in the thrombus region)
    assert abs(_winding(ilt._interior_point(lumen), sac)) > 0.5


def test_interior_point_on_the_closed_s_inner_torus_triangles():
    """What fill_shells does for the closed-S wall: an interior point of the
    label-0 triangles of the one boundary shell."""
    surface, _ = generate("torus", **TORUS)
    lattice = build_wall_lattice(surface, THICKNESS, n_theta=N_THETA, n_axial=N_AXIAL, n_layers=1)
    pts, tris, labels = lattice.boundary_shell()
    inner = tris[labels == 0]
    p = tetfill.interior_point(pts, inner)
    assert abs(tetfill._solid_angle_winding(p, pts, inner)) > 0.5
    assert _tube_distance(p.reshape(1, 3))[0] < TORUS["tube_radius"] - 0.2

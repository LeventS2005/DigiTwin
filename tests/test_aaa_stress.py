"""D88 (M3): AAA wall stress — units, wall read-back, refusals, end zones,
and the CalculiX benchmarks (Lamé/Laplace cylinder, a sphere-like bulge, a
fusiform sac, linearity, modulus and thickness independence).

Everything below the ``ccx`` marker skips cleanly without CalculiX.
"""
from __future__ import annotations

import functools
import json
import re
from pathlib import Path

import numpy as np
import pytest

import meshforge.solve.aaa_metrics as am
from meshforge.pipeline import MeshingSession
from meshforge.solve import find_ccx

P140 = 140 * 1.33322e-4  # MPa

requires_ccx = pytest.mark.skipif(find_ccx() is None, reason="CalculiX (ccx) not available")


@functools.lru_cache(maxsize=None)
def _wall(shape: str, t: float = 1.5, nth: int = 24, nax: int = 30, nl: int = 2, **kw):
    """A swept wall on a synthetic surface (cached: tests must not mutate it)."""
    s = MeshingSession()
    s.generate_synth(shape, **dict(kw))
    s.compute_centerline(voxel_size=1.0)
    s.compute_volmesh(thickness=t, n_theta=nth, n_axial=nax, n_layers=nl, curvature_cap=0.9)
    assert s.volmesh.quality.n_inverted == 0
    return s.volmesh


def _cylinder(nth=24, nax=30, nl=2, t=1.5):
    return _wall("straight_tube", t, nth, nax, nl, radius=10.0, length=100.0,
                 n_theta=96, n_axial=101)


SAC = dict(radius=10.0, sac_radius=25.0, sac_center=120.0, sac_sigma=40.0, flare=0.0,
           length=240.0, n_theta=96, n_axial=241)


def _fusiform(t=1.5):
    return _wall("aneurysm_tube", t, 24, 40, 2, **SAC)


def _pws_keys(obj) -> list:
    """Every ``pws_mpa`` value at any depth (a reader may search the whole JSON)."""
    if isinstance(obj, dict):
        return [v for k, v in obj.items() if k == "pws_mpa"] + [
            x for v in obj.values() for x in _pws_keys(v)]
    if isinstance(obj, list):
        return [x for v in obj for x in _pws_keys(v)]
    return []


def _clean_summary(wall) -> dict:
    return json.loads(json.dumps(wall.summary()))


# ---------------------------------------------------------------------------
# units and helpers


def test_mmhg_to_mpa():
    assert am.mmhg_to_mpa(1) == pytest.approx(1.33322e-4, rel=1e-12)
    assert am.mmhg_to_mpa(140) == pytest.approx(0.01866508, rel=1e-7)
    assert am.mmhg_to_mpa(120) == pytest.approx(0.01599864, rel=1e-7)
    # 760 mmHg = 1 atm = 101 325 Pa (to the 133.322 Pa/mmHg rounding, -2.8e-6)
    assert am.mmhg_to_mpa(760) == pytest.approx(0.101325, rel=5e-6)
    for bad in (0, -120, float("nan"), float("inf")):
        with pytest.raises(ValueError, match="positive number of mmHg"):
            am.mmhg_to_mpa(bad)


def test_weighted_percentile_matches_numpy_and_weights():
    rng = np.random.default_rng(3)
    v = rng.normal(size=1001)
    for q in (50.0, 99.0):
        assert am.weighted_percentile(v, np.ones_like(v), q) == pytest.approx(
            np.percentile(v, q, method="inverted_cdf"))
    # 99 % of the weight sits on the value 1, 1 % on 10: p99 is 1, p99.5 is 10
    vals, w = np.array([10.0, 1.0]), np.array([1.0, 99.0])
    assert am.weighted_percentile(vals, w, 99.0) == 1.0
    assert am.weighted_percentile(vals, w, 99.5) == 10.0
    with pytest.raises(ValueError):
        am.weighted_percentile(np.array([]), np.array([]), 99.0)


def test_crop_report_from_journal_is_the_current_surfaces_crop():
    crop = {"landmarks": {"neck_d_equiv_mm": 22.0}}
    j = [{"step": "load"}, {"step": "crop", "summary": {"crop": crop}}, {"step": "volmesh"}]
    assert am.crop_report_from_journal(j) == crop
    assert am.crop_report_from_journal([{"step": "crop", "summary": {"crop": crop}},
                                        {"step": "load"}, {"step": "volmesh"}]) is None
    assert am.crop_report_from_journal([]) is None


# ---------------------------------------------------------------------------
# wall read-back (no ccx)


def test_wall_vtu_roundtrip_recovers_the_boundary_groups(tmp_path):
    wall = _cylinder()
    path = tmp_path / "w.vtu"
    wall.grid.save(path)
    (tmp_path / "w.summary.json").write_text(json.dumps(wall.summary()))
    back, summary = am.read_wall(path)
    assert summary == _clean_summary(wall)
    assert set(back.boundary) == {"inner", "outer", "inlet", "outlet"}
    for name, g in wall.boundary.items():
        got = back.boundary[name]
        assert got.n_faces == g.n_faces
        assert {tuple(sorted(f)) for f in got.face_nodes.tolist()} == {
            tuple(sorted(f)) for f in g.face_nodes.tolist()}
        assert sorted(got.cell_ids.tolist()) == sorted(g.cell_ids.tolist())
    assert am.wall_refusals(back, summary) == []


def test_read_wall_refuses_non_swept_meshes(tmp_path):
    import pyvista as pv

    with pytest.raises(ValueError, match="all hexahedra"):
        am.wall_from_grid(pv.ImageData(dimensions=(3, 3, 3)).cast_to_unstructured_grid()
                          .triangulate())
    corners = np.array([[0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 1, 0],
                        [0, 0, 1], [1, 0, 1], [1, 1, 1], [0, 1, 1]], dtype=float)
    one_hex = pv.UnstructuredGrid(np.array([8, *range(8)]),
                                  np.array([pv.CellType.HEXAHEDRON]), corners)
    with pytest.raises(ValueError, match="circumferential sides"):
        am.wall_from_grid(one_hex)
    with pytest.raises(FileNotFoundError):
        am.read_wall(tmp_path / "missing.vtu")


# ---------------------------------------------------------------------------
# refusals (no ccx: a refused wall is never solved)


def _refused(wall, summary, tmp_path):
    # out_dir in tmp: a broken guard must not write a deck into the repo
    res = am.run_aaa_stress(wall, summary, out_dir=tmp_path, ccx="/nonexistent/ccx")
    assert res.grid is None and res.pws_mpa is None
    assert _pws_keys(res.summary) == [None]  # present, the sentinel
    assert res.summary["systolic"] is None and res.refused == res.summary["refused"]
    assert "REFUSED" in res.to_text() and "investigational" in res.to_text()
    return " | ".join(res.refused)


def test_refuses_a_wall_without_its_summary(tmp_path):
    assert "no mesh summary" in _refused(_cylinder(), None, tmp_path)


def test_refuses_an_inverted_wall(tmp_path):
    wall = _cylinder()
    grid = wall.grid.copy()
    cells = np.asarray(grid.cells).reshape(-1, 9).copy()
    cells[0, 1:] = cells[0, [5, 6, 7, 8, 1, 2, 3, 4]]  # flip one hex inside out
    import pyvista as pv

    bad = am.wall_from_grid(pv.UnstructuredGrid(cells.ravel(), grid.celltypes, grid.points))
    assert bad.quality.n_inverted >= 1
    assert "inverted cell" in _refused(bad, _clean_summary(wall), tmp_path)
    summ = _clean_summary(wall)
    summ["quality"]["n_inverted"] = 3  # the summary says so even if the file does not
    assert "records 3 inverted" in _refused(_cylinder(), summ, tmp_path)


def test_refuses_capped_rays(tmp_path):
    summ = _clean_summary(_cylinder())
    summ["lattice"]["n_capped"] = 4
    assert re.search(r"4 of \d+ wall rays capped", _refused(_cylinder(), summ, tmp_path))
    summ["lattice"].update(n_capped_tilt=1, n_capped_concave=3)  # a planes-sweep summary
    assert "(tilt 1, concavity 3)" in _refused(_cylinder(), summ, tmp_path)


def test_refuses_floored_rays(tmp_path):
    summ = _clean_summary(_cylinder())
    summ["lattice"]["n_floored"] = 2
    assert re.search(r"2 of \d+ wall rays floored", _refused(_cylinder(), summ, tmp_path))


def test_refuses_missing_ray_counts_and_a_foreign_summary(tmp_path):
    summ = _clean_summary(_cylinder())
    del summ["lattice"]
    assert "no wall-ray counts" in _refused(_cylinder(), summ, tmp_path)
    other = _clean_summary(_cylinder(nax=20))
    assert "belongs to a different mesh" in _refused(_cylinder(), other, tmp_path)


def test_volmesh_records_the_wall_thickness_along_the_normal():
    """D93: the copilot's gate reads ``thickness_normal_median`` from the
    volmesh summary. A centreline sweep's rays meet a sac's shoulders at an
    angle, so its wall can only come out thinner than the ray length."""
    cyl = _cylinder()
    assert _clean_summary(cyl)["lattice"]["thickness_normal_median"] == pytest.approx(1.5, rel=0.01)
    sac = _fusiform()
    assert sac.lattice["sweep"] == "centreline"
    assert sac.lattice["thickness_normal_median"] < 1.5 * 0.999
    assert am.annotate_wall_thickness(type("Bare", (), {"lattice": None})()) is None


def test_cli_refusal_exits_1_and_writes_the_sentinel(tmp_path):
    from meshforge.cli import main

    wall = _cylinder()
    vtu = tmp_path / "w.vtu"
    wall.grid.save(vtu)
    summ = _clean_summary(wall)
    summ["lattice"]["n_capped"] = 1
    (tmp_path / "w.summary.json").write_text(json.dumps(summ))
    rc = main(["aaa-stress", str(vtu), "--outputs", str(tmp_path / "out"),
               "--ccx", "/nonexistent/ccx"])
    assert rc == 1
    out = json.loads((tmp_path / "out" / "aaa_stress" / "w" / "w_aaa_stress.json").read_text())
    assert out["systolic"] is None and "capped" in out["refused"][0]
    assert not (tmp_path / "out" / "aaa_stress" / "w" / "w_aaa_stress.vtu").exists()


# ---------------------------------------------------------------------------
# end zones (no ccx)


def _centres_z(wall):
    return np.asarray(wall.grid.cell_centers().points)[:, 2]


def test_end_zones_one_neck_diameter_from_each_cap_plane():
    wall = _cylinder()
    z = _centres_z(wall)
    ez = am.end_zones(wall, end_zone="neck", thickness=1.5,
                      crop_report={"landmarks": {"neck_d_equiv_mm": 20.0}})
    lo, hi = sorted(c.centroid[2] for c in ez.caps)  # the swept ends: z 0.16 / 99.84
    assert lo == pytest.approx(0.0, abs=0.5) and hi == pytest.approx(100.0, abs=0.5)
    assert np.array_equal(ez.excluded, (z - lo < 20.0) | (hi - z < 20.0))
    # no crop report: the lower-z cap is distal (whichever group it is)
    assert ez.distal.centroid[2] == lo
    assert "lower-z cap" in ez.notes[0]
    assert ez.evaluated_fraction == pytest.approx(0.6, abs=0.02)
    # no crop report: each cap's own inner equivalent diameter (a 24-gon in r=10)
    ez2 = am.end_zones(wall, end_zone="neck", thickness=1.5)
    d24 = 20.0 * np.sqrt(24 * np.sin(2 * np.pi / 24) / (2 * np.pi))
    for cap in ez2.caps:
        assert cap.end_zone_mm == pytest.approx(d24, rel=2e-3)
    near = [np.abs((np.asarray(wall.grid.cell_centers().points) - c.centroid) @ c.normal_in)
            < c.end_zone_mm for c in ez2.caps]
    assert np.array_equal(ez2.excluded, near[0] | near[1])


def test_end_zones_bending_fixed_and_bad_rules():
    wall = _cylinder()
    z = _centres_z(wall)
    ez = am.end_zones(wall, end_zone="12.5", thickness=1.5)
    lo, hi = sorted(c.centroid[2] for c in ez.caps)
    assert np.array_equal(ez.excluded, (z - lo < 12.5) | (hi - z < 12.5))
    ez = am.end_zones(wall, end_zone="bending", thickness=1.5, nu=0.49)
    r_mid = 0.5 * ez.caps[0].d_equiv + 0.75
    lam = np.sqrt(r_mid * 1.5) / (3 * (1 - 0.49**2)) ** 0.25
    assert ez.caps[0].end_zone_mm == pytest.approx(3 * lam)
    for bad in ("abc", "-1"):
        with pytest.raises(ValueError, match="end_zone"):
            am.end_zones(wall, end_zone=bad, thickness=1.5)


def test_distal_cap_from_the_crop_report_planes():
    wall = _cylinder()
    crop = {"planes": {"distal": {"origin": [0, 0, 100.0], "normal_out": [0, 0, 1.0]}}}
    ez = am.end_zones(wall, end_zone="10", thickness=1.5, crop_report=crop)
    assert ez.distal.centroid[2] == pytest.approx(100.0, abs=0.5)
    assert "crop report's distal plane" in ez.notes[0]


def test_pws_ignores_stress_inside_the_end_zones():
    """A planted 100 MPa in every end-zone cell must not reach the PWS, the
    max or the peak location; the kept cells carry exactly 1 MPa."""
    wall = _cylinder()
    cols = am._columns(wall)
    ez = am.end_zones(wall, end_zone="20", thickness=1.5, volume=cols.volume)
    ip = np.zeros((wall.grid.n_cells, 8, 6))
    ip[..., 0] = 1.0  # uniaxial 1 MPa: von Mises = max principal = 1
    ip[ez.excluded, :, 0] = 100.0
    f = am.stress_fields(ip, cols)
    centres = np.asarray(wall.grid.cell_centers().points)
    m = am.stress_metrics(f, cols, ~ez.excluded, centres, ez.distal)
    assert m["von_mises_p99_mpa"] == pytest.approx(1.0)
    assert m["von_mises_max_mpa"] == pytest.approx(1.0)
    assert m["max_principal_p99_mpa"] == pytest.approx(1.0)
    assert 20.0 <= m["peak"]["height_above_distal_cap_mm"] <= 80.0


def test_tension_is_the_through_thickness_resultant():
    """Uniform hoop stress s on a 1.5 mm wall: T = s * 1.5 on every column,
    and the node field carries it to both surfaces."""
    wall = _cylinder()
    cols = am._columns(wall)
    pts = np.asarray(wall.grid.cell_centers().points)
    th = np.arctan2(pts[:, 1], pts[:, 0])
    s = 0.2
    ip = np.zeros((wall.grid.n_cells, 8, 6))
    ip[..., 0] = (s * np.sin(th) ** 2)[:, None]
    ip[..., 1] = (s * np.cos(th) ** 2)[:, None]
    ip[..., 3] = (-s * np.sin(th) * np.cos(th))[:, None]
    assert np.bincount(cols.cell_column, weights=cols.h_normal) == pytest.approx(1.5, rel=1e-9)
    assert np.bincount(cols.cell_column, weights=cols.layer_frac) == pytest.approx(1.0)
    for f in (am.stress_fields(ip, cols), am.stress_fields(ip, cols, 1.5)):
        # measured (vertex normals) and nominal thickness agree on a round tube
        assert f.tension_col == pytest.approx(s * 1.5, rel=2e-3)
        tp = am._tension_points(cols, f.tension_col, wall.grid.n_points)
        assert np.isfinite(tp).all() and tp == pytest.approx(s * 1.5, rel=2e-3)


# ---------------------------------------------------------------------------
# CalculiX benchmarks


def _solve(wall, tmp, stem, p=P140, **kw):
    """As run_aaa_stress does it: tension = nominal t x the layer-mean stress."""
    cols = am._columns(wall)
    ip, u, res = am.solve_pressure(wall, p, Path(tmp), stem, **kw)
    return cols, am.stress_fields(ip, cols, wall.lattice["thickness_max"]), u, res


def _column_frames(wall, cols, f):
    cc = np.asarray(wall.grid.cell_centers().points)
    ccol = np.zeros((cols.n_columns, 3))
    np.add.at(ccol, cols.cell_column, cc)
    ccol /= np.bincount(cols.cell_column)[:, None]
    et = np.column_stack([-ccol[:, 1], ccol[:, 0], np.zeros(len(ccol))])
    et /= np.linalg.norm(et, axis=1)[:, None]
    ep = np.cross(f.col_normal, et)
    t_hoop = np.einsum("ci,cij,cj->c", et, f.resultant, et)
    t_mer = np.einsum("ci,cij,cj->c", ep, f.resultant, ep)
    return ccol, t_hoop, t_mer


@requires_ccx
def test_cylinder_laplace_and_lame(tmp_path):
    """Straight tube a = 10, t = 1.5, clamped ends, 140 mmHg. Far from the
    ends (plane strain): the hoop tension is exactly P a (Laplace, by
    equilibrium) and the integration-point stresses follow Lamé. Measured:
    tension -0.6 % (24 samples a ring; -> 0 as (pi/n)^2), PWS +0.1 %."""
    wall = _cylinder()
    cols, f, u, _ = _solve(wall, tmp_path, "cyl")
    a, b, nu, t = 10.0, 11.5, 0.49, 1.5
    ccol, t_hoop, t_mer = _column_frames(wall, cols, f)
    mid = np.abs(ccol[:, 2] - 50.0) < 15.0
    assert t_hoop[mid].mean() == pytest.approx(P140 * a, rel=0.01)  # T = P a
    assert t_hoop[mid].mean() / t == pytest.approx(P140 * a / t, rel=0.01)  # P r / t
    assert f.tension_col[mid] == pytest.approx(P140 * a, rel=0.01)
    # axial/hoop: plane strain gives nu (s_r + s_t) / s_t ~ 0.46; the clamped
    # ends of a finite tube relax it a little (measured 0.43); closed ends 0.5
    assert 0.35 < np.median(t_mer[mid] / t_hoop[mid]) < 0.5

    ez = am.end_zones(wall, end_zone="neck", thickness=t, volume=cols.volume)
    m = am.stress_metrics(f, cols, ~ez.excluded, np.asarray(wall.grid.cell_centers().points),
                          ez.distal)
    A = P140 * a * a / (b * b - a * a)
    r_ip = a + 0.5 * (t / 2) * (1 - 1 / np.sqrt(3))  # innermost 2x2x2 Gauss radius
    s_t, s_r, s_z = A * (1 + b * b / r_ip**2), A * (1 - b * b / r_ip**2), 2 * nu * A
    vm = np.sqrt(0.5 * ((s_t - s_r) ** 2 + (s_r - s_z) ** 2 + (s_z - s_t) ** 2))
    assert m["von_mises_p99_mpa"] == pytest.approx(vm, rel=0.01)
    assert m["max_principal_p99_mpa"] == pytest.approx(s_t, rel=0.01)
    assert m["tension_n_per_mm"]["p50"] == pytest.approx(P140 * a, rel=0.01)
    # plane-strain Lamé radial displacement at the inner surface
    inner = np.unique(wall.boundary["inner"].face_nodes)
    pts = np.asarray(wall.grid.points)[inner]
    sel = np.abs(pts[:, 2] - 50.0) < 10.0
    ur = np.einsum("ij,ij->i", u[inner][sel][:, :2], pts[sel][:, :2]) / np.hypot(
        pts[sel][:, 0], pts[sel][:, 1])
    ur_an = (1 + nu) / 100.0 * A * ((1 - 2 * nu) * a + b * b / a)
    assert ur.mean() == pytest.approx(ur_an, rel=0.02)


@requires_ccx
def test_sphere_like_bulge_young_laplace(tmp_path):
    """A Gaussian bulge with sigma^2 = R (R - r): at its apex the meridian's
    radius equals the hoop radius (R = 25), a locally spherical cap. Normal
    equilibrium of the mid-surface (Young-Laplace): T_hoop + T_meridional =
    P a^2 / R_m (a = 25 loaded, R_m = 25.75). Membrane theory drops the
    transverse shear of a curvature that changes over sigma ~ 19 mm, only
    ~4 bending lengths: measured +2.8..+3.3 % on two meshes -> 5 %."""
    R, r = 25.0, 10.0
    wall = _wall("aneurysm_tube", 1.5, 24, 48, 2, radius=r, sac_radius=R, sac_center=80.0,
                 sac_sigma=float(np.sqrt(R * (R - r))), flare=0.0, length=160.0,
                 n_theta=96, n_axial=321)
    cols, f, _, _ = _solve(wall, tmp_path, "sph")
    ccol, t_hoop, t_mer = _column_frames(wall, cols, f)
    apex = np.abs(ccol[:, 2] - 80.0) < 3.0
    assert apex.sum() >= 24
    total = (t_hoop[apex] + t_mer[apex]).mean()
    assert total == pytest.approx(P140 * R * R / (R + 0.75), rel=0.05)
    # nearly equi-biaxial, as on a sphere
    assert np.median(t_mer[apex] / t_hoop[apex]) == pytest.approx(1.0, abs=0.15)


@pytest.fixture(scope="module")
def fusiform_run(tmp_path_factory):
    if find_ccx() is None:
        pytest.skip("CalculiX (ccx) not available")
    return am.run_aaa_stress(_fusiform(), _clean_summary(_fusiform()),
                             out_dir=tmp_path_factory.mktemp("fus"), stem="fus")


@requires_ccx
def test_fusiform_sac_pws_vs_laplace_and_linearity(fusiform_run):
    """Long fusiform sac (R 25 on a 10 mm calibre, sigma 40): PWS against the
    cylinder Laplace stress P R / t. Membrane bounds for its apex: hoop <= P R
    / t (x 1.03 for the inner-surface Lamé rise) and >= (1 - R / 2 R_phi) of it
    (R_phi = sigma^2 / 15 = 107 mm, meridional tension <= P R / 2); von Mises
    is 0.87..1 of the hoop for a meridional/hoop ratio 0..0.5 -> [0.75, 1.05].
    Measured 0.83. The 120/140 mmHg ratio is 120/140 by linearity (two solves)."""
    res = fusiform_run
    assert res.refused == []
    # exactly one pws_mpa anywhere in the JSON: the systolic von Mises p99 (MPa),
    # its measure named beside it — what tissue-index --from-summary reads
    js = json.loads(json.dumps(res.summary))
    assert _pws_keys(js) == [js["systolic"]["von_mises_p99_mpa"]]
    assert js["pws_mpa"] == res.pws_mpa and js["pws_pressure_mmhg"] == 140.0
    assert "Cauchy von Mises" in js["pws_measure"] and "MPa" in js["pws_measure"]
    s, w = res.summary["systolic"], res.summary["whatif"]
    laplace = P140 * 25.0 / 1.5
    assert 0.75 <= s["von_mises_p99_mpa"] / laplace <= 1.05
    assert s["max_principal_p99_mpa"] >= s["von_mises_p99_mpa"]
    assert abs(s["peak"]["height_above_distal_cap_mm"] - 120.0) < 25.0  # in the sac
    assert w["von_mises_p99_mpa"] / s["von_mises_p99_mpa"] == pytest.approx(
        120.0 / 140.0, abs=1e-3)
    assert res.summary["whatif_ratio"]["pws"] == pytest.approx(0.857143, abs=1e-3)
    assert w["tension_n_per_mm"]["p99"] / s["tension_n_per_mm"]["p99"] == pytest.approx(
        120.0 / 140.0, abs=1e-3)
    # outputs: fields on the grid, the assumptions in the text
    g = res.grid
    for name in ("von_mises", "max_principal", "tension", "excluded_end_zone",
                 "von_mises_whatif", "tension_whatif"):
        assert name in g.cell_data and name in g.point_data
    assert "DISP" in g.point_data and np.isfinite(g.point_data["tension"]).all()
    assert g.cell_data["excluded_end_zone"].sum() == int(
        (res.grid.n_cells - res.summary["end_zones"]["evaluated_cells"]))
    text = res.to_text()
    for phrase in ("INVESTIGATIONAL", "wall thickness assumed 1.5 mm",
                   "no intraluminal thrombus modelled", "stiff-linear on the loaded",
                   "not a rupture prediction"):
        assert phrase in text


@requires_ccx
def test_fusiform_apex_satisfies_the_general_laplace_law(tmp_path):
    """T_hoop / R_hoop,m + T_mer / R_mer,m = P a_hoop a_mer / (R_hoop,m R_mer,m)
    at the sac apex (mid-surface radii; measured residual -0.6 %)."""
    wall = _fusiform()
    cols, f, _, _ = _solve(wall, tmp_path, "fusapex")
    ccol, t_hoop, t_mer = _column_frames(wall, cols, f)
    apex = np.abs(ccol[:, 2] - 120.0) < 4.0
    r_phi = 40.0**2 / 15.0
    lhs = t_hoop[apex].mean() / 25.75 + t_mer[apex].mean() / (r_phi + 0.75)
    assert lhs == pytest.approx(P140 * 25.0 * r_phi / (25.75 * (r_phi + 0.75)), rel=0.02)


@requires_ccx
def test_stiff_linear_pws_does_not_depend_on_the_modulus(tmp_path):
    """E x 10 must move the PWS by < 1 %. On a linear, small-strain solve with
    clamped ends and a pressure load it is exact (to the .dat's 7 digits) —
    the stiff-linear premise."""
    wall = _fusiform()
    cols = am._columns(wall)
    ez = am.end_zones(wall, end_zone="neck", thickness=1.5, volume=cols.volume)
    cc = np.asarray(wall.grid.cell_centers().points)
    out = []
    for E in (100.0, 1000.0):
        _, f, u, _ = _solve(wall, tmp_path, f"E{E:g}", E=E)
        out.append((am.stress_metrics(f, cols, ~ez.excluded, cc, ez.distal), np.abs(u).max()))
    (m1, u1), (m2, u2) = out
    r = m2["von_mises_p99_mpa"] / m1["von_mises_p99_mpa"]
    assert abs(r - 1.0) < 0.01
    assert abs(r - 1.0) < 1e-5  # in fact exact
    assert u2 == pytest.approx(u1 / 10.0, rel=1e-4)  # displacement is not


@requires_ccx
def test_wall_tension_is_thickness_independent(tmp_path_factory, fusiform_run):
    """The same loaded (inner) surface with t 1.2 / 1.5 / 2.0 mm: the wall
    tension p50/p99 agree within 2 % (measured <= 0.3 %) while the PWS scales
    ~ 1/t."""
    tmp = Path(tmp_path_factory.mktemp("thick"))
    base = fusiform_run.summary["systolic"]
    for t in (1.2, 2.0):
        wall = _fusiform(t)
        cols, f, _, _ = _solve(wall, tmp, f"t{t:g}")
        ez = am.end_zones(wall, end_zone="neck", thickness=t, volume=cols.volume)
        m = am.stress_metrics(f, cols, ~ez.excluded, np.asarray(wall.grid.cell_centers().points),
                              ez.distal)
        for q in ("p50", "p99"):
            assert m["tension_n_per_mm"][q] == pytest.approx(base["tension_n_per_mm"][q],
                                                             rel=0.02)
        assert m["von_mises_p99_mpa"] * t == pytest.approx(base["von_mises_p99_mpa"] * 1.5,
                                                          rel=0.05)


@requires_ccx
def test_end_zones_that_leave_too_little_are_refused(tmp_path):
    res = am.run_aaa_stress(_cylinder(), _clean_summary(_cylinder()), out_dir=tmp_path,
                            stem="short", end_zone="48")
    assert res.pws_mpa is None and res.grid is not None
    assert "too short for a clamped-end PWS" in res.refused[0]
    assert res.summary["end_zones"]["evaluated_volume_fraction"] < 0.10


@requires_ccx
def test_cli_end_to_end_and_input_unchanged(tmp_path):
    from meshforge.cli import main
    from meshforge.io import file_sha256

    wall = _cylinder()
    vtu = tmp_path / "cyl.vtu"
    wall.grid.save(vtu)
    (tmp_path / "cyl.summary.json").write_text(json.dumps(wall.summary()))
    crop = tmp_path / "crop.json"
    crop.write_text(json.dumps({"landmarks": {"neck_d_equiv_mm": 20.0}}))
    before = {p: file_sha256(p) for p in (vtu, crop, tmp_path / "cyl.summary.json")}
    rc = main(["aaa-stress", str(vtu), "--crop-report", str(crop), "--outputs",
               str(tmp_path / "out"), "--systolic", "140", "--whatif", "120"])
    assert rc == 0
    d = tmp_path / "out" / "aaa_stress" / "cyl"
    out = json.loads((d / "cyl_aaa_stress.json").read_text())
    assert out["end_zones"]["caps"]["distal"]["end_zone_mm"] == 20.0
    assert out["whatif_ratio"]["pws"] == pytest.approx(120 / 140, abs=1e-3)
    assert (d / "cyl_aaa_stress.vtu").is_file() and (d / "cyl_p140mmHg.inp").is_file()
    assert "C3D20R" in (d / "cyl_p140mmHg.inp").read_text()
    assert {p: file_sha256(p) for p in before} == before


@requires_ccx
def test_session_step_journal_replay_and_mfwork_cli(tmp_path, monkeypatch):
    from meshforge.cli import main
    from meshforge.pipeline import STEP_KNOBS, STEP_ORDER, load_workfile, replay

    assert "aaa_stress" in STEP_ORDER and STEP_KNOBS["aaa_stress"]["end_zone"][1] == "neck"
    monkeypatch.chdir(tmp_path)
    s = MeshingSession()
    s.generate_synth("straight_tube", radius=10.0, length=100.0, n_theta=96, n_axial=101)
    s.compute_centerline(voxel_size=1.0)
    s.compute_volmesh(thickness=1.5, n_theta=24, n_axial=30, n_layers=2)
    wf = s.save_workfile(tmp_path / "wall.mfwork")  # without the stress step
    r = s.compute_aaa_stress(end_zone="20")
    pws = r.summary["pws_mpa"]
    assert pws == r.summary["systolic"]["von_mises_p99_mpa"]
    assert pws and "INVESTIGATIONAL" in r.text
    assert (tmp_path / "outputs/aaa_stress/session/session_aaa_stress.vtu").is_file()
    assert s.journal[-1]["step"] == "aaa_stress"

    s.save_workfile(tmp_path / "full.mfwork")
    fresh = load_workfile(tmp_path / "full.mfwork")
    replay(fresh)
    assert fresh.aaa_stress.pws_mpa == pytest.approx(pws, rel=1e-9)

    rc = main(["aaa-stress", str(wf), "--end-zone", "20", "--outputs", str(tmp_path / "o")])
    assert rc == 0
    out = json.loads((tmp_path / "o/aaa_stress/wall/wall_aaa_stress.json").read_text())
    assert out["pws_mpa"] == pytest.approx(pws, rel=1e-9)

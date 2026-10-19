"""D91: intraluminal thrombus (ILT) as a tied solid in the AAA wall-stress model.

Mesh checks run everywhere. The two CalculiX benchmarks (two-layer Lamé, and
a soft thrombus passing the pressure through like a fluid) skip without ccx.
"""
from __future__ import annotations

import functools
import math

import numpy as np
import pytest

import meshforge.solve.aaa_metrics as am
from meshforge.geometry.crop import planes_from_report
from meshforge.pipeline import MeshingSession
from meshforge.solve import find_ccx
from meshforge.solve.ilt import build_ilt_mesh
from meshforge.synth import generate

P140 = 140 * 1.33322e-4  # MPa
requires_ccx = pytest.mark.skipif(find_ccx() is None, reason="CalculiX (ccx) not available")
L = 120.0


def _tube(radius: float):
    surf, _ = generate("straight_tube", radius=radius, length=L, n_theta=96, n_axial=121)
    return surf


def _report(surface) -> dict:
    """Crop-report planes on a straight tube's two flat end caps (z extremes)."""
    z0, z1 = surface.bounds[4], surface.bounds[5]
    return {"planes": {
        "distal": {"origin": [0.0, 0.0, z0], "normal_out": [0.0, 0.0, -1.0], "height_mm": 0.0},
        "proximal": {"origin": [0.0, 0.0, z1], "normal_out": [0.0, 0.0, 1.0], "height_mm": z1 - z0},
    }, "landmarks": None}


@functools.lru_cache(maxsize=None)
def _wall(radius: float):
    s = MeshingSession()
    s.generate_synth("straight_tube", radius=radius, length=L, n_theta=96, n_axial=121)
    s.compute_centerline(voxel_size=1.0)
    # the plane sweep runs cap to cap (end stations = the cap rims), like the demo walls
    s.compute_volmesh(thickness=1.5, n_theta=32, n_axial=40, n_layers=2, curvature_cap=0.9,
                      sweep="planes")
    assert s.volmesh.quality.n_inverted == 0
    return s.volmesh


def test_thrombus_mesh_is_the_shell_between_lumen_and_sac():
    sac, lumen = _tube(10.0), _tube(6.0)
    m = build_ilt_mesh(lumen, sac, planes_from_report(_report(sac)), size=3.0, inset=0.5)
    expected = (abs(sac.volume) - math.pi * 36.0 * (L - 1.0)) / 1000.0
    assert m.volume_ml == pytest.approx(expected, rel=0.02)
    assert m.tets.shape[1] == 10 and m.sj_min > 0.02
    assert m.inner_faces and m.outer_faces
    # straight mid-edge nodes, CalculiX C3D10 order (5 = 1-2, 8 = 1-4, 10 = 3-4)
    t = m.tets[0]
    p = m.points
    assert np.allclose(p[t[4]], 0.5 * (p[t[0]] + p[t[1]]))
    assert np.allclose(p[t[7]], 0.5 * (p[t[0]] + p[t[3]]))
    assert np.allclose(p[t[9]], 0.5 * (p[t[2]] + p[t[3]]))
    # every corner tet positively oriented (CalculiX refuses a negative Jacobian)
    a, b, c, d = (p[m.tets[:, i]] for i in range(4))
    assert (np.einsum("ij,ij->i", np.cross(b - a, c - a), d - a) > 0).all()


def test_thrombus_mesh_refuses_a_lumen_outside_the_sac():
    sac, lumen = _tube(10.0), _tube(12.0)
    with pytest.raises(ValueError, match="outside the sac"):
        build_ilt_mesh(lumen, sac, planes_from_report(_report(sac)))


def test_sac_from_wall_closes_the_inner_surface():
    wall = _wall(14.0)
    sac = am.sac_from_wall(wall)
    assert sac.n_open_edges == 0
    assert abs(sac.volume) == pytest.approx(math.pi * 14.0 ** 2 * L, rel=0.02)


def test_thrombus_needs_the_crop_planes(tmp_path):
    wall = _wall(14.0)
    with pytest.raises(ValueError, match="cap planes"):
        am.run_aaa_stress(wall, json_summary(wall), out_dir=tmp_path, lumen=_tube(8.0),
                          sac=_tube(14.0), crop_report=None, end_zone="30")


def test_thrombus_needs_the_sac_surface(tmp_path):
    wall, sac = _wall(14.0), _tube(14.0)
    with pytest.raises(ValueError, match="sac surface"):
        am.run_aaa_stress(wall, json_summary(wall), out_dir=tmp_path, lumen=_tube(8.0),
                          crop_report=_report(sac), end_zone="30")


def json_summary(wall) -> dict:
    import json

    return json.loads(json.dumps(wall.summary()))


def _two_layer_hoop(a, b, c, E1, n1, E2, n2, p, r):
    """Plane-strain Lamé, inner layer a..b (E1, n1) bonded to b..c (E2, n2),
    pressure p at a, free at c: the hoop stress at radius r in the outer layer."""
    def u(E, n, r):  # radial displacement coefficients of (A, B)
        return (1 + n) / E * (1 - 2 * n) * r, (1 + n) / E / r
    M = np.array([
        [1, -1 / a**2, 0, 0],
        [0, 0, 1, -1 / c**2],
        [1, -1 / b**2, -1, 1 / b**2],
        [*u(E1, n1, b), *(-np.array(u(E2, n2, b)))],
    ])
    A1, B1, A2, B2 = np.linalg.solve(M, [-p, 0, 0, 0])
    return A2 + B2 / r**2


@requires_ccx
def test_two_layer_lame_through_the_tied_thrombus(tmp_path):
    a, b, t = 8.0, 14.0, 1.5
    wall, lumen, sac = _wall(b), _tube(a), _tube(b)
    res = am.run_aaa_stress(wall, json_summary(wall), out_dir=tmp_path, crop_report=_report(sac),
                            lumen=lumen, sac=sac, E_ilt=5.0, end_zone="30", systolic_mmhg=140.0,
                            whatif_mmhg=120.0)
    assert not res.refused and res.summary["thrombus"]["modelled"]
    g = res.grid
    zc = g.cell_centers().points[:, 2]
    mid = np.abs(zc - 0.5 * (zc.min() + zc.max())) < L / 6
    vol = g.compute_cell_sizes()["Volume"]
    fe = np.average(g.cell_data["max_principal"][mid], weights=vol[mid])
    exact = _two_layer_hoop(a, b, b + t, 5.0, 0.49, 100.0, 0.49, P140, b + t / 2)
    print(f"two-layer Lame: FE {fe:.5f} MPa vs exact {exact:.5f} MPa")
    # 2%: the FE lands within 0.6% here. Pressure wrongly left on the wall's
    # inner face (the interface) would read +4% (0.11325 vs 0.10885 MPa
    # analytically): a tied, nearly incompressible thrombus shields the wall
    # almost as much either way, so the tolerance must stay tight
    assert fe == pytest.approx(exact, rel=0.02)
    # and the thrombus really shields the wall (here by ~38%): Lamé for the
    # wall alone, loaded at its own inner radius, is far higher
    wall_alone = _two_layer_hoop(b - 1e-9, b, b + t, 1.0, 0.49, 100.0, 0.49, P140, b + t / 2)
    assert fe < 0.75 * wall_alone
    assert "intraluminal thrombus modelled" in res.to_text()


@requires_ccx
def test_a_soft_thrombus_passes_the_pressure_through(tmp_path):
    wall, lumen, sac = _wall(14.0), _tube(8.0), _tube(14.0)
    rep = _report(sac)
    soft = am.run_aaa_stress(wall, json_summary(wall), out_dir=tmp_path / "soft", crop_report=rep,
                             lumen=lumen, sac=sac, E_ilt=0.01, end_zone="30")
    bare = am.run_aaa_stress(wall, json_summary(wall), out_dir=tmp_path / "bare", crop_report=rep,
                             end_zone="30")
    assert soft.pws_mpa == pytest.approx(bare.pws_mpa, rel=0.05)
    assert bare.summary["thrombus"] == {"modelled": False}

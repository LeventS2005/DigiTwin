"""V25 MYSTRAN solve-level NASTRAN validation.

Ungated: finder plumbing, F06 parser on a captured block, and the
pressure->nodal-forces converter's invariants. Gated on find_mystran
(cross test also needs ccx): the V15 deck solves, the three-way
benchmark (MYSTRAN vs ccx vs Lamé), CLI e2e. MYSTRAN cannot run the
solid-face PLOAD4 form (V25_SPEC/D39), so pressure enters as FORCE."""
from __future__ import annotations

import numpy as np
import pytest

from meshforge.export.deck import (
    BoundaryCondition,
    DeckSpec,
    Material,
    Step,
    pressure_to_nodal_forces,
    write_deck,
)
from meshforge.solve.mystran import (
    find_mystran,
    parse_f06_displacements,
    solve_bdf,
)
from meshforge.solve.runner import find_ccx

A, B, LENGTH, PRESSURE, E_MOD, NU = 3.0, 4.0, 20.0, 1.0, 1000.0, 0.3


@pytest.fixture(scope="module")
def tube_wall():
    from meshforge.geometry import compute_centerline
    from meshforge.meshing import get_backend
    from meshforge.synth import generate

    surface, _ = generate("straight_tube")
    cl = compute_centerline(surface)
    return get_backend("swept").mesh_wall(
        surface, 1.0, centerline=cl, n_theta=24, n_axial=11, n_layers=3
    )


def _spec(wall, target: str) -> DeckSpec:
    return DeckSpec(
        material=Material.elastic(E_MOD, NU),
        step=Step(),
        boundary_conditions=(
            BoundaryCondition("inlet", 1, 3),
            BoundaryCondition("outlet", 1, 3),
        ),
        loads=pressure_to_nodal_forces(wall, "inner", PRESSURE),
        target=target,
    )


# ---------------------------------------------------------------------------
# pressure -> nodal forces (ungated)


def test_pressure_forces_invariants(tube_wall):
    loads = pressure_to_nodal_forces(tube_wall, "inner", PRESSURE)
    pts = np.asarray(tube_wall.grid.points)
    total = np.zeros(3)
    f_by_node: dict[int, np.ndarray] = {}
    for ld in loads:
        assert ld.kind == "cload"
        vec = f_by_node.setdefault(int(ld.target), np.zeros(3))
        vec[ld.dof - 1] += ld.magnitude
        total[ld.dof - 1] += ld.magnitude
    # closed pressurized surface: zero net force
    assert np.abs(total).max() < 1e-9 * 2 * np.pi * A * LENGTH * PRESSURE
    # inflation: every interior-node force points radially OUTWARD
    for nid, vec in f_by_node.items():
        p = pts[nid]
        if 1.0 < p[2] < LENGTH - 1.0:  # away from the rim rows
            r_hat = np.array([p[0], p[1], 0.0])
            r_hat /= np.linalg.norm(r_hat)
            assert np.dot(vec, r_hat) > 0.0
    # total |force| ~ p * 2*pi*a*L_covered (the swept wall covers most of
    # the tube; centerline caps trim the ends — loose sanity bound)
    mag_sum = sum(np.linalg.norm(v) for v in f_by_node.values())
    assert 0.7 * 2 * np.pi * A * LENGTH <= mag_sum <= 1.05 * 2 * np.pi * A * LENGTH


def test_pressure_forces_loud_errors(tube_wall):
    with pytest.raises(ValueError, match="not found"):
        pressure_to_nodal_forces(tube_wall, "nope", 1.0)


# ---------------------------------------------------------------------------
# finder + parser (ungated)


def test_find_mystran_env_override(tmp_path, monkeypatch):
    fake = tmp_path / "mystran"
    fake.write_text("#!/bin/sh\n")
    monkeypatch.setenv("MESHFORGE_MYSTRAN", str(fake))
    assert find_mystran() == str(fake)
    monkeypatch.setenv("MESHFORGE_MYSTRAN", str(tmp_path / "missing"))
    assert find_mystran() != str(tmp_path / "missing")  # falls through


def test_solve_bdf_missing_deck(tmp_path):
    with pytest.raises(FileNotFoundError, match="bdf deck"):
        solve_bdf(tmp_path / "none.bdf", tmp_path)


_F06_SAMPLE = """
 OUTPUT FOR SUBCASE        1

                                                       D I S P L A C E M E N T S
                                              (in global coordinate system at each grid)
           GRID     COORD      T1            T2            T3            R1            R2            R3
                     SYS
              1        0  0.0           0.0           0.0           0.0           0.0           0.0
              2        0  1.2345E-02   -2.0E-03       3.0E-04       0.0           0.0           0.0

 ------------------------------------------------------------------------------------------------------------------------
"""


def test_parse_f06_displacements_sample():
    u = parse_f06_displacements(_F06_SAMPLE)
    assert sorted(u) == [1, 2]
    assert np.allclose(u[2], [1.2345e-02, -2.0e-03, 3.0e-04])
    assert parse_f06_displacements("nothing here") == {}


# ---------------------------------------------------------------------------
# MYSTRAN-gated (trap #16)

needs_mystran = pytest.mark.skipif(
    find_mystran() is None, reason="MYSTRAN not available"
)


@needs_mystran
def test_v15_deck_solves_in_mystran(tube_wall, tmp_path):
    bdf = write_deck(tube_wall, _spec(tube_wall, "nastran"), tmp_path / "tube")
    assert "DISPLACEMENT(PRINT) = ALL" in bdf.read_text()
    result = solve_bdf(bdf, tmp_path / "out")
    assert result.f06_path.is_file()
    assert len(result.grid_ids) == tube_wall.grid.n_points
    assert 0.005 < result.u_max < 0.05  # Lamé-scale displacements


@needs_mystran
@pytest.mark.skipif(find_ccx() is None, reason="CalculiX not available")
def test_cross_solver_benchmark_closes_d29(tube_wall, tmp_path):
    """Same mesh, same nodal loads: MYSTRAN vs ccx vs Lamé (D39).

    Probe-measured: MYSTRAN mid-tube mean u_r(a) 0.75% off Lamé; vs ccx
    median 1.5%, mid-tube max 1.5% (clamped-end max 7.9% is element-
    formulation difference, excluded by the mid-tube window)."""
    from meshforge.solve import solve_deck

    bdf = write_deck(tube_wall, _spec(tube_wall, "nastran"), tmp_path / "x")
    my = solve_bdf(bdf, tmp_path / "out_my")

    inp = write_deck(tube_wall, _spec(tube_wall, "ccx"), tmp_path / "x_ccx")
    cc = solve_deck(inp, tmp_path / "out_ccx")

    # both writers number nodes 1..N in point order
    assert np.array_equal(my.grid_ids, cc.frd.node_ids)
    u_my = my.displacements
    u_cc = cc.frd.fields["DISP"].values[:, :3]
    pts = np.asarray(tube_wall.grid.points)
    scale = float(np.linalg.norm(u_cc, axis=1).max())
    rel = np.linalg.norm(u_my - u_cc, axis=1) / scale
    mid = np.abs(pts[:, 2] - LENGTH / 2) < 3.0
    assert float(np.median(rel)) < 0.03
    assert float(rel[mid].max()) < 0.03

    # MYSTRAN vs the Lamé truth: mean radial displacement at r=a mid-tube
    r = np.linalg.norm(pts[:, :2], axis=1)
    inner_mid = (np.abs(r - A) < 1e-6) & mid
    r_hat = pts[inner_mid, :2] / r[inner_mid, None]
    ur = (u_my[inner_mid, :2] * r_hat).sum(axis=1).mean()
    ur_lame = (
        (1 + NU) / E_MOD * A**2 * PRESSURE / (B**2 - A**2)
        * ((1 - 2 * NU) * A + B**2 / A)
    )
    assert abs(ur / ur_lame - 1.0) < 0.02  # measured 0.75%


@needs_mystran
def test_cli_solve_bdf_e2e(tube_wall, tmp_path, capsys):
    from meshforge.cli import main

    bdf = write_deck(tube_wall, _spec(tube_wall, "nastran"), tmp_path / "tube")
    rc = main(["solve", str(bdf), "--outputs", str(tmp_path / "outputs")])
    out = capsys.readouterr().out
    assert rc == 0
    assert "solve[mystran]" in out
    base = tmp_path / "outputs" / "solve" / "tube"
    assert (base / "tube.F06").is_file()
    assert (base / "tube_results.vtu").is_file()
    import pyvista as pv

    grid = pv.read(base / "tube_results.vtu")
    assert "U_mag" in grid.point_data
    assert grid.n_points == tube_wall.grid.n_points

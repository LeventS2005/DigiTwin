"""V13 in-app solve: frd/dat parsers, ccx runner (fake + real), session step.

Runner failure branches use fake ccx executables so they run WITHOUT
CalculiX; the flagship Lamé read-back is ccx-gated (skips cleanly, trap #16).
"""
from __future__ import annotations

import json
import stat
from pathlib import Path

import numpy as np
import pytest

from digitwin.pipeline import MeshingSession, _parse_fix_spec, _parse_pressure_spec
from digitwin.solve import (
    find_ccx,
    frd_to_grid,
    parse_displacements,
    parse_stresses,
    read_frd,
    solve_deck,
    von_mises,
)

# ---------------------------------------------------------------------------
# frd fixture writers (exact CalculiX fixed-width ASCII format)


def _frd_nodes(ids_pts: list[tuple[int, tuple[float, float, float]]]) -> str:
    lines = [f"    2C{len(ids_pts):30d}{1:38d}"]
    for nid, (x, y, z) in ids_pts:
        lines.append(f" -1{nid:10d}{x:12.5E}{y:12.5E}{z:12.5E}")
    lines.append(" -3")
    return "\n".join(lines) + "\n"


def _frd_elements(elems: list[tuple[int, int, list[int]]]) -> str:
    lines = [f"    3C{len(elems):30d}{1:38d}"]
    for eid, etype, conn in elems:
        lines.append(f" -1{eid:10d}{etype:5d}{0:5d}{1:5d}")
        lines.append(" -2" + "".join(f"{n:10d}" for n in conn))
    lines.append(" -3")
    return "\n".join(lines) + "\n"


def _frd_field(name: str, comps: list[str], data: dict[int, list[float]]) -> str:
    lines = [
        f"  100CL       101 1.00000E+00{len(data):10d}                     0    1           1",
        f" -4  {name:<8s}{len(comps):5d}    1",
    ]
    for k, c in enumerate(comps, start=1):
        lines.append(f" -5  {c:<8s}    1    2{k:5d}    0")
    for nid, vals in data.items():
        lines.append(f" -1{nid:10d}" + "".join(f"{v:12.5E}" for v in vals))
    lines.append(" -3")
    return "\n".join(lines) + "\n"


_STRESS_COMPS = ["SXX", "SYY", "SZZ", "SXY", "SYZ", "SZX"]


def _unit_cube_frd(tmp_path: Path, *, shuffle_ids: bool = False) -> Path:
    """One he8 unit cube; DISP = 0.1*(x,y,z); uniaxial SXX=2 everywhere."""
    corners = [
        (0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (1.0, 1.0, 0.0), (0.0, 1.0, 0.0),
        (0.0, 0.0, 1.0), (1.0, 0.0, 1.0), (1.0, 1.0, 1.0), (0.0, 1.0, 1.0),
    ]
    ids = [3, 1, 8, 2, 7, 4, 6, 5] if shuffle_ids else list(range(1, 9))
    nodes = list(zip(ids, corners))
    disp = {nid: [0.1 * x, 0.1 * y, 0.1 * z] for nid, (x, y, z) in nodes}
    stress = {nid: [2.0, 0.0, 0.0, 0.0, 0.0, 0.0] for nid in ids}
    text = (
        "    1C  meshforge fake\n"
        + _frd_nodes(nodes)
        + _frd_elements([(1, 1, ids)])
        + _frd_field("DISP", ["D1", "D2", "D3", "ALL"], disp)
        + _frd_field("STRESS", _STRESS_COMPS, stress)
        + " 9999\n"
    )
    path = tmp_path / "cube.frd"
    path.write_text(text)
    return path


# ---------------------------------------------------------------------------
# frd parser


def test_read_frd_unit_cube(tmp_path):
    model = read_frd(_unit_cube_frd(tmp_path))
    assert np.array_equal(model.node_ids, np.arange(1, 9))
    assert model.points.shape == (8, 3)
    assert model.points[1].tolist() == [1.0, 0.0, 0.0]
    disp = model.fields["DISP"]
    assert disp.values.shape == (8, 3)  # trailing computed 'ALL' carries no data
    assert disp.components == ("D1", "D2", "D3")
    np.testing.assert_allclose(disp.values, 0.1 * model.points, atol=1e-9)
    stress = model.fields["STRESS"]
    assert stress.values.shape == (8, 6)
    assert stress.components == tuple(_STRESS_COMPS)


def test_read_frd_sorts_scrambled_node_ids(tmp_path):
    model = read_frd(_unit_cube_frd(tmp_path, shuffle_ids=True))
    assert np.array_equal(model.node_ids, np.arange(1, 9))
    # DISP = 0.1 * coords must survive the reordering node-for-node.
    np.testing.assert_allclose(model.fields["DISP"].values, 0.1 * model.points, atol=1e-9)
    grid = frd_to_grid(model)
    assert grid.n_cells == 1
    assert float(grid.compute_cell_sizes()["Volume"][0]) == pytest.approx(1.0)


def test_frd_to_grid_fields_and_von_mises(tmp_path):
    grid = frd_to_grid(read_frd(_unit_cube_frd(tmp_path)))
    assert grid.n_points == 8 and grid.n_cells == 1
    for name in ("DISP", "U_mag", "STRESS", "S_mises"):
        assert name in grid.point_data, name
    # Uniaxial SXX=2 -> von Mises = 2 exactly.
    np.testing.assert_allclose(grid.point_data["S_mises"], 2.0, atol=1e-12)
    np.testing.assert_allclose(
        grid.point_data["U_mag"], 0.1 * np.linalg.norm(grid.points, axis=1), atol=1e-9
    )


def test_von_mises_pure_shear_and_missing_component():
    vals = np.array([[0.0, 0.0, 0.0, 3.0, 0.0, 0.0]])
    np.testing.assert_allclose(
        von_mises(vals, tuple(_STRESS_COMPS)), [3.0 * np.sqrt(3.0)]
    )
    with pytest.raises(ValueError, match="missing component"):
        von_mises(vals[:, :5], ("SXX", "SYY", "SZZ", "SXY", "SYZ"))


def test_read_frd_loud_failures(tmp_path):
    with pytest.raises(FileNotFoundError):
        read_frd(tmp_path / "nope.frd")

    empty = tmp_path / "empty.frd"
    empty.write_text("    1C  header only\n 9999\n")
    with pytest.raises(ValueError, match="no nodal block"):
        read_frd(empty)

    te10 = tmp_path / "te10.frd"
    te10.write_text(
        _frd_nodes([(i, (float(i), 0.0, 0.0)) for i in range(1, 11)])
        + _frd_elements([(1, 6, list(range(1, 11)))])
    )
    with pytest.raises(ValueError, match="te10"):
        read_frd(te10)

    truncated = tmp_path / "trunc.frd"
    truncated.write_text(
        _frd_nodes([(i, (float(i), 0.0, 0.0)) for i in range(1, 9)])
        + f"    3C{1:30d}\n -1{1:10d}{1:5d}{0:5d}{1:5d}\n -3\n"
    )
    with pytest.raises(ValueError, match="truncated element connectivity"):
        read_frd(truncated)

    partial = tmp_path / "partial.frd"
    partial.write_text(
        _frd_nodes([(1, (0.0, 0.0, 0.0)), (2, (1.0, 0.0, 0.0))])
        + _frd_field("DISP", ["D1", "D2", "D3"], {1: [0.0, 0.0, 0.0]})
    )
    with pytest.raises(ValueError, match="covers 1 of 2 nodes"):
        read_frd(partial)


def test_frd_to_grid_needs_elements(tmp_path):
    no_elems = tmp_path / "noel.frd"
    no_elems.write_text(_frd_nodes([(1, (0.0, 0.0, 0.0))]))
    with pytest.raises(ValueError, match="no element block"):
        frd_to_grid(read_frd(no_elems))


# ---------------------------------------------------------------------------
# dat parsers (shared with the V2 benchmarks — see test_deck.py)


def test_dat_parsers_roundtrip_and_absent():
    dat = (
        " displacements (vx,vy,vz) for set INNER and time  0.1000000E+01\n\n"
        "        12  1.500000E-03 -2.000000E-04  0.000000E+00\n\n"
        " stresses (elem, integ.pnt.,sxx,syy,szz,sxy,sxz,syz) for set X\n\n"
        "         3   1  1.0E+00  2.0E+00  3.0E+00  4.0E-01  5.0E-02  6.0E-03\n"
    )
    u = parse_displacements(dat)
    assert set(u) == {12}
    np.testing.assert_allclose(u[12], [1.5e-3, -2.0e-4, 0.0])
    s = parse_stresses(dat)
    assert set(s) == {(3, 1)}
    np.testing.assert_allclose(s[(3, 1)], [1.0, 2.0, 3.0, 0.4, 0.05, 0.006])
    assert parse_displacements("") == {}
    assert parse_stresses("no blocks here\n") == {}


# ---------------------------------------------------------------------------
# runner: find_ccx + fake-ccx failure branches (no CalculiX needed)


def test_find_ccx_env_override(tmp_path, monkeypatch):
    fake = tmp_path / "ccx"
    fake.write_text("#!/bin/sh\n")
    monkeypatch.setenv("MESHFORGE_CCX", str(fake))
    assert find_ccx() == str(fake)
    monkeypatch.setenv("MESHFORGE_CCX", str(tmp_path / "missing"))
    assert find_ccx() != str(tmp_path / "missing")  # non-file falls through


def _fake_ccx(tmp_path: Path, body: str) -> str:
    exe = tmp_path / "fake_ccx"
    exe.write_text("#!/bin/sh\n" + body)
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    return str(exe)


def _dummy_deck(tmp_path: Path) -> Path:
    deck = tmp_path / "job.inp"
    deck.write_text("*HEADING\nfake\n")
    return deck


def test_solve_deck_missing_deck_and_missing_ccx(tmp_path, monkeypatch):
    with pytest.raises(FileNotFoundError, match="deck not found"):
        solve_deck(tmp_path / "nope.inp", tmp_path / "out", ccx="/bin/sh")
    deck = _dummy_deck(tmp_path)
    monkeypatch.setenv("MESHFORGE_CCX", "")
    monkeypatch.setenv("PATH", str(tmp_path))  # hide any real ccx
    monkeypatch.setattr(
        "digitwin.solve.runner._CCX_FALLBACK", tmp_path / "absent"
    )
    with pytest.raises(RuntimeError, match="not found: set MESHFORGE_CCX"):
        solve_deck(deck, tmp_path / "out")
    with pytest.raises(RuntimeError, match="executable not found"):
        solve_deck(deck, tmp_path / "out", ccx=str(tmp_path / "ghost"))


def test_solve_deck_loud_on_solver_failure(tmp_path):
    deck = _dummy_deck(tmp_path)
    with pytest.raises(RuntimeError, match=r"ccx failed \(exit 3\)"):
        solve_deck(deck, tmp_path / "out", ccx=_fake_ccx(tmp_path, "exit 3\n"))
    with pytest.raises(RuntimeError, match="fails quietly"):
        solve_deck(
            deck, tmp_path / "out",
            ccx=_fake_ccx(tmp_path, 'echo "*ERROR reading the input"\nexit 0\n'),
        )
    with pytest.raises(RuntimeError, match="produced no .frd"):
        solve_deck(
            deck, tmp_path / "out",
            ccx=_fake_ccx(tmp_path, 'echo "Job finished"\nexit 0\n'),
        )
    with pytest.raises(RuntimeError, match="timeout"):
        solve_deck(
            deck, tmp_path / "out",
            ccx=_fake_ccx(tmp_path, "sleep 5\n"), timeout=0.5,
        )


def test_solve_deck_success_with_canned_results(tmp_path):
    canned = _unit_cube_frd(tmp_path)
    deck = _dummy_deck(tmp_path)
    dat = tmp_path / "canned.dat"
    dat.write_text(" displacements (vx,vy,vz) for set S\n\n         1  1.0E-01  0.0E+00  0.0E+00\n")
    exe = _fake_ccx(
        tmp_path, f'cp "{canned}" "$2.frd"\ncp "{dat}" "$2.dat"\nexit 0\n'
    )
    out_dir = tmp_path / "out"
    result = solve_deck(deck, out_dir, ccx=exe)
    assert result.n_nodes == 8
    assert sorted(result.frd.fields) == ["DISP", "STRESS"]
    # DISP = 0.1*coords, farthest corner (1,1,1) -> |U| max = 0.1*sqrt(3).
    assert result.u_max == pytest.approx(0.1 * np.sqrt(3.0))
    assert result.mises_max == pytest.approx(2.0)  # uniaxial SXX=2
    assert result.frd_path == out_dir / "job.frd" and result.frd_path.is_file()
    assert result.dat_path == out_dir / "job.dat" and result.dat_path.is_file()
    assert parse_displacements(result.dat_path.read_text())
    # Scratch ran in a temp dir: nothing landed next to the deck.
    assert not (tmp_path / "job.frd").exists()


# ---------------------------------------------------------------------------
# session knob parsing


def test_parse_fix_spec():
    bcs = _parse_fix_spec("inlet:3,outlet:1-3, walls")
    assert [(b.target, b.first_dof, b.last_dof) for b in bcs] == [
        ("inlet", 3, None), ("outlet", 1, 3), ("walls", 1, 3),
    ]
    assert _parse_fix_spec("") == []
    with pytest.raises(ValueError, match="missing a group name"):
        _parse_fix_spec(":3")
    with pytest.raises(ValueError, match="integer DOFs"):
        _parse_fix_spec("inlet:z")


def test_parse_pressure_spec():
    load = _parse_pressure_spec("inner=1.5")
    assert load.target == "inner" and load.magnitude == 1.5
    with pytest.raises(ValueError, match="NAME=VALUE"):
        _parse_pressure_spec("inner")
    with pytest.raises(ValueError, match="not a number"):
        _parse_pressure_spec("inner=lots")


def test_compute_solve_guards(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    session = MeshingSession()
    with pytest.raises(ValueError, match="session-built decks"):
        session.compute_solve(deck="x.inp", pressure="inner=1")
    with pytest.raises(RuntimeError, match="no volmesh"):
        session.compute_solve()


def test_compute_solve_external_deck_rebuilds_grid(tmp_path, monkeypatch):
    """External deck + no session mesh: the results grid comes from the frd."""
    monkeypatch.chdir(tmp_path)
    canned = _unit_cube_frd(tmp_path)
    exe = _fake_ccx(tmp_path, f'cp "{canned}" "$2.frd"\nexit 0\n')
    session = MeshingSession()
    result = session.compute_solve(deck=str(_dummy_deck(tmp_path)), ccx=exe)
    assert "rebuilt from the frd element block" in result.text
    grid = result.render_hints["mesh"]
    assert grid.n_cells == 1 and "S_mises" in grid.point_data
    assert grid.point_data.active_scalars_name == "S_mises"
    assert session.journal[-1]["step"] == "solve"
    assert session.solve is not None and session.solve.n_nodes == 8


def _tube_session() -> MeshingSession:
    session = MeshingSession()
    session.generate_synth("straight_tube")
    session.compute_centerline()
    session.compute_volmesh(thickness=1.0, n_theta=16, n_axial=4, n_layers=2)
    return session


def test_compute_solve_attaches_to_session_volmesh(tmp_path, monkeypatch):
    """Fake ccx echoes the session mesh geometry back: fields must attach to
    the live volmesh point data (the read-back path, no solver needed)."""
    monkeypatch.chdir(tmp_path)
    session = _tube_session()
    grid = session.volmesh.grid
    pts = np.asarray(grid.points)
    nodes = [(i + 1, tuple(p)) for i, p in enumerate(pts)]
    disp = {i + 1: [0.0, 0.0, 0.001 * p[2]] for i, p in enumerate(pts)}
    frd = tmp_path / "echo.frd"
    frd.write_text(
        _frd_nodes(nodes) + _frd_field("DISP", ["D1", "D2", "D3"], disp)
    )
    exe = _fake_ccx(tmp_path, f'cp "{frd}" "$2.frd"\nexit 0\n')

    result = session.compute_solve(fix="inlet", pressure="inner=1.0", ccx=exe)
    assert "attached to the session volmesh" in result.text
    assert result.render_hints["mesh"] is grid
    np.testing.assert_allclose(
        grid.point_data["DISP"][:, 2], 0.001 * pts[:, 2], atol=1e-6
    )
    assert grid.point_data.active_scalars_name == "U_mag"
    # The session-built deck exists under outputs/ and is a runnable ccx deck.
    deck = Path("outputs/solve/session.inp")
    assert deck.is_file()
    text = deck.read_text()
    assert "TYPE=C3D8," in text and "INNER_SURF, P, 1" in text and "INLET, 1, 3" in text


def test_compute_solve_refuses_inverted_and_bad_knobs(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    session = _tube_session()
    with pytest.raises(ValueError, match="NAME=VALUE"):
        session.compute_solve(pressure="oops")
    session.volmesh.quality.n_inverted = 1
    with pytest.raises(ValueError, match="inverted"):
        session.compute_solve()


def test_workfile_replay_includes_solve(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    canned = _unit_cube_frd(tmp_path)
    exe = _fake_ccx(tmp_path, f'cp "{canned}" "$2.frd"\nexit 0\n')
    session = MeshingSession()
    session.compute_solve(deck=str(_dummy_deck(tmp_path)), ccx=exe)
    wf = session.save_workfile(tmp_path / "s.mfwork")

    from digitwin.pipeline import load_workfile, replay

    fresh = load_workfile(wf)
    results = replay(fresh)
    assert [r.step for r in results] == ["solve"]
    assert fresh.solve is not None and fresh.solve.n_nodes == 8


# ---------------------------------------------------------------------------
# ccx-gated flagship: Lamé through the in-app path (skips without CalculiX)


def test_session_solve_lame_readback_matches_analytic(tmp_path, monkeypatch):
    """The V2 Lamé benchmark, now THROUGH compute_solve: radial displacement
    read from the attached point data must match the analytic solution at
    both walls within 3% (same mesh + tolerances as the deck-level test)."""
    if find_ccx() is None:
        pytest.skip("CalculiX (ccx) not available")
    monkeypatch.chdir(tmp_path)

    session = MeshingSession()
    session.generate_synth("straight_tube")  # a = 3, L = 20
    session.compute_centerline()
    session.compute_volmesh(thickness=1.0, n_theta=32, n_axial=6, n_layers=6)
    wall = session.volmesh
    assert wall.quality.n_inverted == 0

    E, nu, p = 1000.0, 0.3, 1.0
    result = session.compute_solve(
        E=E, nu=nu, pressure="inner=1.0", fix="inlet:3,outlet:3", pin_lateral=1
    )
    assert "attached to the session volmesh" in result.text
    grid = wall.grid
    disp = np.asarray(grid.point_data["DISP"])
    assert np.isfinite(disp).all()
    assert np.isfinite(np.asarray(grid.point_data["S_mises"])).all()

    pts = np.asarray(grid.points)
    r_node = np.hypot(pts[:, 0], pts[:, 1])
    inner_nodes = np.unique(wall.boundary["inner"].face_nodes)
    outer_nodes = np.unique(wall.boundary["outer"].face_nodes)
    a = float(r_node[inner_nodes].mean())
    b = float(r_node[outer_nodes].mean())

    def u_r_analytic(r: float) -> float:
        return (1 + nu) / E * p * a**2 / (b**2 - a**2) * ((1 - 2 * nu) * r + b**2 / r)

    def mean_u_r(nodes: np.ndarray) -> float:
        ur = (disp[nodes, 0] * pts[nodes, 0] + disp[nodes, 1] * pts[nodes, 1]) / r_node[nodes]
        return float(ur.mean())

    assert mean_u_r(inner_nodes) == pytest.approx(u_r_analytic(a), rel=0.03)
    assert mean_u_r(outer_nodes) == pytest.approx(u_r_analytic(b), rel=0.03)
    assert result.summary["u_max"] == pytest.approx(u_r_analytic(a), rel=0.05)


def test_cli_solve_end_to_end(tmp_path):
    """CLI solve on a real deck: results .vtu + summary JSON land in outputs."""
    if find_ccx() is None:
        pytest.skip("CalculiX (ccx) not available")
    from digitwin.cli import main
    from digitwin.export.deck import (
        BoundaryCondition, DeckSpec, Load, Material, Step, lateral_pins, write_deck,
    )
    from digitwin.geometry import compute_centerline
    from digitwin.meshing import get_backend
    from digitwin.synth import generate

    surface, _ = generate("straight_tube")
    cl = compute_centerline(surface)
    wall = get_backend("swept").mesh_wall(
        surface, 1.0, centerline=cl, n_theta=16, n_axial=4, n_layers=2
    )
    spec = DeckSpec(
        material=Material.elastic(1000.0, 0.3),
        step=Step(),
        boundary_conditions=(
            BoundaryCondition("inlet", 3), BoundaryCondition("outlet", 3),
            *lateral_pins(wall),
        ),
        loads=(Load.pressure("inner", 1.0),),
        target="ccx",
    )
    deck = write_deck(wall, spec, tmp_path / "lame_cli")

    rc = main(["solve", str(deck), "--outputs", str(tmp_path / "outputs")])
    assert rc == 0
    out_dir = tmp_path / "outputs" / "solve" / "lame_cli"
    vtu = out_dir / "lame_cli_results.vtu"
    assert vtu.is_file() and (out_dir / "lame_cli.frd").is_file()
    summary = json.loads((out_dir / "lame_cli_solve.json").read_text())
    assert summary["n_nodes"] == wall.grid.n_points
    assert summary["u_max"] > 0 and summary["mises_max"] > 0

    import pyvista as pv

    back = pv.read(vtu)
    assert back.n_cells == wall.grid.n_cells
    assert "S_mises" in back.point_data


def test_cli_solve_error_paths(tmp_path):
    from digitwin.cli import main

    rc = main(["solve", str(tmp_path / "nope.inp"), "--outputs", str(tmp_path / "o")])
    assert rc == 1

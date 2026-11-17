"""V31 transient heat + *FILM convection on CalculiX.

Ungated: card exactness (incl. THE trap — *FILM refuses the surface-name
form *DFLUX accepts, so every film line must repeat the group's *SURFACE
(eid, face) pairs element-wise), loud validation, frd multi-step history
on a hand-written two-step fixture, V24 decks untouched. ccx-gated (skip
cleanly): the steady film tube vs the exact series-resistance solution,
transient lumped-capacitance decay at DIRECT dt << tau, session + CLI e2e.
"""
from __future__ import annotations

import math
import re

import numpy as np
import pytest

from digitwin.export.deck import (
    BoundaryCondition,
    DeckSpec,
    Load,
    Material,
    Step,
    write_deck,
)
from digitwin.solve.frd import read_frd
from digitwin.solve.runner import find_ccx

# ---------------------------------------------------------------------------
# meshes (the V24 fixtures)


def _bar_wall(n_cross: int = 2, n_layers: int = 20, length: float = 20.0):
    """1 x 1 x length rectilinear hex bar via the V5 blocks path."""
    from digitwin.meshing import build_block_mesh, extrude_blocks

    rect = np.array([[0, 0], [1, 0], [1, 1], [0, 1]], dtype=float)
    bm = build_block_mesh([rect], target_size=1.0 / n_cross)
    return extrude_blocks(bm, thickness=length, n_layers=n_layers)


def _tube_wall(n_layers: int = 4):
    """Annulus a=3, b=4, L=20 (the V13/V14 fixture geometry)."""
    from digitwin.geometry import compute_centerline
    from digitwin.meshing import get_backend
    from digitwin.synth import generate

    surface, _ = generate("straight_tube")
    cl = compute_centerline(surface)
    return get_backend("swept").mesh_wall(
        surface, 1.0, centerline=cl, n_theta=16, n_axial=21, n_layers=n_layers
    )


def _transient_spec(loads, *, dt=0.25, period=25.0, t0=100.0, direct=True):
    return DeckSpec(
        material=Material.thermal(10.0, density=1.0, specific_heat=1.0),
        step=Step(
            kind="heat", transient=True, dt=dt, period=period,
            initial_temperature=t0, direct=direct,
        ),
        loads=loads,
        target="ccx",
    )


# ---------------------------------------------------------------------------
# deck cards (ungated)


def test_transient_film_deck_cards(tmp_path):
    wall = _bar_wall(n_layers=4)
    deck = write_deck(
        wall, _transient_spec((Load.film("outer_rim", 20.0, 0.01),)),
        tmp_path / "bar",
    )
    text = deck.read_text()
    assert "*SPECIFIC HEAT\n1\n" in text
    assert "*DENSITY\n1\n" in text
    assert f"*NSET, NSET=NALL, GENERATE\n1, {wall.grid.n_points}, 1\n" in text
    # initial state belongs to the MODEL portion, before *STEP
    init = text.index("*INITIAL CONDITIONS, TYPE=TEMPERATURE\nNALL, 100\n")
    assert init < text.index("*STEP")
    assert "*HEAT TRANSFER, DIRECT\n0.25, 25\n" in text
    assert "*FILM\n" in text
    assert "*NODE FILE\nNT" in text
    assert "*HEAT TRANSFER, STEADY STATE" not in text


def test_film_expansion_equals_surface_pairs(tmp_path):
    """THE trap: ccx fatals on `SURF, F, sink, h` — the writer must expand
    element-wise, one line per (eid, face) pair, exactly the pairs the
    group's *SURFACE block carries (probe finding 1)."""
    wall = _bar_wall(n_layers=4)
    deck = write_deck(
        wall,
        _transient_spec((
            Load.film("outer_rim", 20.0, 0.01),
            Load.film("front", 30.0, 0.02),
        )),
        tmp_path / "bar",
    )
    text = deck.read_text()
    film_block = re.search(r"\*FILM\n((?:\d+, F\d, [^\n]+\n)+)", text)
    assert film_block, "no element-wise *FILM block"
    film_lines = film_block.group(1).splitlines()
    # the surface-name form must never appear
    assert not re.search(r"\*FILM\n\w+_SURF", text)

    expected = []
    for name, sink, h in (("OUTER_RIM", 20.0, 0.01), ("FRONT", 30.0, 0.02)):
        m = re.search(
            rf"\*SURFACE, NAME={name}_SURF, TYPE=ELEMENT\n((?:\d+, S\d\n)+)",
            text,
        )
        assert m, f"no surface block {name}"
        for eid, face in re.findall(r"(\d+), S(\d)", m.group(1)):
            expected.append(f"{eid}, F{face}, {sink:g}, {h:g}")
    assert film_lines == expected


def test_adaptive_transient_drops_direct(tmp_path):
    wall = _bar_wall(n_layers=2)
    deck = write_deck(
        wall,
        _transient_spec((Load.film("outer_rim", 20.0, 0.01),), direct=False),
        tmp_path / "bar",
    )
    text = deck.read_text()
    assert "*HEAT TRANSFER\n0.25, 25\n" in text
    assert "*HEAT TRANSFER, DIRECT" not in text


def test_steady_film_deck_cards(tmp_path):
    """Films are valid on STEADY heat too (the tube benchmark) — without
    any transient card leaking in."""
    wall = _tube_wall(n_layers=2)
    spec = DeckSpec(
        material=Material.thermal(2.0),
        step=Step(kind="heat"),
        boundary_conditions=(BoundaryCondition("inner", 11, 11, 100.0),),
        loads=(Load.film("outer", 20.0, 0.5),),
        target="ccx",
    )
    text = write_deck(wall, spec, tmp_path / "tube").read_text()
    assert "*HEAT TRANSFER, STEADY STATE" in text
    assert "*FILM\n" in text
    assert "NALL" not in text
    assert "*INITIAL CONDITIONS" not in text
    assert "*SPECIFIC HEAT" not in text


def test_v24_decks_unchanged_by_v31(tmp_path):
    """Static and steady-heat decks must not grow any V31 card."""
    wall = _tube_wall(n_layers=2)
    static = DeckSpec(
        material=Material.elastic(1000.0, 0.3),
        step=Step(),
        boundary_conditions=(BoundaryCondition("inlet", 1, 3),),
        loads=(Load.pressure("inner", 1.0),),
        target="ccx",
    )
    text = write_deck(wall, static, tmp_path / "static").read_text()
    for card in ("*FILM", "*INITIAL CONDITIONS", "*SPECIFIC HEAT", "NALL"):
        assert card not in text
    steady = DeckSpec(
        material=Material.thermal(2.0),
        step=Step(kind="heat"),
        boundary_conditions=(
            BoundaryCondition("inner", 11, 11, 100.0),
            BoundaryCondition("outer", 11, 11, 20.0),
        ),
        target="ccx",
    )
    text = write_deck(wall, steady, tmp_path / "steady").read_text()
    assert "*HEAT TRANSFER, STEADY STATE" in text
    for card in ("*FILM", "*INITIAL CONDITIONS", "*SPECIFIC HEAT", "NALL"):
        assert card not in text


# ---------------------------------------------------------------------------
# validation is loud (ungated)


def test_film_and_material_validation():
    with pytest.raises(ValueError, match="film coefficient"):
        Load.film("outer", 20.0, 0.0)
    with pytest.raises(ValueError, match="film coefficient"):
        Load.film("outer", 20.0, -1.0)
    with pytest.raises(ValueError, match="specific heat"):
        Material.thermal(2.0, specific_heat=0.0)


def test_step_transient_validation():
    with pytest.raises(ValueError, match="transient applies to heat"):
        Step(kind="static", transient=True)
    with pytest.raises(ValueError, match="apply to transient heat"):
        Step(kind="frequency", dt=0.1)
    with pytest.raises(ValueError, match="direct applies to transient heat"):
        Step(kind="static", direct=False)
    with pytest.raises(ValueError, match="positive dt"):
        Step(kind="heat", transient=True, period=25.0, initial_temperature=1.0)
    with pytest.raises(ValueError, match="positive dt"):
        Step(kind="heat", transient=True, dt=-1.0, period=25.0,
             initial_temperature=1.0)
    with pytest.raises(ValueError, match="positive period"):
        Step(kind="heat", transient=True, dt=0.1, initial_temperature=1.0)
    with pytest.raises(ValueError, match="exceeds the period"):
        Step(kind="heat", transient=True, dt=30.0, period=25.0,
             initial_temperature=1.0)
    with pytest.raises(ValueError, match="never guesses"):
        Step(kind="heat", transient=True, dt=0.1, period=25.0)
    with pytest.raises(ValueError, match="cannot act on a steady heat"):
        Step(kind="heat", dt=0.1)
    with pytest.raises(ValueError, match="requires transient=True"):
        Step(kind="heat", direct=False)


def test_spec_transient_cross_validation(tmp_path):
    # thermal mass is REQUIRED for transient
    with pytest.raises(ValueError, match="thermal mass"):
        DeckSpec(
            material=Material.thermal(10.0),
            step=Step(kind="heat", transient=True, dt=0.25, period=25.0,
                      initial_temperature=100.0),
            target="ccx",
        ).validate()
    # specific_heat cannot act on a steady step
    with pytest.raises(ValueError, match="specific_heat cannot act"):
        DeckSpec(
            material=Material.thermal(10.0, specific_heat=1.0),
            step=Step(kind="heat"),
            target="ccx",
        ).validate()
    # films are heat loads
    with pytest.raises(ValueError, match="belong to heat steps"):
        DeckSpec(
            material=Material.elastic(1000.0, 0.3),
            step=Step(),
            loads=(Load.film("outer", 20.0, 0.5),),
            target="ccx",
        ).validate()
    # unknown film surface is loud at write time
    with pytest.raises(ValueError, match="unknown surface"):
        write_deck(
            _bar_wall(n_layers=2),
            DeckSpec(
                material=Material.thermal(2.0),
                step=Step(kind="heat"),
                loads=(Load.film("nope", 20.0, 0.5),),
                target="ccx",
            ),
            tmp_path / "bad",
        )


def test_session_transient_knob_validation():
    from digitwin.pipeline import MeshingSession

    session = MeshingSession()
    with pytest.raises(ValueError, match="only to session-built"):
        session.compute_solve(deck="x.inp", transient=1)
    with pytest.raises(ValueError, match="only to session-built"):
        session.compute_solve(deck="x.inp", film="outer=20,0.5")
    session.volmesh = _bar_wall(n_layers=2)
    with pytest.raises(ValueError, match="analysis='heat'"):
        session.compute_solve(analysis="static", film="outer_rim=20,0.5")
    with pytest.raises(ValueError, match="analysis='heat'"):
        session.compute_solve(analysis="static", t0=100.0)
    with pytest.raises(ValueError, match="NAME=SINK,H"):
        session.compute_solve(
            analysis="heat", conductivity=1.0, film="outer_rim"
        )
    with pytest.raises(ValueError, match="not two numbers"):
        session.compute_solve(
            analysis="heat", conductivity=1.0, film="outer_rim=x,y"
        )


# ---------------------------------------------------------------------------
# frd multi-step history on hand-written fixtures (ungated)


def _frd_nodes(ids_pts):
    lines = [f"    2C{len(ids_pts):30d}{1:38d}"]
    for nid, (x, y, z) in ids_pts:
        lines.append(f" -1{nid:10d}{x:12.5E}{y:12.5E}{z:12.5E}")
    lines.append(" -3")
    return "\n".join(lines) + "\n"


def _frd_field(name, comps, data, *, time=1.0, header=True):
    lines = []
    if header:
        lines.append(
            f"  100CL       101 {time:.5E}{len(data):10d}"
            f"                     0    1           1"
        )
    lines.append(f" -4  {name:<8s}{len(comps):5d}    1")
    for k, c in enumerate(comps, start=1):
        lines.append(f" -5  {c:<8s}    1    2{k:5d}    0")
    for nid, vals in data.items():
        lines.append(f" -1{nid:10d}" + "".join(f"{v:12.5E}" for v in vals))
    lines.append(" -3")
    return "\n".join(lines) + "\n"


_NODES = [(1, (0.0, 0.0, 0.0)), (2, (1.0, 0.0, 0.0))]


def _two_step_frd(tmp_path):
    text = (
        "    1C  meshforge fake\n"
        + _frd_nodes(_NODES)
        + _frd_field("NDTEMP", ["T"], {1: [90.0], 2: [80.0]}, time=0.5)
        + _frd_field("NDTEMP", ["T"], {1: [70.0], 2: [60.0]}, time=1.0)
        + " 9999\n"
    )
    path = tmp_path / "two_step.frd"
    path.write_text(text)
    return path


def test_frd_history_two_steps(tmp_path):
    model = read_frd(_two_step_frd(tmp_path))
    hist = model.history["NDTEMP"]
    assert [t for t, _ in hist] == [0.5, 1.0]
    np.testing.assert_allclose(hist[0][1].ravel(), [90.0, 80.0])
    np.testing.assert_allclose(hist[1][1].ravel(), [70.0, 60.0])
    # fields keeps the LAST block (V13 behavior, unchanged)
    np.testing.assert_allclose(
        model.fields["NDTEMP"].values.ravel(), [70.0, 60.0]
    )
    assert any("repeats 2x" in note for note in model.notes)


def test_frd_single_step_history(tmp_path):
    text = (
        "    1C  meshforge fake\n"
        + _frd_nodes(_NODES)
        + _frd_field("NDTEMP", ["T"], {1: [90.0], 2: [80.0]}, time=2.5)
        + " 9999\n"
    )
    path = tmp_path / "one.frd"
    path.write_text(text)
    model = read_frd(path)
    assert len(model.history["NDTEMP"]) == 1
    assert model.history["NDTEMP"][0][0] == 2.5
    assert not any("repeats" in note for note in model.notes)


def test_frd_malformed_100cl_is_loud(tmp_path):
    path = _two_step_frd(tmp_path)
    path.write_text(path.read_text().replace("5.00000E-01", "banana     "))
    with pytest.raises(ValueError, match="malformed 100CL"):
        read_frd(path)


def test_frd_block_without_100cl_is_loud(tmp_path):
    text = (
        "    1C  meshforge fake\n"
        + _frd_nodes(_NODES)
        + _frd_field("NDTEMP", ["T"], {1: [90.0], 2: [80.0]}, header=False)
        + " 9999\n"
    )
    path = tmp_path / "headless.frd"
    path.write_text(text)
    with pytest.raises(ValueError, match="no preceding\\s+100CL"):
        read_frd(path)


def test_frd_history_partial_coverage_is_loud(tmp_path):
    text = (
        "    1C  meshforge fake\n"
        + _frd_nodes(_NODES)
        + _frd_field("NDTEMP", ["T"], {1: [90.0], 2: [80.0]}, time=0.5)
        + _frd_field("NDTEMP", ["T"], {1: [70.0]}, time=1.0)  # node 2 missing
        + " 9999\n"
    )
    path = tmp_path / "partial.frd"
    path.write_text(text)
    with pytest.raises(ValueError, match="covers 1 of 2"):
        read_frd(path)


# ---------------------------------------------------------------------------
# ccx-gated benchmarks (trap #16)

needs_ccx = pytest.mark.skipif(find_ccx() is None, reason="CalculiX not available")

# tube: a=3 b=4 L=20 k=2, inner fixed T=100, outer film (20, 0.5)
_A, _B, _K, _H, _TI, _TINF = 3.0, 4.0, 2.0, 0.5, 100.0, 20.0


@needs_ccx
def test_steady_film_tube_exact():
    """Conduction + convective resistance in series: q' = ΔT / (ln(b/a)/2πk
    + 1/2πbh), T(r) = T_i − q'·ln(r/a)/2πk — exact (probe: +0.34% of ΔT
    at the outer wall)."""
    from digitwin.solve import solve_deck

    wall = _tube_wall()
    spec = DeckSpec(
        material=Material.thermal(_K),
        step=Step(kind="heat"),
        boundary_conditions=(BoundaryCondition("inner", 11, 11, _TI),),
        loads=(Load.film("outer", _TINF, _H),),
        target="ccx",
    )
    deck = write_deck(wall, spec, "outputs/solve/v31_film")
    result = solve_deck(deck, "outputs/solve/v31_film_out")
    qprime = (_TI - _TINF) / (
        math.log(_B / _A) / (2 * math.pi * _K) + 1 / (2 * math.pi * _B * _H)
    )
    r = np.linalg.norm(result.frd.points[:, :2], axis=1)
    analytic = _TI - qprime * np.log(r / _A) / (2 * math.pi * _K)
    err = np.abs(result.frd.fields["NDTEMP"].values[:, 0] - analytic).max()
    assert err < 0.01 * (_TI - _TINF)  # <1% of ΔT everywhere
    assert result.time_steps is None  # steady: a single block is no series


@needs_ccx
def test_transient_lumped_capacitance_exact():
    """Slender bar (Bi ≈ 2e-4) under all-face films: the mean temperature
    follows T∞ + ΔT·exp(−t/τ), τ = ρ c_p V / (h A). ccx integrates
    backward Euler (probe: per-step factor exactly 1/(1+Δt/τ)), so the
    truth needs dt << τ: DIRECT dt=0.25 over one τ gave 0.186% of ΔT."""
    from digitwin.solve import solve_deck

    h, rho, cp, k, t0, tinf = 0.01, 1.0, 1.0, 10.0, 100.0, 20.0
    wall = _bar_wall()  # 1 x 1 x 20
    spec = DeckSpec(
        material=Material.thermal(k, density=rho, specific_heat=cp),
        step=Step(kind="heat", transient=True, dt=0.25, period=25.0,
                  initial_temperature=t0),
        loads=(
            Load.film("outer_rim", tinf, h),
            Load.film("front", tinf, h),
            Load.film("back", tinf, h),
        ),
        target="ccx",
    )
    deck = write_deck(wall, spec, "outputs/solve/v31_lumped")
    result = solve_deck(deck, "outputs/solve/v31_lumped_out")

    hist = result.frd.history["NDTEMP"]
    times = np.array([t for t, _ in hist])
    # DIRECT stepping writes EVERY increment: times complete and exact
    np.testing.assert_allclose(times, 0.25 * np.arange(1, 101), atol=1e-9)
    np.testing.assert_array_equal(result.time_steps, times)

    volume, area = 20.0, 4 * 20.0 + 2 * 1.0
    tau = rho * cp * volume / (h * area)  # 24.39
    worst = max(
        abs(float(vals.mean()) - (tinf + (t0 - tinf) * math.exp(-t / tau)))
        for t, vals in hist
    )
    assert worst < 0.01 * (t0 - tinf)  # <1% of ΔT at EVERY written step
    assert "time steps  : 100" in result.to_text()


@needs_ccx
def test_session_transient_film_e2e():
    """Transient film cooling through the session: knobs -> journal ->
    time_steps in the summary."""
    from digitwin.pipeline import MeshingSession

    session = MeshingSession()
    session.volmesh = _bar_wall(n_layers=4)
    result = session.compute_solve(
        analysis="heat", conductivity=10.0, density=1.0, specific_heat=1.0,
        transient=1, dt=0.5, period=5.0, t0=100.0, film="outer_rim=20,0.01",
    )
    steps = result.summary["time_steps"]
    assert steps is not None and len(steps) == 10
    assert steps[-1] == pytest.approx(5.0)
    assert session.journal[-1]["step"] == "solve"
    assert session.journal[-1]["params"]["film"] == "outer_rim=20,0.01"
    assert session.journal[-1]["params"]["transient"] == 1


@needs_ccx
def test_cli_deck_transient_film_solve_e2e(tmp_path, capsys):
    """CLI: deck --transient --film on the tube, then solve — the printed
    result carries the time-series line."""
    from digitwin.cli import main
    from digitwin.synth import generate

    surface, _ = generate("straight_tube")
    src = tmp_path / "tube.vtp"
    surface.save(src)
    rc = main([
        "deck", str(src), "--thickness", "1", "--n-theta", "16",
        "--n-axial", "11", "--n-layers", "2",
        "--material", "thermal", "--conductivity", "2",
        "--density", "1", "--specific-heat", "1",
        "--analysis", "heat", "--transient",
        "--dt", "0.5", "--period", "5", "--t0", "20",
        "--temp", "inner,100", "--film", "outer,20,0.5",
        "--target", "ccx", "--outputs", str(tmp_path / "outputs"),
    ])
    assert rc == 0
    deck_path = tmp_path / "outputs" / "tube_ccx.inp"
    assert deck_path.is_file()
    rc = main(["solve", str(deck_path), "--outputs", str(tmp_path / "outputs")])
    out = capsys.readouterr().out
    assert rc == 0
    assert "time steps  : 10" in out

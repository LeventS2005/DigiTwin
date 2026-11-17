"""V41 environmental radiation (*RADIATE + *PHYSICAL CONSTANTS) and
temperature-dependent conductivity (*CONDUCTIVITY table) on CalculiX.

Ungated: card exactness (element-wise *RADIATE like *FILM — ccx refuses the
surface-name form; *PHYSICAL CONSTANTS present iff a radiate load; k(T) table
rows), loud validation, V24/V31 decks untouched, session/CLI knob refusals.
ccx-gated (skip cleanly): the steady radiation bar vs the exact scalar-root
conduction/radiation balance; the k(T) bar vs the exact variable-k profile; a
combined radiate+k(T) solve; session + CLI e2e.

The absolute-temperature TRAP (probe, session 26, ccx 2.23): a missing/
incomplete *PHYSICAL CONSTANTS makes ccx fail loud, but a WRONG absolute-zero
offset is silently wrong (555 vs 399.9 degC) — so the writer always emits the
card when radiation is present and carries the offset explicitly.
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
    PhysicalConstants,
    Step,
    write_deck,
)
from digitwin.solve.runner import find_ccx

# ---------------------------------------------------------------------------
# mesh (the V24/V31 bar fixture)


def _bar_wall(n_cross: int = 2, n_layers: int = 20, length: float = 20.0):
    """1 x 1 x length rectilinear hex bar via the V5 blocks path."""
    from digitwin.meshing import build_block_mesh, extrude_blocks

    rect = np.array([[0, 0], [1, 0], [1, 1], [0, 1]], dtype=float)
    bm = build_block_mesh([rect], target_size=1.0 / n_cross)
    return extrude_blocks(bm, thickness=length, n_layers=n_layers)


def _rad_spec(loads, *, k=671.0, bc=("front", 700.0), sigma=5.67e-8, off=-273.15):
    return DeckSpec(
        material=Material.thermal(k),
        step=Step(kind="heat"),
        boundary_conditions=(BoundaryCondition(bc[0], 11, 11, bc[1]),),
        loads=loads,
        physical_constants=PhysicalConstants(
            stefan_boltzmann=sigma, absolute_zero=off
        ),
        target="ccx",
    )


# ---------------------------------------------------------------------------
# deck cards (ungated)


def test_radiate_deck_cards(tmp_path):
    wall = _bar_wall(n_layers=4)
    deck = write_deck(
        wall, _rad_spec((Load.radiate("back", 25.0, 0.9),)), tmp_path / "rad"
    )
    text = deck.read_text()
    assert (
        "*PHYSICAL CONSTANTS, ABSOLUTE ZERO=-273.15, STEFAN BOLTZMANN=5.67e-08"
        in text
    )
    # physical constants are model data — they precede the step
    assert text.index("*PHYSICAL CONSTANTS") < text.index("*STEP")
    assert "*RADIATE\n" in text
    assert "*HEAT TRANSFER, STEADY STATE" in text
    assert "*NODE FILE\nNT" in text
    # emissivity + sink ride on each element-wise line
    assert re.search(r"\*RADIATE\n\d+, R\d, 25, 0.9", text)


def test_radiate_expansion_equals_surface_pairs(tmp_path):
    """THE trap: like *FILM, ccx fatals on `SURF, R, sink, eps` — the writer
    expands element-wise, one line per (eid, face) pair with label Rk."""
    wall = _bar_wall(n_layers=4)
    deck = write_deck(
        wall,
        _rad_spec((
            Load.radiate("back", 25.0, 0.9),
            Load.radiate("outer_rim", 30.0, 0.5),
        )),
        tmp_path / "rad",
    )
    text = deck.read_text()
    rad_block = re.search(r"\*RADIATE\n((?:\d+, R\d, [^\n]+\n)+)", text)
    assert rad_block, "no element-wise *RADIATE block"
    rad_lines = rad_block.group(1).splitlines()
    assert not re.search(r"\*RADIATE\n\w+_SURF", text)  # never the surface form

    expected = []
    for name, sink, eps in (("BACK", 25.0, 0.9), ("OUTER_RIM", 30.0, 0.5)):
        m = re.search(
            rf"\*SURFACE, NAME={name}_SURF, TYPE=ELEMENT\n((?:\d+, S\d\n)+)",
            text,
        )
        assert m, f"no surface block {name}"
        for eid, face in re.findall(r"(\d+), S(\d)", m.group(1)):
            expected.append(f"{eid}, R{face}, {sink:g}, {eps:g}")
    assert rad_lines == expected


def test_conductivity_table_cards(tmp_path):
    wall = _bar_wall(n_layers=4)
    spec = DeckSpec(
        material=Material.thermal([(2.0, 0.0), (12.0, 1000.0)]),
        step=Step(kind="heat"),
        boundary_conditions=(
            BoundaryCondition("front", 11, 11, 500.0),
            BoundaryCondition("back", 11, 11, 100.0),
        ),
        target="ccx",
    )
    text = write_deck(wall, spec, tmp_path / "kt").read_text()
    assert "*CONDUCTIVITY\n2, 0\n12, 1000\n" in text
    assert "*PHYSICAL CONSTANTS" not in text  # no radiation here


def test_v24_v31_decks_grow_no_v41_cards(tmp_path):
    """A steady/transient heat deck without radiation or a k-table must not
    sprout *PHYSICAL CONSTANTS / *RADIATE / a table row."""
    wall = _bar_wall(n_layers=2)
    steady = DeckSpec(
        material=Material.thermal(2.0),
        step=Step(kind="heat"),
        boundary_conditions=(BoundaryCondition("front", 11, 11, 100.0),),
        loads=(Load.film("back", 20.0, 0.5),),
        target="ccx",
    )
    text = write_deck(wall, steady, tmp_path / "steady").read_text()
    for card in ("*PHYSICAL CONSTANTS", "*RADIATE"):
        assert card not in text
    # scalar conductivity stays a single value, no (k, T) row
    assert re.search(r"\*CONDUCTIVITY\n2\n", text)


# ---------------------------------------------------------------------------
# validation (ungated)


def test_load_radiate_validation():
    with pytest.raises(ValueError, match="emissivity"):
        Load.radiate("back", 25.0, 0.0)
    with pytest.raises(ValueError, match="emissivity"):
        Load.radiate("back", 25.0, 1.5)
    with pytest.raises(ValueError, match="emissivity"):
        Load.radiate("back", 25.0, -0.1)


def test_physical_constants_validation():
    with pytest.raises(ValueError, match="Stefan-Boltzmann"):
        PhysicalConstants(stefan_boltzmann=0.0, absolute_zero=-273.15)
    with pytest.raises(ValueError, match="Stefan-Boltzmann"):
        PhysicalConstants(stefan_boltzmann=-1.0, absolute_zero=0.0)
    # absolute_zero=0 (Kelvin) is valid
    PhysicalConstants(stefan_boltzmann=5.67e-8, absolute_zero=0.0)


def test_radiate_requires_physical_constants():
    spec = DeckSpec(
        material=Material.thermal(2.0),
        step=Step(kind="heat"),
        boundary_conditions=(BoundaryCondition("front", 11, 11, 100.0),),
        loads=(Load.radiate("back", 25.0, 0.9),),
        target="ccx",
    )
    with pytest.raises(ValueError, match="RADIATE load needs .*PHYSICAL CONSTANTS"):
        spec.validate()


def test_physical_constants_without_radiate_is_loud():
    spec = DeckSpec(
        material=Material.thermal(2.0),
        step=Step(kind="heat"),
        boundary_conditions=(BoundaryCondition("front", 11, 11, 100.0),),
        loads=(Load.film("back", 20.0, 0.5),),
        physical_constants=PhysicalConstants(5.67e-8, -273.15),
        target="ccx",
    )
    with pytest.raises(ValueError, match="only feed .*RADIATE"):
        spec.validate()


def test_radiate_belongs_to_heat_steps():
    """A radiate load on a mechanical step is loud."""
    spec = DeckSpec(
        material=Material.elastic(1000.0, 0.3),
        step=Step(),  # static
        loads=(Load.radiate("outer", 25.0, 0.9),),
        physical_constants=PhysicalConstants(5.67e-8, -273.15),
        target="ccx",
    )
    with pytest.raises(ValueError, match="RADIATE heat loads belong to heat"):
        spec.validate()


def test_conductivity_table_validation():
    with pytest.raises(ValueError, match=">= 2"):
        Material.thermal([(2.0, 0.0)])
    with pytest.raises(ValueError, match="positive"):
        Material.thermal([(2.0, 0.0), (-1.0, 500.0)])
    with pytest.raises(ValueError, match="strictly increase"):
        Material.thermal([(2.0, 500.0), (5.0, 100.0)])
    with pytest.raises(ValueError, match="strictly increase"):
        Material.thermal([(2.0, 100.0), (5.0, 100.0)])


def test_unknown_radiate_surface_is_loud(tmp_path):
    with pytest.raises(ValueError, match="unknown surface"):
        write_deck(
            _bar_wall(n_layers=2),
            _rad_spec((Load.radiate("nope", 25.0, 0.9),)),
            tmp_path / "bad",
        )


# ---------------------------------------------------------------------------
# session / CLI knob validation (ungated)


def test_session_radiate_knob_validation():
    from digitwin.pipeline import MeshingSession

    session = MeshingSession()
    session.volmesh = _bar_wall(n_layers=2)
    # radiate on a non-heat analysis
    with pytest.raises(ValueError, match="analysis='heat'"):
        session.compute_solve(analysis="static", radiate="outer=25,0.9")
    # radiate without the physical constants
    with pytest.raises(ValueError, match="stefan_boltzmann and absolute_zero"):
        session.compute_solve(
            analysis="heat", conductivity=2.0, radiate="back=25,0.9"
        )
    # constants without a radiate load
    with pytest.raises(ValueError, match="only feed radiate"):
        session.compute_solve(
            analysis="heat", conductivity=2.0, stefan_boltzmann=5.67e-8,
            absolute_zero=-273.15,
        )
    # malformed radiate spec
    with pytest.raises(ValueError, match="NAME=SINK,EMISS"):
        session.compute_solve(
            analysis="heat", conductivity=2.0, radiate="back",
            stefan_boltzmann=5.67e-8, absolute_zero=-273.15,
        )
    # conductivity + conductivity_table both
    with pytest.raises(ValueError, match="either conductivity or"):
        session.compute_solve(
            analysis="heat", conductivity=2.0,
            conductivity_table="2@0,12@1000",
        )
    # external deck rejects the V41 knobs
    with pytest.raises(ValueError, match="only to session-built"):
        session.compute_solve(deck="x.inp", radiate="back=25,0.9")


# ---------------------------------------------------------------------------
# ccx-gated solve validation

needs_ccx = pytest.mark.skipif(
    find_ccx() is None, reason="CalculiX not available"
)


def _scalar_root(f, lo, hi, tol=1e-9):
    """Bisection — a dependency-free exact-truth solver for the tests."""
    flo = f(lo)
    for _ in range(200):
        mid = 0.5 * (lo + hi)
        fmid = f(mid)
        if abs(fmid) < tol or 0.5 * (hi - lo) < tol:
            return mid
        if (fmid > 0) == (flo > 0):
            lo, flo = mid, fmid
        else:
            hi = mid
    return 0.5 * (lo + hi)


@needs_ccx
def test_steady_radiation_bar_exact():
    """Fixed T at one end, environmental radiation at the other, sides
    insulated. Steady 1-D balance k*(T1-Tb)/L = eps*sigma*((Tb+off)^4 -
    (Tenv+off)^4) is a scalar root -> exact (probe: 0.000% of dT)."""
    from digitwin.solve import solve_deck

    L, K, EPS, SIGMA, OFF = 20.0, 671.0, 0.9, 5.67e-8, 273.15
    T1, TENV = 700.0, 25.0
    wall = _bar_wall(n_layers=20, length=L)
    spec = _rad_spec(
        (Load.radiate("back", TENV, EPS),), k=K, bc=("front", T1),
        sigma=SIGMA, off=-OFF,
    )
    deck = write_deck(wall, spec, "outputs/solve/v41_rad")
    result = solve_deck(deck, "outputs/solve/v41_rad_out")

    def balance(Tb):
        return K * (T1 - Tb) / L - EPS * SIGMA * (
            (Tb + OFF) ** 4 - (TENV + OFF) ** 4
        )

    tb_exact = _scalar_root(balance, TENV, T1)
    z = result.frd.points[:, 2]
    nt = result.frd.fields["NDTEMP"].values[:, 0]
    tb_ccx = float(nt[np.abs(z - z.max()) < 1e-6].mean())
    assert abs(tb_ccx - tb_exact) < 0.01 * (T1 - TENV)  # <1% of dT
    # the near end sits at the imposed hot temperature
    assert abs(float(nt[np.abs(z - z.min()) < 1e-6].mean()) - T1) < 1e-3


@needs_ccx
def test_temperature_dependent_conductivity_exact():
    """Bar, fixed T at both ends, sides insulated, k(T)=k0+k1*T. Steady q is
    constant so int_{T(x)}^{T_A} k dT = (x/L) int_{T_B}^{T_A} k dT -> the
    exact variable-k profile (probe: 0.017% of dT; clearly non-linear)."""
    from digitwin.solve import solve_deck

    L, TA, TB, k0, k1 = 20.0, 500.0, 100.0, 2.0, 0.01
    wall = _bar_wall(n_layers=40, length=L)
    spec = DeckSpec(
        material=Material.thermal([(k0, 0.0), (k0 + k1 * 1000.0, 1000.0)]),
        step=Step(kind="heat"),
        boundary_conditions=(
            BoundaryCondition("front", 11, 11, TA),
            BoundaryCondition("back", 11, 11, TB),
        ),
        target="ccx",
    )
    deck = write_deck(wall, spec, "outputs/solve/v41_kt")
    result = solve_deck(deck, "outputs/solve/v41_kt_out")

    def kint(T):  # antiderivative of k(T)
        return k0 * T + 0.5 * k1 * T * T

    ka, kb = kint(TA), kint(TB)
    z = result.frd.points[:, 2]
    nt = result.frd.fields["NDTEMP"].values[:, 0]
    exact = np.array([
        _scalar_root(lambda T: kint(T) - (ka - (zz / L) * (ka - kb)), TB, TA)
        for zz in z
    ])
    assert np.abs(nt - exact).max() < 0.01 * (TA - TB)  # <1% of dT
    # the profile is genuinely non-linear (distinguishes k(T) from const k)
    linear = TA + (TB - TA) * (z / L)
    assert np.abs(nt - linear).max() > 0.05 * (TA - TB)


@needs_ccx
def test_radiation_and_kt_combined_solves():
    """Radiation on a temperature-dependent-conductivity bar solves (both
    V41 cards active together) with a physical interior temperature field."""
    from digitwin.solve import solve_deck

    wall = _bar_wall(n_layers=20)
    spec = DeckSpec(
        material=Material.thermal([(50.0, 0.0), (90.0, 1000.0)]),
        step=Step(kind="heat"),
        boundary_conditions=(BoundaryCondition("front", 11, 11, 700.0),),
        loads=(Load.radiate("back", 25.0, 0.9),),
        physical_constants=PhysicalConstants(5.67e-8, -273.15),
        target="ccx",
    )
    deck = write_deck(wall, spec, "outputs/solve/v41_combo")
    result = solve_deck(deck, "outputs/solve/v41_combo_out")
    lo, hi = result.temp_range
    assert 25.0 < lo < hi <= 700.0 + 1e-6


@needs_ccx
def test_session_radiation_e2e():
    """Radiation cooling through the session: knobs -> journal -> temp_range."""
    from digitwin.pipeline import MeshingSession

    session = MeshingSession()
    session.volmesh = _bar_wall(n_layers=8)
    result = session.compute_solve(
        analysis="heat", conductivity=671.0,
        temp="front=700", radiate="back=25,0.9",
        stefan_boltzmann=5.67e-8, absolute_zero=-273.15,
    )
    assert result.summary["temp_range"] is not None
    assert session.journal[-1]["step"] == "solve"
    assert session.journal[-1]["params"]["radiate"] == "back=25,0.9"
    assert session.journal[-1]["params"]["stefan_boltzmann"] == 5.67e-8


@needs_ccx
def test_cli_deck_radiation_and_kt_solve_e2e(tmp_path, capsys):
    """CLI: deck --radiate (+ --conductivity-table) on the tube, then solve."""
    from digitwin.cli import main
    from digitwin.synth import generate

    surface, _ = generate("straight_tube")
    src = tmp_path / "tube.vtp"
    surface.save(src)
    rc = main([
        "deck", str(src), "--thickness", "1", "--n-theta", "16",
        "--n-axial", "11", "--n-layers", "2",
        "--material", "thermal", "--conductivity-table", "50@0,90@1000",
        "--analysis", "heat",
        "--temp", "inner,700", "--radiate", "outer,25,0.9",
        "--stefan-boltzmann", "5.67e-8", "--absolute-zero", "-273.15",
        "--target", "ccx", "--outputs", str(tmp_path / "outputs"),
    ])
    assert rc == 0
    deck_path = tmp_path / "outputs" / "tube_ccx.inp"
    assert deck_path.is_file()
    text = deck_path.read_text()
    assert "*PHYSICAL CONSTANTS" in text and "*RADIATE" in text
    assert "*CONDUCTIVITY\n50, 0\n90, 1000\n" in text
    rc = main(["solve", str(deck_path), "--outputs", str(tmp_path / "outputs")])
    out = capsys.readouterr().out
    assert rc == 0
    assert "T range" in out

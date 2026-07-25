"""V32 buckling + preloaded modes: *BUCKLE and *STATIC→*FREQUENCY,
PERTURBATION on CalculiX.

Ungated: card exactness (single-step *BUCKLE; two-step preloaded layout
with BCs+loads in the static step and outputs in the perturbation step),
loud validation, buckling-factor .dat parser on the captured probe
block. ccx-gated (skip cleanly): the fixed-free Euler column (C3D8R —
probed: C3D8 locks +12%, C3D8I is BROKEN under *BUCKLE at +790%), the
preloaded frequency-shift relation, session + CLI e2e.
"""
from __future__ import annotations

import math

import numpy as np
import pytest

from meshforge.export.deck import (
    BoundaryCondition,
    DeckSpec,
    Load,
    Material,
    Step,
    pressure_to_nodal_forces,
    write_deck,
)
from meshforge.solve.dat import parse_buckling_factors
from meshforge.solve.runner import find_ccx

E, L = 1000.0, 20.0
I_SEC = 1.0 / 12.0
PCR = math.pi**2 * E * I_SEC / (2 * L) ** 2  # fixed-free Euler, 0.51404


def _bar_wall(n_cross: int = 4, n_layers: int = 60):
    """1 x 1 x L rectilinear hex bar via the V5 blocks path."""
    from meshforge.meshing import build_block_mesh, extrude_blocks

    rect = np.array([[0, 0], [1, 0], [1, 1], [0, 1]], dtype=float)
    bm = build_block_mesh([rect], target_size=1.0 / n_cross)
    return extrude_blocks(bm, thickness=L, n_layers=n_layers)


def _buckle_spec(loads, n_modes=6, formulation=None):
    return DeckSpec(
        material=Material.elastic(E, 0.0),
        step=Step(kind="buckle", n_modes=n_modes),
        boundary_conditions=(BoundaryCondition("front", 1, 3),),
        loads=loads,
        formulation=formulation,
        target="ccx",
    )


# ---------------------------------------------------------------------------
# deck cards (ungated)


def test_buckle_deck_cards(tmp_path):
    wall = _bar_wall(n_cross=2, n_layers=4)
    deck = write_deck(
        wall, _buckle_spec((Load.concentrated(0, 3, -1.0),)), tmp_path / "b"
    )
    text = deck.read_text()
    assert "*STEP\n*BUCKLE\n6\n" in text
    assert "*CLOAD\n1, 3, -1\n" in text
    assert "*NODE FILE\nU" in text
    assert "*EL FILE" not in text  # mode shapes are the point (V24 pattern)
    assert "*STATIC" not in text and "*FREQUENCY" not in text
    assert "PERTURBATION" not in text


def test_preloaded_deck_two_step_layout(tmp_path):
    """BCs + loads live in the *STATIC step; the PERTURBATION step carries
    *FREQUENCY and the output requests (ccx keeps step-1 BCs active)."""
    wall = _bar_wall(n_cross=2, n_layers=4)
    spec = DeckSpec(
        material=Material.elastic(E, 0.0, density=1e-3),
        step=Step(kind="frequency", n_modes=4, preloaded=True),
        boundary_conditions=(BoundaryCondition("front", 1, 3),),
        loads=(Load.concentrated(0, 3, -0.1),),
        target="ccx",
    )
    text = write_deck(wall, spec, tmp_path / "pre").read_text()
    static_step, _, freq_step = text.partition("*STEP, PERTURBATION\n")
    assert freq_step, "no PERTURBATION step"
    static_step = static_step[static_step.index("*STEP") :]
    assert "*STATIC" in static_step
    assert "*BOUNDARY\nFRONT, 1, 3" in static_step
    assert "*CLOAD\n1, 3, -0.1" in static_step
    assert "*NODE FILE" not in static_step
    assert freq_step.startswith("*FREQUENCY\n4\n")
    assert "*NODE FILE\nU" in freq_step
    assert "*CLOAD" not in freq_step and "*BOUNDARY" not in freq_step
    # NALL stays available for lateral clamps (V24 discipline)
    assert f"*NSET, NSET=NALL, GENERATE\n1, {wall.grid.n_points}, 1\n" in text


def test_plain_decks_unchanged_by_v32(tmp_path):
    """Static, plain-frequency and heat decks must not grow V32 cards."""
    wall = _bar_wall(n_cross=2, n_layers=4)
    static = DeckSpec(
        material=Material.elastic(E, 0.3),
        step=Step(),
        boundary_conditions=(BoundaryCondition("front", 1, 3),),
        loads=(Load.concentrated(0, 3, -1.0),),
        target="ccx",
    )
    text = write_deck(wall, static, tmp_path / "s").read_text()
    assert "*BUCKLE" not in text and "PERTURBATION" not in text
    assert text.count("*STEP") == 1 and text.count("*END STEP") == 1
    freq = DeckSpec(
        material=Material.elastic(E, 0.0, density=1e-3),
        step=Step(kind="frequency", n_modes=4),
        boundary_conditions=(BoundaryCondition("NALL", 1, 2),),
        target="ccx",
    )
    text = write_deck(wall, freq, tmp_path / "f").read_text()
    assert "*FREQUENCY\n4\n" in text
    assert "*BUCKLE" not in text and "PERTURBATION" not in text
    assert text.count("*END STEP") == 1


# ---------------------------------------------------------------------------
# validation is loud (ungated)


def test_step_buckle_validation():
    with pytest.raises(ValueError, match="n_modes"):
        Step(kind="buckle", n_modes=0)
    with pytest.raises(ValueError, match="nlgeom"):
        Step(kind="buckle", nlgeom=True)
    with pytest.raises(ValueError, match="preloaded applies to frequency"):
        Step(kind="buckle", preloaded=True)
    with pytest.raises(ValueError, match="preloaded applies to frequency"):
        Step(kind="static", preloaded=True)
    with pytest.raises(ValueError, match="apply to transient heat"):
        Step(kind="buckle", dt=0.1)


def test_spec_buckle_cross_validation():
    with pytest.raises(ValueError, match="factors its loads"):
        _buckle_spec(()).validate()
    with pytest.raises(ValueError, match="elastic material"):
        DeckSpec(
            material=Material.thermal(2.0),
            step=Step(kind="buckle"),
            loads=(Load.concentrated(0, 3, -1.0),),
            target="ccx",
        ).validate()
    with pytest.raises(ValueError, match="belong to heat steps"):
        DeckSpec(
            material=Material.elastic(E, 0.0),
            step=Step(kind="buckle"),
            loads=(Load.surface_flux("front", 1.0),),
            target="ccx",
        ).validate()
    with pytest.raises(ValueError, match="static NLGEOM step"):
        DeckSpec(
            material=Material.neo_hookean(1.0, 0.1),
            step=Step(kind="buckle"),
            loads=(Load.concentrated(0, 3, -1.0),),
            target="ccx",
        ).validate()
    with pytest.raises(ValueError, match="SOL 101"):
        DeckSpec(
            material=Material.elastic(E, 0.0),
            step=Step(kind="buckle"),
            loads=(Load.concentrated(0, 3, -1.0),),
            target="nastran",
        ).validate()


def test_spec_preloaded_cross_validation():
    # preloaded without a load is pointless — loud
    with pytest.raises(ValueError, match="factor a preload"):
        DeckSpec(
            material=Material.elastic(E, 0.0, density=1e-3),
            step=Step(kind="frequency", preloaded=True),
            target="ccx",
        ).validate()
    # plain frequency still refuses loads, now pointing at preloaded=True
    with pytest.raises(ValueError, match="preloaded"):
        DeckSpec(
            material=Material.elastic(E, 0.0, density=1e-3),
            step=Step(kind="frequency"),
            loads=(Load.concentrated(0, 3, -1.0),),
            target="ccx",
        ).validate()
    # density stays required with preload
    with pytest.raises(ValueError, match="density"):
        DeckSpec(
            material=Material.elastic(E, 0.0),
            step=Step(kind="frequency", preloaded=True),
            loads=(Load.concentrated(0, 3, -1.0),),
            target="ccx",
        ).validate()


def test_session_buckle_knob_validation():
    from meshforge.pipeline import MeshingSession

    session = MeshingSession()
    with pytest.raises(ValueError, match="static|frequency|heat|buckle"):
        session.compute_solve(analysis="euler")
    with pytest.raises(ValueError, match="analysis='frequency'"):
        session.compute_solve(analysis="buckle", preloaded=1)
    with pytest.raises(ValueError, match="only to session-built"):
        session.compute_solve(deck="x.inp", analysis="buckle")
    with pytest.raises(ValueError, match="only to session-built"):
        session.compute_solve(deck="x.inp", preloaded=1, analysis="frequency")


# ---------------------------------------------------------------------------
# .dat buckling-factor parser on the captured ccx block (ungated)

_PROBE_DAT = """
     B U C K L I N G   F A C T O R   O U T P U T

 MODE NO       BUCKLING
                FACTOR

      1   0.5162394E+00
      2   0.5162394E+00
      3   0.4589461E+01
"""


def test_parse_buckling_factors_probe_block():
    facs = parse_buckling_factors(_PROBE_DAT)
    np.testing.assert_allclose(facs, [0.5162394, 0.5162394, 4.589461])


def test_parse_buckling_factors_absent_and_malformed():
    assert parse_buckling_factors("no block here").size == 0
    assert parse_buckling_factors(
        "     E I G E N V A L U E   O U T P U T\n      1   0.1E+01\n"
    ).size == 0
    bad = _PROBE_DAT.replace(
        "      3   0.4589461E+01", "      3   0.4589461E+01   0.0E+00"
    )
    with pytest.raises(ValueError, match="malformed buckling factor row"):
        parse_buckling_factors(bad)


# ---------------------------------------------------------------------------
# ccx-gated benchmarks (trap #16)

needs_ccx = pytest.mark.skipif(find_ccx() is None, reason="CalculiX not available")


@needs_ccx
def test_euler_column_fixed_free_exact():
    """Fixed-free slender column, unit end pressure: the first buckling
    factor is P_cr = pi^2 E I / (2L)^2 — C3D8R (reduced), probed +0.43%;
    THE trap regression: n_modes=6 so the degenerate first doublet cannot
    be silently skipped (n=2 returned ONLY the second doublet, exit 0)."""
    from meshforge.solve import solve_deck

    wall = _bar_wall()
    loads = pressure_to_nodal_forces(wall, "back", 1.0)  # uniform compression
    deck = write_deck(
        wall, _buckle_spec(loads, formulation="reduced"),
        "outputs/solve/v32_buckle",
    )
    result = solve_deck(deck, "outputs/solve/v32_buckle_out")
    facs = result.buckling_factors
    assert facs is not None and facs.shape == (6,)
    assert abs(facs[0] / PCR - 1.0) < 0.01           # probe +0.43%
    assert facs[0] == pytest.approx(facs[1])         # exact doublet
    assert abs(facs[2] / (9 * PCR) - 1.0) < 0.02     # mode 3 = 9*P_cr, -0.8%
    assert "buckling    :" in result.to_text()


@needs_ccx
def test_preloaded_frequency_shift():
    """Cantilever first bending mode under axial preload: f_P/f_0 within
    2% of sqrt(1 - P/P_cr) at P/P_cr in {+0.2, +0.4, -0.2} (probe <=1.2%;
    the sqrt relation is exact for pinned-pinned, first-order here).
    f_0 itself within 2% of the analytic cantilever frequency."""
    from meshforge.solve import solve_deck

    rho = 1e-3
    wall = _bar_wall()

    def modes(p):
        preloaded = p != 0.0
        spec = DeckSpec(
            material=Material.elastic(E, 0.0, density=rho),
            step=Step(kind="frequency", n_modes=2, preloaded=preloaded),
            boundary_conditions=(BoundaryCondition("front", 1, 3),),
            loads=pressure_to_nodal_forces(wall, "back", p) if preloaded else (),
            formulation="reduced",
            target="ccx",
        )
        tag = f"v32_pre_{int(round(p / PCR * 100)):+04d}"
        deck = write_deck(wall, spec, f"outputs/solve/{tag}")
        return solve_deck(deck, f"outputs/solve/{tag}_out").eigenfrequencies

    f0 = modes(0.0)
    f_analytic = (1.875104**2 / (2 * math.pi)) * math.sqrt(
        E * I_SEC / (rho * 1.0 * L**4)
    )
    assert abs(f0[0] / f_analytic - 1.0) < 0.02  # probe -1.6% at 4x60
    for frac in (0.2, 0.4, -0.2):
        fp = modes(frac * PCR)
        ratio = fp[0] / f0[0]
        assert abs(ratio / math.sqrt(1.0 - frac) - 1.0) < 0.02


@needs_ccx
def test_session_buckle_e2e():
    """analysis='buckle' through the session: axial end pressure on the
    bar; factors parsed + journaled. Default formulation is FULL C3D8 —
    +12% locking bias at this mesh (probed), so the band is honest 15%;
    the tight truth is the direct C3D8R test above."""
    from meshforge.pipeline import MeshingSession

    session = MeshingSession()
    session.volmesh = _bar_wall(n_cross=3, n_layers=40)
    result = session.compute_solve(
        analysis="buckle", E=E, nu=0.0, n_modes=6,
        fix="front", pressure="back=1.0",
    )
    facs = result.summary["buckling_factors"]
    assert facs is not None and len(facs) == 6
    assert abs(facs[0] / PCR - 1.0) < 0.15
    assert facs[0] == pytest.approx(facs[1], rel=1e-6)
    assert session.journal[-1]["params"]["analysis"] == "buckle"


@needs_ccx
def test_cli_deck_buckle_solve_e2e(tmp_path, capsys):
    """CLI: deck --analysis buckle on the tube under external pressure,
    then solve — factors parsed and printed (plumbing; no closed-form
    truth for tube wall buckling here)."""
    from meshforge.cli import main
    from meshforge.synth import generate

    surface, _ = generate("straight_tube")
    src = tmp_path / "tube.vtp"
    surface.save(src)
    rc = main([
        "deck", str(src), "--thickness", "1", "--n-theta", "16",
        "--n-axial", "11", "--n-layers", "2",
        "--material", "elastic", "--E", "1000", "--nu", "0.3",
        "--analysis", "buckle", "--n-modes", "4",
        "--formulation", "reduced",
        "--fix", "inlet,1,3", "--fix", "outlet,1,3",
        "--pressure", "outer,1.0",
        "--target", "ccx", "--outputs", str(tmp_path / "outputs"),
    ])
    assert rc == 0
    deck_path = tmp_path / "outputs" / "tube_ccx.inp"
    assert deck_path.is_file()
    rc = main(["solve", str(deck_path), "--outputs", str(tmp_path / "outputs")])
    out = capsys.readouterr().out
    assert rc == 0
    assert "buckling    :" in out

"""V24 FEA depth: *FREQUENCY modal + steady *HEAT TRANSFER on CalculiX.

Ungated: deck-card exactness, loud validation, .dat eigenvalue parser on
the captured probe block. ccx-gated (skip cleanly, like test_solve.py):
the exact-rod modal benchmark, both radial-conduction truths, session
and CLI e2e."""
from __future__ import annotations

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
from digitwin.solve.dat import parse_eigenfrequencies
from digitwin.solve.runner import find_ccx

# ---------------------------------------------------------------------------
# meshes: slender blocks bar (modal) and swept tube wall (heat)


def _bar_wall(n_cross: int = 3, n_layers: int = 40, length: float = 20.0):
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


def _freq_spec(n_modes: int = 6) -> DeckSpec:
    return DeckSpec(
        material=Material.elastic(1000.0, 0.0, density=1e-3),
        step=Step(kind="frequency", n_modes=n_modes),
        boundary_conditions=(
            BoundaryCondition("NALL", 1, 2),
            BoundaryCondition("front", 3),
            BoundaryCondition("back", 3),
        ),
        target="ccx",
    )


def _heat_spec(bcs, loads=()) -> DeckSpec:
    return DeckSpec(
        material=Material.thermal(2.0),
        step=Step(kind="heat"),
        boundary_conditions=bcs,
        loads=loads,
        target="ccx",
    )


# ---------------------------------------------------------------------------
# deck cards (ungated)


def test_frequency_deck_cards(tmp_path):
    wall = _bar_wall(n_cross=2, n_layers=4)
    deck = write_deck(wall, _freq_spec(), tmp_path / "freq")
    text = deck.read_text()
    assert "*FREQUENCY\n6\n" in text
    assert f"*NSET, NSET=NALL, GENERATE\n1, {wall.grid.n_points}, 1\n" in text
    assert "*DENSITY\n0.001" in text
    assert "NALL, 1, 2" in text
    assert "*STATIC" not in text


def test_heat_deck_cards(tmp_path):
    wall = _tube_wall(n_layers=2)
    spec = _heat_spec(
        bcs=(
            BoundaryCondition("inner", 11, 11, 100.0),
            BoundaryCondition("outer", 11, None, 20.0),
        ),
        loads=(Load.surface_flux("inlet", 5.0),),
    )
    deck = write_deck(wall, spec, tmp_path / "heat")
    text = deck.read_text()
    assert "*HEAT TRANSFER, STEADY STATE" in text
    assert "*CONDUCTIVITY\n2" in text
    assert "INNER, 11, 11, 100" in text
    assert "OUTER, 11, 11, 20" in text
    assert "*DFLUX\nINLET_SURF, S, 5" in text
    # untouched output defaults swapped to temperatures
    assert "*NODE FILE\nNT" in text
    assert "*EL FILE" not in text
    assert "NSET=NALL" not in text  # frequency-only card


def test_static_decks_unchanged_by_v24(tmp_path):
    """The V24 additions must not perturb existing static decks."""
    wall = _tube_wall(n_layers=2)
    spec = DeckSpec(
        material=Material.elastic(1000.0, 0.3),
        step=Step(),
        boundary_conditions=(BoundaryCondition("inlet", 1, 3),),
        loads=(Load.pressure("inner", 1.0),),
        target="ccx",
    )
    text = write_deck(wall, spec, tmp_path / "static").read_text()
    assert "*STATIC" in text
    assert "NALL" not in text and "*DFLUX" not in text and "*FREQUENCY" not in text


# ---------------------------------------------------------------------------
# validation is loud (ungated)


def test_step_and_material_validation():
    with pytest.raises(ValueError, match="static|frequency|heat"):
        Step(kind="modal")
    with pytest.raises(ValueError, match="n_modes"):
        Step(kind="frequency", n_modes=0)
    with pytest.raises(ValueError, match="nlgeom"):
        Step(kind="heat", nlgeom=True)
    with pytest.raises(ValueError, match="mechanical output"):
        Step(kind="heat", node_file=("U", "NT"))
    with pytest.raises(ValueError, match="conductivity"):
        Material.thermal(0.0)
    with pytest.raises(ValueError, match="temperature is the single DOF 11"):
        BoundaryCondition("inner", 11, 3)
    with pytest.raises(ValueError, match="1..3"):
        BoundaryCondition("inner", 7)


def test_spec_cross_validation():
    freq_no_density = DeckSpec(
        material=Material.elastic(1000.0, 0.0),
        step=Step(kind="frequency"),
        target="ccx",
    )
    with pytest.raises(ValueError, match="density"):
        freq_no_density.validate()
    with pytest.raises(ValueError, match="preloaded"):
        DeckSpec(
            material=Material.elastic(1000.0, 0.0, density=1e-3),
            step=Step(kind="frequency"),
            loads=(Load.pressure("inner", 1.0),),
            target="ccx",
        ).validate()
    with pytest.raises(ValueError, match="Material.thermal"):
        DeckSpec(
            material=Material.elastic(1000.0, 0.3),
            step=Step(kind="heat"),
            target="ccx",
        ).validate()
    with pytest.raises(ValueError, match="mechanical BC"):
        DeckSpec(
            material=Material.thermal(2.0),
            step=Step(kind="heat"),
            boundary_conditions=(BoundaryCondition("inlet", 1, 3),),
            target="ccx",
        ).validate()
    with pytest.raises(ValueError, match="belong to heat steps"):
        DeckSpec(
            material=Material.elastic(1000.0, 0.3),
            step=Step(),
            boundary_conditions=(BoundaryCondition("inner", 11, 11, 5.0),),
            target="ccx",
        ).validate()
    with pytest.raises(ValueError, match="SOL 101"):
        DeckSpec(
            material=Material.elastic(1000.0, 0.0, density=1e-3),
            step=Step(kind="frequency"),
            target="nastran",
        ).validate()
    with pytest.raises(ValueError, match="static step needs a mechanical"):
        DeckSpec(
            material=Material.thermal(2.0), step=Step(), target="ccx"
        ).validate()


def test_session_solve_knob_validation():
    from digitwin.pipeline import MeshingSession

    session = MeshingSession()
    with pytest.raises(ValueError, match="analysis must be"):
        session.compute_solve(analysis="modal")
    with pytest.raises(ValueError, match="only to session-built"):
        session.compute_solve(deck="x.inp", analysis="frequency")
    session.volmesh = _bar_wall(n_cross=2, n_layers=2)
    with pytest.raises(ValueError, match="conductivity"):
        session.compute_solve(analysis="heat", temp="inner=100")
    with pytest.raises(ValueError, match="analysis='heat'"):
        session.compute_solve(analysis="static", temp="inner=100")


# ---------------------------------------------------------------------------
# .dat eigenvalue parser on the captured ccx block (ungated)

_PROBE_DAT = """
                        S T E P       1


     E I G E N V A L U E   O U T P U T

 MODE NO    EIGENVALUE                       FREQUENCY
                                     REAL PART            IMAGINARY PART
                           (RAD/TIME)      (CYCLES/TIME     (RAD/TIME)

      1   0.2494069E+05   0.1579262E+03   0.2513474E+02   0.0000000E+00
      2   0.6766898E+05   0.2601326E+03   0.4140139E+02   0.0000000E+00

     P A R T I C I P A T I O N   F A C T O R S

MODE NO.   X-COMPONENT     Y-COMPONENT     Z-COMPONENT

      1   0.0000000E+00   0.0000000E+00  -0.5855402E+00
"""


def test_parse_eigenfrequencies_probe_block():
    freqs = parse_eigenfrequencies(_PROBE_DAT)
    assert np.allclose(freqs, [25.13474, 41.40139])


def test_parse_eigenfrequencies_absent_and_malformed():
    assert parse_eigenfrequencies("no block here").size == 0
    bad = _PROBE_DAT.replace("      2   0.6766898E+05   0.2601326E+03   0.4140139E+02   0.0000000E+00",
                             "      2   0.6766898E+05")
    with pytest.raises(ValueError, match="malformed eigenvalue row"):
        parse_eigenfrequencies(bad)


# ---------------------------------------------------------------------------
# ccx-gated benchmarks (trap #16)

needs_ccx = pytest.mark.skipif(find_ccx() is None, reason="CalculiX not available")


@needs_ccx
def test_modal_rod_spectrum_exact():
    """Fixed-fixed slender bar, lateral DOFs clamped, nu=0: the spectrum
    is EXACTLY the 1-D rod's f_n = (n/2L)*sqrt(E/rho) — no plate/beam
    theory approximation (V24_SPEC: solid bar chosen over the tube, whose
    azimuthal shear modes interleave the rod modes)."""
    from digitwin.pipeline import MeshingSession

    session = MeshingSession()
    session.volmesh = _bar_wall()  # 1x1x20, 40 axial layers
    result = session.compute_solve(
        analysis="frequency",
        E=1000.0,
        nu=0.0,
        density=1e-3,
        n_modes=4,
        fix="NALL:1-2,front:3,back:3",
    )
    freqs = np.asarray(result.summary["eigenfrequencies"], dtype=float)
    assert freqs.shape == (4,)
    c = np.sqrt(1000.0 / 1e-3)
    analytic = np.arange(1, 5) * c / (2 * 20.0)  # 25, 50, 75, 100
    assert np.abs(freqs / analytic - 1.0).max() < 0.01


@needs_ccx
def test_heat_log_profile_exact():
    """Fixed T_a/T_b on the tube wall: T(r) = T_a + ΔT ln(r/a)/ln(b/a)."""
    from digitwin.solve import solve_deck

    wall = _tube_wall()
    spec = _heat_spec(
        bcs=(
            BoundaryCondition("inner", 11, 11, 100.0),
            BoundaryCondition("outer", 11, 11, 20.0),
        )
    )
    deck = write_deck(wall, spec, "outputs/solve/v24_heat")
    result = solve_deck(deck, "outputs/solve/v24_heat_out")
    assert result.temp_range == pytest.approx((20.0, 100.0), abs=1e-6)
    nt = result.frd.fields["NDTEMP"]
    r = np.linalg.norm(result.frd.points[:, :2], axis=1)
    analytic = 100.0 + (20.0 - 100.0) * np.log(r / 3.0) / np.log(4.0 / 3.0)
    err = np.abs(nt.values[:, 0] - analytic).max()
    assert err < 0.01 * 80.0  # <1% of ΔT (probe: 0.27% at 3 layers)


@needs_ccx
def test_heat_flux_profile_exact():
    """Flux q on the inner face, fixed T_b outside:
    T(r) = T_b + (q a / k) ln(b/r)."""
    from digitwin.solve import solve_deck

    wall = _tube_wall()
    spec = _heat_spec(
        bcs=(BoundaryCondition("outer", 11, 11, 20.0),),
        loads=(Load.surface_flux("inner", 5.0),),
    )
    deck = write_deck(wall, spec, "outputs/solve/v24_flux")
    result = solve_deck(deck, "outputs/solve/v24_flux_out")
    nt = result.frd.fields["NDTEMP"]
    r = np.linalg.norm(result.frd.points[:, :2], axis=1)
    analytic = 20.0 + (5.0 * 3.0 / 2.0) * np.log(4.0 / r)
    span = float(analytic.max() - analytic.min())  # ~2.16
    err = np.abs(nt.values[:, 0] - analytic).max()
    assert err < 0.03 * span or err < 0.05  # probe: 2.3% at 3 layers


@needs_ccx
def test_session_heat_solve_e2e():
    """analysis='heat' through the session: temp knobs -> NDTEMP attached."""
    from digitwin.pipeline import MeshingSession

    session = MeshingSession()
    session.volmesh = _tube_wall(n_layers=3)
    result = session.compute_solve(
        analysis="heat", conductivity=2.0, temp="inner=100,outer=20"
    )
    assert result.summary["temp_range"] == pytest.approx([20.0, 100.0])
    assert session.journal[-1]["step"] == "solve"
    assert session.journal[-1]["params"]["analysis"] == "heat"


@needs_ccx
def test_cli_deck_frequency_solve_e2e(tmp_path, capsys):
    """CLI: deck --analysis frequency on the tube, then solve it. The tube
    spectrum is NOT the pure rod's (shear modes interleave — V24_SPEC), so
    this asserts plumbing: 6 eigenfrequencies parsed and printed."""
    from digitwin.cli import main
    from digitwin.synth import generate

    surface, _ = generate("straight_tube")
    src = tmp_path / "tube.vtp"
    surface.save(src)
    rc = main([
        "deck", str(src), "--thickness", "1", "--n-theta", "16",
        "--n-axial", "11", "--n-layers", "2",
        "--material", "elastic", "--E", "1000", "--nu", "0",
        "--density", "0.001", "--analysis", "frequency", "--n-modes", "6",
        "--fix", "NALL,1,2", "--fix", "inlet,3", "--fix", "outlet,3",
        "--target", "ccx", "--outputs", str(tmp_path / "outputs"),
    ])
    assert rc == 0
    deck_path = tmp_path / "outputs" / "tube_ccx.inp"
    assert deck_path.is_file()
    rc = main(["solve", str(deck_path), "--outputs", str(tmp_path / "outputs")])
    out = capsys.readouterr().out
    assert rc == 0
    assert "frequencies :" in out

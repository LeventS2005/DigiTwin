"""V45 thermal stress: sequential thermo-mechanical coupling on CalculiX.

Ungated: card exactness (*EXPANSION on the elastic material; *TEMPERATURE
uniform/per-node; the *INITIAL CONDITIONS stress-free reference), loud
validation of every probe-found trap, the frd temperature helper, session/CLI
knob refusals. ccx-gated (skip cleanly): the free bar (eps = alpha*dT exact),
the constrained bar (sigma = -E*alpha*dT exact), the flagship tube (V24
log-T(r) heat solve chained into plane-strain thermal stress vs the
Timoshenko closed form, with refinement convergence), session + CLI e2e.

THE TRAPS (probe, session 30, ccx 2.23): *TEMPERATURE without *EXPANSION is
silently ZERO stress; the expansion reference is *INITIAL CONDITIONS only
(an *EXPANSION ZERO= parameter is silently ignored) so reference_temperature
is REQUIRED, never defaulted; alpha(T) tables are secant-anchored at numeric
temperature zero (scale-dependent, silently near-double under a Kelvin
shift) so only constant alpha is accepted.
"""
from __future__ import annotations

import numpy as np
import pytest

from digitwin.export.deck import (
    BoundaryCondition,
    DeckSpec,
    Material,
    Step,
    lateral_pins,
    write_deck,
)
from digitwin.solve.frd import FrdField, FrdModel, nodal_temperatures
from digitwin.solve.runner import find_ccx

# ---------------------------------------------------------------------------
# fixtures (the V24/V31/V41 bar + tube)


def _bar_wall(n_cross: int = 2, n_layers: int = 20, length: float = 20.0):
    """1 x 1 x length rectilinear hex bar via the V5 blocks path."""
    from digitwin.meshing import build_block_mesh, extrude_blocks

    rect = np.array([[0, 0], [1, 0], [1, 1], [0, 1]], dtype=float)
    bm = build_block_mesh([rect], target_size=1.0 / n_cross)
    return extrude_blocks(bm, thickness=length, n_layers=n_layers)


def _tube_wall(n_layers: int = 3):
    """Annulus a=3, b=4, L=20 (the V13/V24 fixture geometry)."""
    from digitwin.geometry import compute_centerline
    from digitwin.meshing import get_backend
    from digitwin.synth import generate

    surface, _ = generate("straight_tube")
    cl = compute_centerline(surface)
    return get_backend("swept").mesh_wall(
        surface, 1.0, centerline=cl, n_theta=16, n_axial=21, n_layers=n_layers
    )


def _corner_node(wall, x: float, y: float, z: float = 0.0) -> int:
    pts = np.asarray(wall.grid.points)
    d = np.linalg.norm(pts - np.array([x, y, z]), axis=1)
    idx = int(np.argmin(d))
    assert d[idx] < 1e-9
    return idx


def _static_spec(wall_or_bcs, **kw) -> DeckSpec:
    return DeckSpec(
        material=kw.pop(
            "material", Material.elastic(1000.0, 0.3, expansion=1e-5)
        ),
        step=kw.pop("step", Step()),
        boundary_conditions=tuple(wall_or_bcs),
        target="ccx",
        **kw,
    )


# ---------------------------------------------------------------------------
# closed forms


ALPHA, E_MOD, NU = 1e-5, 1000.0, 0.3


def _sigma_theta_plane_strain(r, *, a, b, dT):
    """Timoshenko thermal hoop stress, long tube, theta(rho)=dT ln(b/rho)/ln(b/a).

    I(r) = int_a^r theta(rho) rho drho has the analytic antiderivative
    ln(b/rho) rho^2/2 + rho^2/4 (probe: matches quadrature to 6 digits and
    the FEA to discretization error).
    """
    r = np.asarray(r, dtype=float)
    c = dT / np.log(b / a)

    def prim(rr):
        return np.log(b / rr) * rr**2 / 2.0 + rr**2 / 4.0

    integral = c * (prim(r) - prim(a))
    i_ab = c * (prim(b) - prim(a))
    theta = c * np.log(b / r)
    pref = ALPHA * E_MOD / (1.0 - NU)
    return pref * (
        integral / r**2
        - theta
        + (r**2 + a**2) / (r**2 * (b**2 - a**2)) * i_ab
    )


# ---------------------------------------------------------------------------
# deck cards (ungated)


def test_expansion_card(tmp_path):
    wall = _bar_wall(n_layers=4)
    spec = _static_spec(
        (BoundaryCondition("front", 3),),
        uniform_temperature=120.0,
        reference_temperature=20.0,
    )
    text = write_deck(wall, spec, tmp_path / "exp").read_text()
    assert "*ELASTIC\n1000, 0.3\n*EXPANSION\n1e-05" in text
    # the reference is model data (before *STEP); the load is step data
    assert "*INITIAL CONDITIONS, TYPE=TEMPERATURE\nNALL, 20" in text
    assert text.index("*INITIAL CONDITIONS") < text.index("*STEP")
    assert "*TEMPERATURE\nNALL, 120" in text
    assert text.index("*TEMPERATURE\nNALL") > text.index("*STEP")
    assert "*NSET, NSET=NALL, GENERATE" in text


def test_nodal_temperature_cards(tmp_path):
    wall = _bar_wall(n_layers=4)
    n = wall.grid.n_points
    pairs = tuple((i, 20.0 + i) for i in range(n))
    spec = _static_spec(
        (BoundaryCondition("front", 3),),
        nodal_temperatures=pairs,
        reference_temperature=20.0,
    )
    text = write_deck(wall, spec, tmp_path / "nodal").read_text()
    # 0-based ids -> 1-based solver ids, one line per node
    assert "*TEMPERATURE\n1, 20\n2, 21\n" in text
    assert f"{n}, {20.0 + n - 1:.10g}" in text


def test_expansion_table_refused():
    with pytest.raises(ValueError, match="secant anchored at numeric"):
        Material.elastic(1000.0, 0.3, expansion=[(1e-5, 20.0), (1.5e-5, 120.0)])


def test_expansion_invalid_values():
    for bad in (0.0, -1e-5, float("nan"), float("inf")):
        with pytest.raises(ValueError, match="positive finite"):
            Material.elastic(1000.0, 0.3, expansion=bad)


def test_temperature_requires_static(tmp_path):
    wall = _bar_wall(n_layers=4)
    for kind, mat in (
        ("heat", Material.thermal(2.0)),
        ("frequency", Material.elastic(1000.0, 0.3, density=1e-3)),
    ):
        spec = DeckSpec(
            material=mat,
            step=Step(kind=kind, n_modes=4) if kind == "frequency" else Step(kind=kind),
            boundary_conditions=(
                BoundaryCondition("front", 11, 11, 100.0)
                if kind == "heat"
                else BoundaryCondition("front", 3),
            ),
            uniform_temperature=120.0,
            reference_temperature=20.0,
            target="ccx",
        )
        with pytest.raises(ValueError, match="acts in a static step"):
            write_deck(wall, spec, tmp_path / "bad")


def test_temperature_requires_expansion(tmp_path):
    wall = _bar_wall(n_layers=4)
    # elastic without expansion AND a non-elastic material: both silent-zero traps
    for mat in (
        Material.elastic(1000.0, 0.3),
        Material.neo_hookean(100.0, 0.01),
    ):
        spec = DeckSpec(
            material=mat,
            step=Step(nlgeom=mat.is_hyperelastic),
            boundary_conditions=(BoundaryCondition("front", 3),),
            uniform_temperature=120.0,
            reference_temperature=20.0,
            target="ccx",
        )
        with pytest.raises(ValueError, match="silently ZERO"):
            write_deck(wall, spec, tmp_path / "bad")


def test_reference_temperature_required_and_dangling(tmp_path):
    wall = _bar_wall(n_layers=4)
    with pytest.raises(ValueError, match="never defaulted"):
        write_deck(
            wall,
            _static_spec(
                (BoundaryCondition("front", 3),), uniform_temperature=120.0
            ),
            tmp_path / "bad",
        )
    with pytest.raises(ValueError, match="inert without a"):
        write_deck(
            wall,
            _static_spec((BoundaryCondition("front", 3),)),
            tmp_path / "bad",
        )
    with pytest.raises(ValueError, match="only feeds a thermal load"):
        write_deck(
            wall,
            _static_spec(
                (BoundaryCondition("front", 3),),
                material=Material.elastic(1000.0, 0.3),
                reference_temperature=20.0,
            ),
            tmp_path / "bad",
        )


def test_both_sources_and_corrupt_values_refused(tmp_path):
    wall = _bar_wall(n_layers=4)
    bcs = (BoundaryCondition("front", 3),)
    with pytest.raises(ValueError, match="not both"):
        write_deck(
            wall,
            _static_spec(
                bcs,
                uniform_temperature=120.0,
                nodal_temperatures=((0, 120.0),),
                reference_temperature=20.0,
            ),
            tmp_path / "bad",
        )
    with pytest.raises(ValueError, match="is empty"):
        write_deck(
            wall,
            _static_spec(
                bcs, nodal_temperatures=(), reference_temperature=20.0
            ),
            tmp_path / "bad",
        )
    for kw in (
        {"uniform_temperature": float("nan")},
        {"nodal_temperatures": ((0, float("inf")),)},
    ):
        with pytest.raises(ValueError, match="finite|non-finite"):
            write_deck(
                wall,
                _static_spec(bcs, reference_temperature=20.0, **kw),
                tmp_path / "bad",
            )
    with pytest.raises(ValueError, match="finite"):
        write_deck(
            wall,
            _static_spec(
                bcs,
                uniform_temperature=120.0,
                reference_temperature=float("nan"),
            ),
            tmp_path / "bad",
        )


def test_nodal_id_range_and_format_targets(tmp_path):
    wall = _bar_wall(n_layers=4)
    spec = _static_spec(
        (BoundaryCondition("front", 3),),
        nodal_temperatures=((wall.grid.n_points, 120.0),),
        reference_temperature=20.0,
    )
    with pytest.raises(ValueError, match="out of range"):
        write_deck(wall, spec, tmp_path / "bad")
    for target in ("nastran", "lsdyna", "ansys"):
        spec = DeckSpec(
            material=Material.elastic(1000.0, 0.3, expansion=1e-5),
            step=Step(),
            boundary_conditions=(BoundaryCondition("front", 1, 3),),
            uniform_temperature=120.0,
            reference_temperature=20.0,
            target=target,
        )
        with pytest.raises(ValueError, match="abaqus/ccx decks only"):
            write_deck(wall, spec, tmp_path / "bad")


def test_frd_nodal_temperatures_helper():
    model = FrdModel(
        node_ids=np.array([1, 2, 3]),
        points=np.zeros((3, 3)),
        cell_types=None,
        cells=None,
        fields={
            "NDTEMP": FrdField("NDTEMP", ("T",), np.array([[20.0], [30.0], [40.0]]))
        },
    )
    assert nodal_temperatures(model) == ((0, 20.0), (1, 30.0), (2, 40.0))
    model.fields["NDTEMP"].values[1, 0] = float("nan")
    with pytest.raises(ValueError, match="non-finite temperature"):
        nodal_temperatures(model)
    with pytest.raises(ValueError, match="no temperature field"):
        nodal_temperatures(
            FrdModel(
                node_ids=np.array([1]),
                points=np.zeros((1, 3)),
                cell_types=None,
                cells=None,
            )
        )


# ---------------------------------------------------------------------------
# session/CLI knob refusals (ungated — they fire before any solve)


def test_session_thermal_knob_refusals():
    from digitwin.pipeline import MeshingSession

    session = MeshingSession()
    session.volmesh = _bar_wall(n_layers=4)
    with pytest.raises(ValueError, match="set analysis='static'"):
        session.compute_solve(
            analysis="heat", conductivity=2.0, temp="front=100",
            expansion=1e-5,
        )
    with pytest.raises(ValueError, match="session-built decks"):
        session.compute_solve(deck="x.inp", temperature=120.0)
    with pytest.raises(ValueError, match="inert without a temperature"):
        session.compute_solve(analysis="static", expansion=1e-5, fix="front:3")
    with pytest.raises(ValueError, match="only feeds a thermal load"):
        session.compute_solve(analysis="static", t_ref=20.0, fix="front:3")
    with pytest.raises(ValueError, match="needs the expansion knob"):
        session.compute_solve(
            analysis="static", temperature=120.0, t_ref=20.0, fix="front:3"
        )
    with pytest.raises(ValueError, match="needs t_ref"):
        session.compute_solve(
            analysis="static", temperature=120.0, expansion=1e-5, fix="front:3"
        )
    with pytest.raises(ValueError, match="not both"):
        session.compute_solve(
            analysis="static", temperature=120.0, temperature_from="x.frd",
            expansion=1e-5, t_ref=20.0, fix="front:3",
        )
    with pytest.raises(ValueError, match="frd not found"):
        session.compute_solve(
            analysis="static", temperature_from="nope.frd",
            expansion=1e-5, t_ref=20.0, fix="front:3",
        )


def test_cli_thermal_refusals(tmp_path):
    from digitwin.cli import main
    from digitwin.synth import generate

    surface, _ = generate("straight_tube")
    src = tmp_path / "tube.vtp"
    surface.save(src)
    base = [
        "deck", str(src), "--thickness", "1", "--n-theta", "8",
        "--n-axial", "6", "--n-layers", "2",
        "--outputs", str(tmp_path / "outputs"), "--target", "ccx",
    ]
    rc = main(base + [
        "--material", "neo_hooke", "--c10", "100", "--d1", "0.01",
        "--nlgeom", "--expansion", "1e-5",
    ])
    assert rc == 1  # --expansion belongs to --material elastic
    rc = main(base + [
        "--material", "elastic", "--E", "1000", "--nu", "0.3",
        "--expansion", "1e-5", "--t-ref", "20",
        "--temperature", "120", "--temperature-from", "x.frd",
    ])
    assert rc == 1  # both sources


# ---------------------------------------------------------------------------
# ccx-gated closed-form truths

needs_ccx = pytest.mark.skipif(
    find_ccx() is None, reason="CalculiX (ccx) not installed"
)


@needs_ccx
def test_free_bar_expansion_exact():
    """Free bar, uniform dT: eps = alpha*dT exactly, stress ~ 0."""
    from digitwin.solve import solve_deck

    wall = _bar_wall()
    a = _corner_node(wall, 0, 0, 0)
    b = _corner_node(wall, 1, 0, 0)
    c = _corner_node(wall, 0, 1, 0)
    spec = _static_spec(
        (
            BoundaryCondition(a, 1, 3),
            BoundaryCondition(b, 2, 3),
            BoundaryCondition(c, 3, 3),
        ),
        uniform_temperature=120.0,
        reference_temperature=20.0,
    )
    deck = write_deck(wall, spec, "outputs/solve/v45_free_bar")
    result = solve_deck(deck, "outputs/solve/v45_free_bar_out")
    pts = np.asarray(wall.grid.points)
    tip = pts[:, 2] > 20.0 - 1e-9
    uz = result.frd.fields["DISP"].values[tip, 2]
    eps = float(np.mean(uz)) / 20.0
    assert eps == pytest.approx(1e-5 * 100.0, rel=1e-6)
    assert result.mises_max == pytest.approx(0.0, abs=1e-8)


@needs_ccx
def test_constrained_bar_stress_exact():
    """Both ends axially fixed, uniform dT: sigma_z = -E*alpha*dT exactly."""
    from digitwin.solve import solve_deck

    wall = _bar_wall()
    a = _corner_node(wall, 0, 0, 0)
    b = _corner_node(wall, 1, 0, 0)
    spec = _static_spec(
        (
            BoundaryCondition("front", 3),
            BoundaryCondition("back", 3),
            BoundaryCondition(a, 1, 2),
            BoundaryCondition(b, 2, 2),
        ),
        uniform_temperature=120.0,
        reference_temperature=20.0,
    )
    deck = write_deck(wall, spec, "outputs/solve/v45_constrained_bar")
    result = solve_deck(deck, "outputs/solve/v45_constrained_bar_out")
    # the .dat integration-point stress is EXACTLY -1.0 (probe-verified on
    # this deck); the frd NODAL value carries a ~4e-5 ccx extrapolation/print
    # artifact — the honest frd-level tolerance.
    szz = result.frd.fields["STRESS"].values[:, 2]
    assert float(szz.min()) == pytest.approx(-1.0, abs=2e-4)
    assert float(szz.max()) == pytest.approx(-1.0, abs=2e-4)


def _tube_hoop_error(n_layers: int):
    """Heat solve -> chained static (plane strain) -> wall hoop-stress error
    vs the Timoshenko closed form, as a fraction of the closed-form value."""
    from digitwin.solve import solve_deck

    wall = _tube_wall(n_layers)
    heat_spec = DeckSpec(
        material=Material.thermal(2.0),
        step=Step(kind="heat"),
        boundary_conditions=(
            BoundaryCondition("inner", 11, 11, 100.0),
            BoundaryCondition("outer", 11, 11, 20.0),
        ),
        target="ccx",
    )
    heat_deck = write_deck(
        wall, heat_spec, f"outputs/solve/v45_tube_heat_{n_layers}"
    )
    heat = solve_deck(heat_deck, f"outputs/solve/v45_tube_heat_{n_layers}_out")
    pairs = nodal_temperatures(heat.frd)

    spec = _static_spec(
        (
            BoundaryCondition("inlet", 3),
            BoundaryCondition("outlet", 3),
            *lateral_pins(wall),
        ),
        nodal_temperatures=pairs,
        reference_temperature=20.0,
    )
    deck = write_deck(wall, spec, f"outputs/solve/v45_tube_static_{n_layers}")
    result = solve_deck(
        deck, f"outputs/solve/v45_tube_static_{n_layers}_out"
    )

    pts = result.frd.points
    r = np.linalg.norm(pts[:, :2], axis=1)
    s = result.frd.fields["STRESS"].values
    cos = pts[:, 0] / r
    sin = pts[:, 1] / r
    hoop = s[:, 0] * sin**2 + s[:, 1] * cos**2 - 2.0 * s[:, 3] * sin * cos

    # interior axial band (away from the pinned end faces)
    mid = (pts[:, 2] > 5.0) & (pts[:, 2] < 15.0)
    a_eff, b_eff = float(r.min()), float(r.max())
    cf = _sigma_theta_plane_strain(r, a=a_eff, b=b_eff, dT=80.0)
    inner = mid & (r < a_eff + 0.05)
    outer = mid & (r > b_eff - 0.05)
    err_in = abs(float(np.mean(hoop[inner])) / float(np.mean(cf[inner])) - 1.0)
    err_out = abs(float(np.mean(hoop[outer])) / float(np.mean(cf[outer])) - 1.0)
    return err_in, err_out, hoop, cf, mid


@needs_ccx
def test_tube_thermal_stress_flagship():
    """The V24 log-T(r) heat truth chained into plane-strain thermal stress:
    wall hoop stress converges onto Timoshenko under radial refinement.

    Honest tolerances, measured at build time (V30 precedent): the steep
    near-wall sigma_theta gradient makes the frd NODAL extrapolation read low
    at the walls on linear hexes — 32/34 % at 3 radial layers, 11/13 % at 8,
    7.4/8.2 % at 12 (the probe's ideal quarter-ring mesh reads the same 7-9 %
    at 12 radial elements; its .dat centroid values sit within ~2 %). The
    convergence ratio, not a tight wall number, is the guarded truth here.
    """
    err_in3, err_out3, _hoop3, _cf3, _mid3 = _tube_hoop_error(3)
    assert err_in3 < 0.40
    assert err_out3 < 0.40
    err_in8, err_out8, hoop, cf, mid = _tube_hoop_error(8)
    assert err_in8 < 0.15
    assert err_out8 < 0.15
    # refinement must move BOTH walls decisively toward the closed form
    # (measured ratio 0.36-0.37; 0.55 leaves honest margin)
    assert err_in8 < 0.55 * err_in3
    assert err_out8 < 0.55 * err_out3
    # the interior profile tracks the closed form (fraction of its range;
    # measured 0.064 at 8 layers)
    rng = float(cf.max() - cf.min())
    assert float(np.abs(hoop[mid] - cf[mid]).max()) < 0.10 * rng


@needs_ccx
def test_session_thermal_chain_e2e():
    """Heat solve -> static solve chained via temperature_from, all in one
    session; journal carries both entries; a foreign frd is refused."""
    from digitwin.pipeline import MeshingSession

    session = MeshingSession()
    session.volmesh = _tube_wall(3)
    session.compute_solve(
        analysis="heat", conductivity=2.0, temp="inner=100,outer=20"
    )
    frd_path = str(session.solve.frd_path)
    result = session.compute_solve(
        analysis="static", expansion=1e-5, t_ref=20.0,
        temperature_from=frd_path,
        fix="inlet:3,outlet:3", pin_lateral=1,
    )
    assert result.summary["mises_max"] > 0.1
    solves = [e for e in session.journal if e["step"] == "solve"]
    assert len(solves) == 2
    assert solves[0]["params"]["analysis"] == "heat"
    assert solves[1]["params"]["temperature_from"] == frd_path
    assert solves[1]["params"]["expansion"] == 1e-5
    assert solves[1]["params"]["t_ref"] == 20.0

    # the SAME frd against a different mesh must refuse (fail loud, rule 6)
    session2 = MeshingSession()
    session2.volmesh = _bar_wall(n_layers=4)
    with pytest.raises(ValueError, match="different mesh"):
        session2.compute_solve(
            analysis="static", expansion=1e-5, t_ref=20.0,
            temperature_from=frd_path, fix="front:3",
        )


@needs_ccx
def test_cli_thermal_stress_e2e(tmp_path, capsys):
    """CLI two-invocation chain: deck heat -> solve -> deck static
    --temperature-from -> solve."""
    from digitwin.cli import main
    from digitwin.synth import generate

    surface, _ = generate("straight_tube")
    src = tmp_path / "tube.vtp"
    surface.save(src)
    outputs = str(tmp_path / "outputs")
    mesh_args = [
        "--thickness", "1", "--n-theta", "16", "--n-axial", "11",
        "--n-layers", "2", "--outputs", outputs,
    ]
    rc = main([
        "deck", str(src), *mesh_args,
        "--material", "thermal", "--conductivity", "2.0",
        "--analysis", "heat", "--temp", "inner,100", "--temp", "outer,20",
        "--target", "ccx",
    ])
    assert rc == 0
    heat_deck = tmp_path / "outputs" / "tube_ccx.inp"
    rc = main(["solve", str(heat_deck), "--outputs", outputs])
    assert rc == 0
    frd = next((tmp_path / "outputs").rglob("*.frd"))

    rc = main([
        "deck", str(src), *mesh_args,
        "--material", "elastic", "--E", "1000", "--nu", "0.3",
        "--expansion", "1e-5", "--t-ref", "20",
        "--temperature-from", str(frd),
        "--fix", "inlet,3", "--fix", "outlet,3", "--pin-lateral",
        "--target", "ccx", "--out", str(tmp_path / "outputs" / "tube_ts.inp"),
    ])
    assert rc == 0
    text = (tmp_path / "outputs" / "tube_ts.inp").read_text()
    assert "*EXPANSION\n1e-05" in text
    assert "*INITIAL CONDITIONS, TYPE=TEMPERATURE\nNALL, 20" in text
    assert "*TEMPERATURE\n1, " in text
    rc = main([
        "solve", str(tmp_path / "outputs" / "tube_ts.inp"),
        "--outputs", outputs,
    ])
    out = capsys.readouterr().out
    assert rc == 0
    assert "von Mises max" in out

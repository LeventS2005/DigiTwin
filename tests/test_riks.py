"""V42 Riks post-buckling: ``*STATIC, RIKS`` arc-length continuation on
CalculiX — trace the load–displacement path THROUGH the buckling limit.

Ungated: the ``*STATIC, RIKS`` card + data line, RF auto-requested,
loud validation (riks needs static+nlgeom; the arc-length knobs dangle
without riks; non-static session solves refuse them), and that plain
static/buckle/heat decks are untouched.

ccx-gated (skip cleanly): the imperfection-seeded fixed-free Euler column
(C3D8R). THE trap — the frd "time" is the arc-length/LPF parameter, NOT
the load, so the true axial load is read from the reaction force (RF)
summed over the clamped base. Validated against the exact clamped-free
elastica ``P/P_cr = (2K(k)/pi)^2``, ``delta = 2kL/K(k)``, ``k =
sin(theta_tip/2)``: (a) the lateral deflection takes off as the reaction
crosses P_cr (<=1%), (b) the deep post-buckling tail hugs the elastica
(<1.5% of L for P/P_cr>1.4, 0.0-0.6% at large delta), (c) a monotone
arc-length trace through the limit. The knee (1.0<P/P_cr<1.4) deviates
more — imperfect-column rounding, documented honestly (V30 precedent).
Imperfection seeding lives HERE (perturb the mesh nodes by the analytic
mode-1 shape) — Riks itself is imperfection-agnostic (rule 5).
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
from meshforge.solve.runner import find_ccx

E, L = 1000.0, 20.0
I_SEC = 1.0 / 12.0
PCR = math.pi**2 * E * I_SEC / (2 * L) ** 2  # fixed-free Euler, 0.51404
AMP = 0.01  # mode-1 imperfection amplitude (0.05% of L) — a test seed, not a knob


def _bar_wall(n_cross: int = 4, n_layers: int = 60):
    """1 x 1 x L rectilinear hex bar via the V5 blocks path (V32 fixture)."""
    from meshforge.meshing import build_block_mesh, extrude_blocks

    rect = np.array([[0, 0], [1, 0], [1, 1], [0, 1]], dtype=float)
    bm = build_block_mesh([rect], target_size=1.0 / n_cross)
    return extrude_blocks(bm, thickness=L, n_layers=n_layers)


def _seed_imperfection(wall, amp: float = AMP):
    """Perturb the mesh in +x by the clamped-free mode-1 shape
    x += amp*(1 - cos(pi z / 2L)) so Riks picks the intended branch."""
    pts = np.asarray(wall.grid.points)
    pts[:, 0] += amp * (1 - np.cos(math.pi * pts[:, 2] / (2 * L)))
    wall.grid.points = pts
    return wall


def _riks_spec(loads, **step_kw):
    step_kw.setdefault("riks_dl_init", 0.005)
    step_kw.setdefault("riks_max_lpf", 0.6)
    step_kw.setdefault("riks_dl_min", 1e-7)
    step_kw.setdefault("riks_dl_max", 0.01)
    return DeckSpec(
        material=Material.elastic(E, 0.0),
        step=Step(nlgeom=True, riks=True, **step_kw),
        boundary_conditions=(BoundaryCondition("front", 1, 3),),
        loads=loads,
        formulation="reduced",
        target="ccx",
    )


def _elastica_delta(p_over_pcr: float) -> float:
    """Exact clamped-free elastica tip horizontal deflection at a given load:
    invert P/P_cr = (2 K(k)/pi)^2 for k, return delta = 2 k L / K(k).
    scipy's ellipk takes the parameter m = k^2."""
    from scipy.optimize import brentq
    from scipy.special import ellipk

    if p_over_pcr <= 1.0:
        return 0.0
    k_target = (math.pi / 2) * math.sqrt(p_over_pcr)  # == ellipk(k^2)
    if k_target <= math.pi / 2:
        return 0.0
    m = brentq(lambda m: ellipk(m) - k_target, 0.0, 1 - 1e-12)
    return 2 * math.sqrt(m) * L / ellipk(m)


# ---------------------------------------------------------------------------
# deck cards (ungated)


def test_riks_deck_cards(tmp_path):
    wall = _bar_wall(n_cross=2, n_layers=4)
    deck = write_deck(
        wall, _riks_spec(pressure_to_nodal_forces(wall, "back", 1.0)),
        tmp_path / "r",
    )
    text = deck.read_text()
    assert "*STEP, NLGEOM\n" in text  # riks is geometrically nonlinear
    assert "*STATIC, RIKS\n0.005, 0.6, 1e-07, 0.01\n" in text
    # RF is auto-requested — the load half of the curve (trap #2)
    assert "*NODE FILE\nU, RF\n" in text
    assert "*BUCKLE" not in text and "PERTURBATION" not in text
    assert text.count("*STEP") == 1 and text.count("*END STEP") == 1


def test_riks_default_arc_length_knobs(tmp_path):
    """Unset arc-length knobs fill with generic defaults; RF is auto-added."""
    wall = _bar_wall(n_cross=2, n_layers=4)
    spec = DeckSpec(
        material=Material.elastic(E, 0.0),
        step=Step(nlgeom=True, riks=True),
        boundary_conditions=(BoundaryCondition("front", 1, 3),),
        loads=pressure_to_nodal_forces(wall, "back", 1.0),
        target="ccx",
    )
    text = write_deck(wall, spec, tmp_path / "d").read_text()
    assert "*STATIC, RIKS\n0.05, 1, 1e-05, 0.5\n" in text
    assert "*NODE FILE\nU, RF\n" in text


def test_riks_rf_appends_to_explicit_node_file():
    step = Step(nlgeom=True, riks=True, node_file=("U", "S"))
    assert step.node_file == ("U", "S", "RF")
    # already present -> not duplicated
    step2 = Step(nlgeom=True, riks=True, node_file=("RF", "U"))
    assert step2.node_file.count("RF") == 1


def test_step_riks_validation():
    with pytest.raises(ValueError, match="riks requires nlgeom"):
        Step(riks=True)
    with pytest.raises(ValueError, match="riks .*applies to static"):
        Step(kind="frequency", riks=True)
    with pytest.raises(ValueError, match="riks .*applies to static"):
        Step(kind="buckle", riks=True)
    # arc-length knobs dangle without riks
    with pytest.raises(ValueError, match="arc-length knobs that apply only"):
        Step(riks_max_lpf=0.6)
    with pytest.raises(ValueError, match="arc-length knobs that apply only"):
        Step(nlgeom=True, riks_dl_init=0.01)
    # bad numeric knobs
    with pytest.raises(ValueError, match="riks_dl_init must be positive"):
        Step(nlgeom=True, riks=True, riks_dl_init=-1.0)
    with pytest.raises(ValueError, match="riks_dl_min .* exceeds riks_dl_max"):
        Step(nlgeom=True, riks=True, riks_dl_min=0.5, riks_dl_max=0.01)


def test_plain_decks_unchanged_by_riks(tmp_path):
    """Static, buckle and heat decks must not grow a RIKS card or RF."""
    wall = _bar_wall(n_cross=2, n_layers=4)
    static = DeckSpec(
        material=Material.elastic(E, 0.3),
        step=Step(),
        boundary_conditions=(BoundaryCondition("front", 1, 3),),
        loads=(Load.concentrated(0, 3, -1.0),),
        target="ccx",
    )
    text = write_deck(wall, static, tmp_path / "s").read_text()
    assert "RIKS" not in text
    # "RF" also matches "..._SURF" — check the node-file request specifically
    assert "*NODE FILE\nU\n" in text and "U, RF" not in text
    buckle = DeckSpec(
        material=Material.elastic(E, 0.0),
        step=Step(kind="buckle", n_modes=4),
        boundary_conditions=(BoundaryCondition("front", 1, 3),),
        loads=(Load.concentrated(0, 3, -1.0),),
        target="ccx",
    )
    assert "RIKS" not in write_deck(wall, buckle, tmp_path / "b").read_text()


def test_session_riks_knob_validation():
    from meshforge.pipeline import MeshingSession

    session = MeshingSession()
    with pytest.raises(ValueError, match="static-analysis knob"):
        session.compute_solve(analysis="buckle", riks=1)
    with pytest.raises(ValueError, match="static-analysis knob"):
        session.compute_solve(analysis="heat", riks_max_lpf=0.6)
    with pytest.raises(ValueError, match="apply only to a riks solve"):
        session.compute_solve(riks_dl_init=0.01)
    with pytest.raises(ValueError, match="riks needs nlgeom=1"):
        session.compute_solve(riks=1)
    with pytest.raises(ValueError, match="only to session-built"):
        session.compute_solve(deck="x.inp", riks=1, nlgeom=1)
    # the V42 solve knobs (formulation + dead cload) refuse an external deck
    with pytest.raises(ValueError, match="only to session-built"):
        session.compute_solve(deck="x.inp", formulation="reduced")
    with pytest.raises(ValueError, match="only to session-built"):
        session.compute_solve(deck="x.inp", cload="back,3,-1")
    # nlgeom alone too (V43: it used to pass silently — an external deck
    # already fixes its own step nonlinearity)
    with pytest.raises(ValueError, match="only to session-built"):
        session.compute_solve(deck="x.inp", nlgeom=1)


def test_cload_spec_parser():
    from meshforge.pipeline import _parse_cload_spec

    ld = _parse_cload_spec("back,3,-0.04")
    assert ld.kind == "cload" and ld.dof == 3 and ld.magnitude == -0.04
    assert ld.target == "back"
    for bad in ("back,3", "back,3,x", ",3,-1"):
        with pytest.raises(ValueError, match="cload"):
            _parse_cload_spec(bad)


# ---------------------------------------------------------------------------
# ccx-gated benchmarks (trap #16)

needs_ccx = pytest.mark.skipif(find_ccx() is None, reason="CalculiX not available")


def _trace(result):
    """Build the (P_reaction, delta_tip) arrays from the frd history:
    P = |sum RF_z over the clamped base|, delta = mean U_x over the tip."""
    pts = result.frd.points
    z = pts[:, 2]
    tip = z > L - 1e-6
    base = z < 1e-6
    disp_hist = result.frd.history["DISP"]
    forc_hist = result.frd.history["FORC"]
    deltas = np.array([d[tip, 0].mean() for _, d in disp_hist])
    loads = np.array([abs(f[base, 2].sum()) for _, f in forc_hist])
    return loads, deltas


@needs_ccx
def test_riks_column_elastica():
    """Imperfection-seeded fixed-free column, *STATIC, RIKS: the traced
    (reaction, tip deflection) path recovers Euler P_cr at take-off and
    lies on the exact clamped-free elastica in deep post-buckling."""
    from meshforge.solve import solve_deck

    wall = _seed_imperfection(_bar_wall())
    deck = write_deck(
        wall, _riks_spec(pressure_to_nodal_forces(wall, "back", 1.0)),
        "outputs/solve/v42_riks",
    )
    result = solve_deck(deck, "outputs/solve/v42_riks_out")

    # the runner recognised the RIKS step
    assert result.riks_lpf is not None and result.riks_lpf.size > 20
    assert "riks incs" in result.to_text()
    loads, deltas = _trace(result)
    pr = loads / PCR

    # (c) monotone arc-length continuation, and the reaction rises through
    # the limit region (plain load-control Newton stalls at the knee)
    assert np.all(np.diff(result.riks_lpf) > 0)
    assert np.all(np.diff(loads) > -1e-3 * PCR)   # non-decreasing (± noise)
    assert pr.max() > 1.5 and (deltas / L).max() > 0.3   # deep post-buckling

    # (a) take-off: the reaction crosses P_cr exactly where the lateral
    # deflection takes off (knee band 0.05 < delta/L < 0.5)
    knee = (deltas / L > 0.05) & (deltas / L < 0.5)
    assert knee.any()
    assert np.min(np.abs(pr[knee] - 1.0)) < 0.01     # probe 0.3%

    # (b) deep post-buckling hugs the elastica; tighten as it converges
    deep = pr > 1.4
    resid_deep = np.array(
        [abs(deltas[j] - _elastica_delta(pr[j])) for j in np.where(deep)[0]]
    )
    assert deep.sum() >= 5
    assert resid_deep.max() < 0.015 * L              # probe 1.27% of L
    # the deepest increments (large delta) sit essentially on the curve
    assert resid_deep[-5:].max() < 0.01 * L          # probe 0.0-0.6% of L


@needs_ccx
def test_session_riks_e2e():
    """analysis='static' + riks=1 through the session on the seeded column:
    riks_lpf lands in the summary, the journal records the knobs, and the
    reaction still crosses P_cr at take-off. Uses a DEAD cload (not the
    follower ``pressure`` — a follower load destabilises the free tip) and
    reduced C3D8R (FULL C3D8 shear-locks and stalls at the knee)."""
    from meshforge.pipeline import MeshingSession

    session = MeshingSession()
    # a coarse mesh needs a larger seed so the sharp knee doesn't cut back
    session.volmesh = _seed_imperfection(_bar_wall(n_cross=3, n_layers=40), amp=0.05)
    result = session.compute_solve(
        analysis="static", E=E, nu=0.0, nlgeom=1, formulation="reduced", riks=1,
        riks_dl_init=0.005, riks_max_lpf=0.6, riks_dl_min=1e-7, riks_dl_max=0.01,
        fix="front", cload="back,3,-0.04",
    )
    assert result.summary["riks_lpf"] is not None
    assert len(result.summary["riks_lpf"]) > 10
    assert session.journal[-1]["params"]["riks"] == 1
    loads, deltas = _trace(session.solve)
    pr = loads / PCR
    assert pr.max() > 1.1  # traced past the buckling limit
    knee = (deltas / L > 0.05) & (deltas / L < 0.5)
    assert knee.any() and np.min(np.abs(pr[knee] - 1.0)) < 0.03  # coarser mesh


@needs_ccx
def test_cli_deck_riks_solve_e2e(tmp_path, capsys):
    """CLI: deck --riks --nlgeom writes a *STATIC, RIKS deck; solve traces
    it and prints the per-increment arc-length count (load from RF)."""
    from meshforge.cli import main

    # the seeded column, written straight to a runnable deck
    wall = _seed_imperfection(_bar_wall(n_cross=3, n_layers=40), amp=0.05)
    deck = write_deck(
        wall, _riks_spec(pressure_to_nodal_forces(wall, "back", 1.0)),
        tmp_path / "col",
    )
    rc = main(["solve", str(deck), "--outputs", str(tmp_path / "out")])
    out = capsys.readouterr().out
    assert rc == 0
    assert "riks incs" in out

    # and the deck CLI flag emits the RIKS card (fast, ungated path)
    from meshforge.synth import generate

    surface, _ = generate("straight_tube")
    src = tmp_path / "tube.vtp"
    surface.save(src)
    rc = main([
        "deck", str(src), "--thickness", "1", "--n-theta", "16",
        "--n-axial", "11", "--n-layers", "2",
        "--material", "elastic", "--E", "1000", "--nu", "0.3",
        "--nlgeom", "--riks", "--riks-max-lpf", "0.5",
        "--fix", "inlet,1,3", "--pressure", "outer,1.0",
        "--target", "ccx", "--outputs", str(tmp_path / "outputs"),
    ])
    assert rc == 0
    text = (tmp_path / "outputs" / "tube_ccx.inp").read_text()
    assert "*STATIC, RIKS\n" in text and "*STEP, NLGEOM" in text
    assert "*NODE FILE\nU, RF\n" in text


def test_cli_deck_riks_needs_nlgeom(tmp_path):
    """deck --riks without --nlgeom fails loud (rc 1)."""
    from meshforge.cli import main
    from meshforge.synth import generate

    surface, _ = generate("straight_tube")
    src = tmp_path / "tube.vtp"
    surface.save(src)
    rc = main([
        "deck", str(src), "--thickness", "1", "--n-theta", "12",
        "--n-axial", "9", "--n-layers", "2",
        "--material", "elastic", "--E", "1000", "--nu", "0.3",
        "--riks", "--fix", "inlet,1,3", "--pressure", "outer,1.0",
        "--target", "ccx", "--outputs", str(tmp_path / "outputs"),
    ])
    assert rc == 1

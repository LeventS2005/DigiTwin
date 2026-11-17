"""V46 transient thermal stress: a transient heat history's snapshot becomes
the *TEMPERATURE load of a static solve (V46, D70).

Ungated: snapshot selection over a crafted FrdModel history (right block,
time=None = last = V45, unknown time refuses LISTING the frd's own times, no
NDTEMP loud), `temperature_times`, session/CLI dangling-knob refusals.
ccx-gated (they RUN locally, skip on CI): the probe-shaped transient bar
chained snapshot-by-snapshot into the pointwise-exact all-nodes-pinned
constrained bar (sigma_z = -E*alpha*(T(z,t)-T0) per node), session e2e with
`temperature_time` in the journal, CLI two-step e2e.

THE PROBE TRAPS (v46_probe, session 31 — independently re-verified):
ccx's *TEMPERATURE, FILE= is unconditionally LAST-increment (BSTEP/BINC are
"parameter not recognized" no-ops) and a missing frd is FAIL-OPEN (exit 0,
fixed bogus temperature) — DigiTwin therefore always reads the frd itself
and inlines per-node lines; snapshot times come from the frd's OWN time list
(adaptive stepping makes them irregular — never assume k*dt); the pointwise
closed form needs EVERY node axially pinned (an end-clamped bar carries the
spatially-constant -E*alpha*(T_avg-T0) instead — equilibrium).
"""
from __future__ import annotations

import numpy as np
import pytest

from digitwin.export.deck import (
    BoundaryCondition,
    DeckSpec,
    Material,
    Step,
    write_deck,
)
from digitwin.solve.frd import (
    FrdField,
    FrdModel,
    nodal_temperatures,
    temperature_times,
)
from digitwin.solve.runner import find_ccx

needs_ccx = pytest.mark.skipif(
    find_ccx() is None, reason="CalculiX (ccx) not installed"
)


# ---------------------------------------------------------------------------
# fixtures (the V45 bar, self-contained like every test module here)


def _bar_wall(n_cross: int = 2, n_layers: int = 20, length: float = 20.0):
    """1 x 1 x length rectilinear hex bar via the V5 blocks path."""
    from digitwin.meshing import build_block_mesh, extrude_blocks

    rect = np.array([[0, 0], [1, 0], [1, 1], [0, 1]], dtype=float)
    bm = build_block_mesh([rect], target_size=1.0 / n_cross)
    return extrude_blocks(bm, thickness=length, n_layers=n_layers)


def _corner_node(wall, x: float, y: float, z: float = 0.0) -> int:
    pts = np.asarray(wall.grid.points)
    d = np.linalg.norm(pts - np.array([x, y, z]), axis=1)
    idx = int(np.argmin(d))
    assert d[idx] < 1e-9
    return idx


def _static_spec(bcs, **kw) -> DeckSpec:
    return DeckSpec(
        material=kw.pop(
            "material", Material.elastic(1000.0, 0.3, expansion=1e-5)
        ),
        step=kw.pop("step", Step()),
        boundary_conditions=tuple(bcs),
        target="ccx",
        **kw,
    )


# ---------------------------------------------------------------------------
# ungated: snapshot selection on a crafted history


def _history_model(times=(1.0, 2.0, 3.0)):
    blocks = [
        (t, np.array([[10.0 * (i + 1)], [10.0 * (i + 1) + 1.0]]))
        for i, t in enumerate(times)
    ]
    return FrdModel(
        node_ids=np.array([1, 2]),
        points=np.zeros((2, 3)),
        cell_types=None,
        cells=None,
        fields={"NDTEMP": FrdField("NDTEMP", ("T",), blocks[-1][1])},
        history={"NDTEMP": blocks},
    )


def test_snapshot_selection_and_times():
    model = _history_model()
    assert temperature_times(model) == (1.0, 2.0, 3.0)
    # time=None keeps the V45 behavior: the LAST block
    assert nodal_temperatures(model) == ((0, 30.0), (1, 31.0))
    assert nodal_temperatures(model, time=3.0) == ((0, 30.0), (1, 31.0))
    # a mid-history snapshot is genuinely different data
    assert nodal_temperatures(model, time=2.0) == ((0, 20.0), (1, 21.0))
    assert nodal_temperatures(model, time=1.0) == ((0, 10.0), (1, 11.0))


def test_unknown_time_refuses_listing_available():
    model = _history_model()
    with pytest.raises(ValueError, match="available times: 1, 2, 3"):
        nodal_temperatures(model, time=2.5)
    with pytest.raises(ValueError, match="no interpolation"):
        nodal_temperatures(model, time=0.0)
    # a long history truncates the listing but keeps the count + endpoints
    long = _history_model(times=tuple(float(k) for k in range(1, 41)))
    with pytest.raises(ValueError, match=r"\(40 total\)") as exc:
        nodal_temperatures(long, time=99.0)
    assert "40" in str(exc.value) and "1, 2" in str(exc.value)


def test_no_temperature_field_still_loud():
    empty = FrdModel(
        node_ids=np.array([1]),
        points=np.zeros((1, 3)),
        cell_types=None,
        cells=None,
    )
    with pytest.raises(ValueError, match="no temperature field"):
        temperature_times(empty)
    with pytest.raises(ValueError, match="no temperature field"):
        nodal_temperatures(empty, time=1.0)
    # non-finite in a SELECTED block is refused like V45's last-block check
    model = _history_model()
    model.history["NDTEMP"][1][1][0, 0] = float("nan")
    with pytest.raises(ValueError, match="non-finite temperature"):
        nodal_temperatures(model, time=2.0)


# ---------------------------------------------------------------------------
# ungated: session/CLI knob refusals (they fire before any solve)


def test_session_temperature_time_refusals():
    from digitwin.pipeline import MeshingSession

    session = MeshingSession()
    session.volmesh = _bar_wall(n_layers=4)
    with pytest.raises(ValueError, match="selects a snapshot"):
        session.compute_solve(
            analysis="static", temperature=120.0, temperature_time=5.0,
            expansion=1e-5, t_ref=20.0, fix="front:3",
        )
    with pytest.raises(ValueError, match="set analysis='static'"):
        session.compute_solve(
            analysis="heat", conductivity=2.0, temp="front=100",
            temperature_time=5.0,
        )
    with pytest.raises(ValueError, match="session-built decks"):
        session.compute_solve(deck="x.inp", temperature_time=5.0)


def test_cli_temperature_time_dangling(tmp_path):
    from digitwin.cli import main
    from digitwin.synth import generate

    surface, _ = generate("straight_tube")
    src = tmp_path / "tube.vtp"
    surface.save(src)
    rc = main([
        "deck", str(src), "--thickness", "1", "--n-theta", "8",
        "--n-axial", "6", "--n-layers", "2",
        "--outputs", str(tmp_path / "outputs"), "--target", "ccx",
        "--material", "elastic", "--E", "1000", "--nu", "0.3",
        "--expansion", "1e-5", "--t-ref", "20",
        "--temperature", "120", "--temperature-time", "5.0",
    ])
    assert rc == 1  # --temperature-time needs --temperature-from


# ---------------------------------------------------------------------------
# ccx-gated: the probe-shaped transient bar, chained snapshot by snapshot


def _transient_bar_frd():
    """Bar at T0=100, front face (z=0) suddenly held at 0, back insulated —
    the probe IBVP; DIRECT dt=0.5 over period 10 -> 20 NDTEMP snapshots."""
    from digitwin.solve import solve_deck

    wall = _bar_wall()
    heat_spec = DeckSpec(
        material=Material.thermal(10.0, density=1.0, specific_heat=1.0),
        step=Step(
            kind="heat", transient=True, dt=0.5, period=10.0,
            initial_temperature=100.0,
        ),
        boundary_conditions=(BoundaryCondition("front", 11, 11, 0.0),),
        target="ccx",
    )
    deck = write_deck(wall, heat_spec, "outputs/solve/v46_bar_heat")
    heat = solve_deck(deck, "outputs/solve/v46_bar_heat_out")
    return wall, heat.frd


def _dat_element_mean_szz(dat_path):
    """Per-element mean sigma_zz from the .dat STRESS table, located by its
    HEADER text (D67 trap: the strains table has the same row shape — a
    generic numeric-row regex silently corrupts the average)."""
    from collections import defaultdict
    from pathlib import Path

    acc: dict[int, list[float]] = defaultdict(list)
    active = False
    for line in Path(dat_path).read_text().splitlines():
        s = line.strip()
        if s.startswith("stresses (elem"):
            active = True
            continue
        if active:
            if not s:
                if acc:
                    break
                continue
            parts = s.split()
            if not parts[0].isdigit():
                break
            acc[int(parts[0])].append(float(parts[4]))
    assert acc, f"no stress table found in {dat_path}"
    return {eid: float(np.mean(v)) for eid, v in acc.items()}


@needs_ccx
def test_transient_snapshot_chain_pointwise_exact():
    """Two different snapshots each satisfy the closed form at the
    integration-point level: with EVERY node axially pinned and nu=0,
    eps_z = 0 pointwise, so each element's mean sigma_zz equals
    -E*alpha*(mean nodal T - T0) EXACTLY (Gauss mean of a trilinear field =
    its centroid value; probe: 5e-8). The two stress states must differ,
    proving the time selection picked distinct blocks."""
    from digitwin.solve import solve_deck

    wall, frd = _transient_bar_frd()
    times = temperature_times(frd)
    assert len(times) == 20
    assert times == tuple(0.5 * k for k in range(1, 21))

    n = wall.grid.n_points
    hexes = wall.grid.cells_dict[12]  # (n_cells, 8) 0-based node ids
    a = _corner_node(wall, 0, 0, 0)
    b = _corner_node(wall, 1, 0, 0)
    z = np.asarray(wall.grid.points)[:, 2]
    sigma = {}
    for tag, t in (("mid", times[9]), ("late", times[-1])):
        pairs = nodal_temperatures(frd, time=t)
        bcs = [BoundaryCondition(i, 3, 3) for i in range(n)]
        bcs += [BoundaryCondition(a, 1, 2), BoundaryCondition(b, 2, 2)]
        spec = _static_spec(
            tuple(bcs),
            material=Material.elastic(1000.0, 0.0, expansion=1e-5),
            step=Step(el_print={"WALL": ("S",)}),
            nodal_temperatures=pairs,
            reference_temperature=100.0,
        )
        deck = write_deck(wall, spec, f"outputs/solve/v46_bar_static_{tag}")
        result = solve_deck(deck, f"outputs/solve/v46_bar_static_{tag}_out")

        temps = np.empty(n)
        for nid, temp in pairs:
            temps[nid] = temp

        # EXACT oracle: per-element ip-mean sigma_zz vs the closed form
        means = _dat_element_mean_szz(result.dat_path)
        worst = max(
            abs(means[eid + 1] - (-1000.0 * 1e-5 * (temps[conn].mean() - 100.0)))
            for eid, conn in enumerate(hexes)
        )
        assert worst < 1e-6

        # supporting frd NODAL check away from the cooled face: ccx writes
        # ip-AVERAGED (not extrapolated) nodal stress, so the one-sided steep
        # gradient at z=0 reads ~half-an-element off (measured 0.041 at the
        # mid snapshot); the interior carries only the O(h^2) averaging bias
        # of the curved T(z) profile — measured 1.35e-3 worst-case here
        szz = np.asarray(result.frd.fields["STRESS"].values[:, 2], dtype=float)
        expected = -1000.0 * 1e-5 * (temps - 100.0)
        interior = z > 1.0 - 1e-9
        assert float(np.abs(szz[interior] - expected[interior]).max()) < 2.5e-3
        sigma[tag] = szz
    # the two snapshots are different physical states
    assert float(np.abs(sigma["mid"] - sigma["late"]).max()) > 0.05
    # and an off-list time refuses loudly even on a real frd
    with pytest.raises(ValueError, match="available times"):
        nodal_temperatures(frd, time=0.42)


@needs_ccx
def test_session_transient_chain_e2e():
    """Transient heat -> static at a chosen snapshot, all in one session;
    the journal records temperature_time; a bogus time refuses loudly."""
    from digitwin.pipeline import MeshingSession

    session = MeshingSession()
    session.volmesh = _bar_wall(n_layers=4)
    session.compute_solve(
        analysis="heat", conductivity=10.0, density=1.0, specific_heat=1.0,
        transient=1, dt=1.0, period=5.0, t0=100.0, temp="front=0",
    )
    frd_path = str(session.solve.frd_path)
    with pytest.raises(ValueError, match="available times"):
        session.compute_solve(
            analysis="static", expansion=1e-5, t_ref=100.0,
            temperature_from=frd_path, temperature_time=999.0,
            fix="front:3,back:3",
        )
    result = session.compute_solve(
        analysis="static", expansion=1e-5, t_ref=100.0,
        temperature_from=frd_path, temperature_time=3.0,
        fix="front:3,back:3",
    )
    assert result.summary["mises_max"] > 0.05
    solves = [e for e in session.journal if e["step"] == "solve"]
    assert len(solves) == 2
    assert solves[0]["params"]["transient"] == 1
    assert solves[1]["params"]["temperature_time"] == 3.0


@needs_ccx
def test_cli_transient_two_step_e2e(tmp_path, capsys):
    """CLI: deck heat --transient -> solve -> deck static --temperature-from
    --temperature-time -> solve; a bad time exits 1 listing the frd times."""
    from digitwin.cli import main
    from digitwin.synth import generate

    surface, _ = generate("straight_tube")
    src = tmp_path / "tube.vtp"
    surface.save(src)
    outputs = str(tmp_path / "outputs")
    mesh_args = [
        "--thickness", "1", "--n-theta", "8", "--n-axial", "6",
        "--n-layers", "2", "--outputs", outputs,
    ]
    rc = main([
        "deck", str(src), *mesh_args,
        "--material", "thermal", "--conductivity", "2.0",
        "--density", "1.0", "--specific-heat", "1.0",
        "--analysis", "heat", "--transient", "--dt", "2.5",
        "--period", "10", "--t0", "20",
        "--temp", "inner,100", "--target", "ccx",
    ])
    assert rc == 0
    rc = main(["solve", str(tmp_path / "outputs" / "tube_ccx.inp"),
               "--outputs", outputs])
    assert rc == 0
    frd = next((tmp_path / "outputs").rglob("*.frd"))

    static = [
        "deck", str(src), *mesh_args,
        "--material", "elastic", "--E", "1000", "--nu", "0.3",
        "--expansion", "1e-5", "--t-ref", "20",
        "--temperature-from", str(frd),
        "--fix", "inlet,3", "--fix", "outlet,3", "--pin-lateral",
        "--target", "ccx",
    ]
    rc = main(static + ["--temperature-time", "0.42",
                        "--out", str(tmp_path / "outputs" / "bad.inp")])
    assert rc == 1  # off-list time refuses, listing the frd's own times
    rc = main(static + ["--temperature-time", "5.0",
                        "--out", str(tmp_path / "outputs" / "ts5.inp")])
    assert rc == 0
    text = (tmp_path / "outputs" / "ts5.inp").read_text()
    assert "*TEMPERATURE\n1, " in text
    rc = main(["solve", str(tmp_path / "outputs" / "ts5.inp"),
               "--outputs", outputs])
    out = capsys.readouterr().out
    assert rc == 0
    assert "von Mises max" in out

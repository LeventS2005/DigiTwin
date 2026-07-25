"""V27 kOmegaSST turbulence templates: spec refusals + inlet k/omega
arithmetic + written-file exactness + laminar byte-stability (ungated),
then the OpenFOAM-gated Blasius friction-factor benchmark on the V26
O-grid pipe and a CLI turbulent smoke run."""
from __future__ import annotations

import numpy as np
import pytest

from meshforge.solve.foam_case import CfdBC, CfdSpec, write_case
from meshforge.solve.foam_runner import find_foam

RE = 1e4
NU = 2e-4  # R=1 (D=2), U_mean=1 -> Re = 1e4
F_BLASIUS = 0.316 * RE**-0.25  # 0.03160
TURB_LENGTH = 0.14  # 0.07 * D


def _ogrid_pipe(target_size: float, length: float, n_layers: int):
    from meshforge.meshing import extrude_blocks, ogrid_disk

    theta = np.linspace(0, 2 * np.pi, 129)[:-1]
    circle = np.column_stack([np.cos(theta), np.sin(theta)])
    bm = ogrid_disk([circle], target_size=target_size)
    return extrude_blocks(bm, thickness=length, n_layers=n_layers)


def _turb_spec(**overrides) -> CfdSpec:
    base = dict(
        solver="simpleFoam",
        nu=NU,
        bcs={
            "front": CfdBC("velocity", (0.0, 0.0, 1.0)),
            "back": CfdBC("pressure", 0.0),
        },
        end_time=5000.0,
        turbulence="kOmegaSST",
        turbulence_length=TURB_LENGTH,
    )
    base.update(overrides)
    return CfdSpec(**base)


# ---------------------------------------------------------------------------
# spec validation + inlet arithmetic (ungated)


def test_turbulence_spec_refusals():
    with pytest.raises(ValueError, match="unknown turbulence model"):
        _turb_spec(turbulence="kEpsilon")
    with pytest.raises(ValueError, match="turbulence needs simpleFoam"):
        _turb_spec(solver="icoFoam", delta_t=0.01)
    with pytest.raises(ValueError, match="turbulence_length"):
        _turb_spec(turbulence_length=None)
    with pytest.raises(ValueError, match="turbulence_length"):
        _turb_spec(turbulence_length=-1.0)
    with pytest.raises(ValueError, match="turbulence_intensity"):
        _turb_spec(turbulence_intensity=0.0)
    with pytest.raises(ValueError, match="turbulence_intensity"):
        _turb_spec(turbulence_intensity=1.5)
    with pytest.raises(ValueError, match="velocity BC"):
        _turb_spec(bcs={"back": CfdBC("pressure", 0.0)})
    with pytest.raises(ValueError, match="zero magnitude"):
        _turb_spec(
            bcs={
                "front": CfdBC("velocity", (0.0, 0.0, 0.0)),
                "back": CfdBC("pressure", 0.0),
            }
        )


def test_inlet_k_omega_arithmetic():
    spec = _turb_spec()  # |U| = 1, I = 0.05, l = 0.14
    k, omega = spec.turbulence_inlet()
    assert k == pytest.approx(1.5 * 0.05**2)  # 0.00375
    assert omega == pytest.approx(0.00375**0.5 / (0.09**0.25 * 0.14))
    # scales quadratically with |U| and I
    spec2 = _turb_spec(
        bcs={
            "front": CfdBC("velocity", (0.0, 0.0, 2.0)),
            "back": CfdBC("pressure", 0.0),
        }
    )
    assert spec2.turbulence_inlet()[0] == pytest.approx(4 * k)


# ---------------------------------------------------------------------------
# written case files (ungated)


@pytest.fixture(scope="module")
def turb_case(tmp_path_factory):
    case = tmp_path_factory.mktemp("turb") / "pipe"
    wall = _ogrid_pipe(0.3, 10.0, 10)
    kinds = write_case(wall, case, _turb_spec())
    return case, kinds


def test_turbulent_field_files(turb_case):
    case, kinds = turb_case
    assert kinds == {"front": "velocity", "back": "pressure", "outer_rim": "wall"}
    k = (case / "0" / "k").read_text()
    assert "internalField   uniform 0.00375;" in k
    assert "front { type fixedValue; value uniform 0.00375; }" in k
    assert "back { type zeroGradient; }" in k
    assert "outer_rim { type kqRWallFunction; value uniform 0.00375; }" in k
    omega = (case / "0" / "omega").read_text()
    assert "outer_rim { type omegaWallFunction;" in omega
    assert "[0 0 -1 0 0 0 0]" in omega
    nut = (case / "0" / "nut").read_text()
    assert "outer_rim { type nutUSpaldingWallFunction; value uniform 0; }" in nut
    assert "front { type calculated; value uniform 0; }" in nut
    assert "back { type calculated; value uniform 0; }" in nut


def test_turbulent_dicts(turb_case):
    case, _ = turb_case
    turb = (case / "constant" / "turbulenceProperties").read_text()
    assert "simulationType  RAS;" in turb
    assert "RASModel    kOmegaSST;" in turb
    schemes = (case / "system" / "fvSchemes").read_text()
    assert "div(phi,k) bounded Gauss upwind;" in schemes
    assert "div(phi,omega) bounded Gauss upwind;" in schemes
    assert "wallDist        { method meshWave; }" in schemes
    solution = (case / "system" / "fvSolution").read_text()
    assert '"(U|k|omega)"' in solution
    assert 'residualControl { p 1e-5; U 1e-6; "(k|omega)" 1e-6; }' in solution


def test_laminar_cases_untouched(tmp_path):
    """V23 laminar output stays byte-stable: no turbulence fields, no
    turbulent schemes/solvers, simulationType laminar."""
    case = tmp_path / "lam"
    wall = _ogrid_pipe(0.3, 10.0, 5)
    write_case(wall, case, _turb_spec(turbulence="laminar", turbulence_length=None))
    for f in ("k", "omega", "nut"):
        assert not (case / "0" / f).exists()
    assert "simulationType  laminar;" in (
        case / "constant" / "turbulenceProperties"
    ).read_text()
    schemes = (case / "system" / "fvSchemes").read_text()
    assert "div(phi,k)" not in schemes
    assert "wallDist" not in schemes
    assert "(k|omega)" not in (case / "system" / "fvSolution").read_text()


# ---------------------------------------------------------------------------
# session + CLI knob errors (ungated)


def test_session_turbulence_knob_errors():
    from meshforge.pipeline import MeshingSession

    session = MeshingSession()
    with pytest.raises(ValueError, match="turbulence knobs apply only"):
        session.compute_cfd(case="somewhere", turbulence="kOmegaSST")
    # spec validation fires before any mesh is required
    with pytest.raises(ValueError, match="turbulence_length"):
        session.compute_cfd(
            inlet="front=0,0,1", outlet="back=0", turbulence="kOmegaSST"
        )


def test_cli_turbulence_error_paths(tmp_path, capsys):
    from meshforge.cli import main

    rc = main(["cfd", str(tmp_path), "--turbulence", "kOmegaSST"])
    assert rc == 1
    assert "--turbulence applies only" in capsys.readouterr().err

    rc = main([
        "cfd", str(tmp_path),
        "--inlet", "front=0,0,1", "--outlet", "back=0",
        "--turbulence", "kOmegaSST",
    ])
    assert rc == 1
    assert "turbulence_length" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# OpenFOAM-gated (trap #16: skip cleanly when absent)

needs_foam = pytest.mark.skipif(
    find_foam() is None, reason="OpenFOAM not available"
)


@needs_foam
def test_blasius_friction_benchmark():
    """Turbulent pipe friction vs Blasius f = 0.316 Re^-0.25 at Re=1e4.

    V26 O-grid pipe R=1, L=40 (20 D), ts=0.15 (y+ ~ 16, buffer layer —
    the Spalding all-y+ wall function is load-bearing: nutkWallFunction
    measured -21% here, Spalding -0.7%). dp/dz plateaus by z ~ 20-25, so
    friction is measured on z in [25, 38]. Tolerance 5% (~7x margin)."""
    from meshforge.pipeline import MeshingSession

    session = MeshingSession()
    session.volmesh = _ogrid_pipe(0.15, 40.0, 100)
    result = session.compute_cfd(
        solver="simpleFoam",
        nu=NU,
        inlet="front=0,0,1",
        outlet="back=0",
        end_time=5000.0,
        turbulence="kOmegaSST",
        turb_length=TURB_LENGTH,
    )
    assert any("converged" in n for n in result.summary["notes"])
    assert session.journal[-1]["params"]["turbulence"] == "kOmegaSST"

    grid = session.cfd.grid
    assert {"U", "p", "U_mag", "k", "omega", "nut"} <= set(grid.point_data)

    # friction factor from the developed-tail kinematic pressure gradient
    line = grid.sample_over_line((0, 0, 25.0), (0, 0, 38.0), resolution=200)
    p = line["p"]
    ok = np.isfinite(p)
    slope = float(np.polyfit(line.points[ok, 2], p[ok], 1)[0])
    f_measured = 2.0 * 2.0 * (-slope) / 1.0**2  # f = 2 D (-dp/dz) / U^2
    assert abs(f_measured - F_BLASIUS) / F_BLASIUS < 0.05  # measured -0.7%

    # turbulent flatness: centerline/mean ratio ~1.22-1.27 (laminar = 2)
    tail = grid.sample_over_line((0, 0, 35.0), (0, 0, 39.0), resolution=50)
    ratio = float(np.nanmean(tail["U"][:, 2]))
    assert 1.15 < ratio < 1.35


@needs_foam
def test_cli_cfd_turbulent_smoke(tmp_path, capsys):
    """CLI e2e: polyMesh-only dir + --turbulence kOmegaSST solves and
    persists results (physics is the benchmark's job)."""
    from meshforge.cli import main
    from meshforge.export.foam import write_polymesh

    case = tmp_path / "turb_cli"
    wall = _ogrid_pipe(0.3, 20.0, 50)
    write_polymesh(wall, case, patch_types={"front": "patch", "back": "patch"})
    rc = main([
        "cfd", str(case),
        "--nu", str(NU),
        "--inlet", "front=0,0,1",
        "--outlet", "back=0",
        "--end-time", "5000",
        "--turbulence", "kOmegaSST",
        "--turb-length", str(TURB_LENGTH),
        "--outputs", str(tmp_path / "outputs"),
    ])
    out = capsys.readouterr().out
    assert rc == 0
    assert "cfd[simpleFoam]" in out
    assert (tmp_path / "outputs" / "cfd" / "turb_cli" / "turb_cli_results.vtu").is_file()

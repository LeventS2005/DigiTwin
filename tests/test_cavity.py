"""V30 closed-cavity mode: loud validation + written case files +
split_boundary_group exactness (ungated), then the OpenFOAM-gated Ghia
et al. (1982) Re=100 lid-driven cavity benchmark and the CLI e2e (skip
cleanly without OpenFOAM, trap #16)."""
from __future__ import annotations

import numpy as np
import pytest

from meshforge.meshing import (
    build_block_mesh,
    extrude_blocks,
    split_boundary_group,
)
from meshforge.solve.foam_case import CfdBC, CfdSpec, write_case
from meshforge.solve.foam_runner import find_foam

# Ghia, Ghia & Shin (1982), tables I/II, Re=100 (verified against the
# published dataset this session). Interior rows only (endpoints are
# BC-trivial).
GHIA_Y_U = [
    (0.9766, 0.84123), (0.9688, 0.78871), (0.9609, 0.73722),
    (0.9531, 0.68717), (0.8516, 0.23151), (0.7344, 0.00332),
    (0.6172, -0.13641), (0.5000, -0.20581), (0.4531, -0.21090),
    (0.2813, -0.15662), (0.1719, -0.10150), (0.1016, -0.06434),
    (0.0703, -0.04775), (0.0625, -0.04192), (0.0547, -0.03717),
]
GHIA_X_V = [
    (0.9688, -0.05906), (0.9609, -0.07391), (0.9531, -0.08864),
    (0.9453, -0.10313), (0.9063, -0.16914), (0.8594, -0.22445),
    (0.8047, -0.24533), (0.5000, 0.05454), (0.2344, 0.17527),
    (0.2266, 0.17507), (0.1563, 0.16077), (0.0938, 0.12317),
    (0.0781, 0.10890), (0.0703, 0.10091), (0.0625, 0.09233),
]


def _cavity_wall(n: int):
    """Unit-square cavity, one cell thick, rim split into lid + walls."""
    loop = np.array([[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0]])
    bm = build_block_mesh([loop], target_size=1.0 / n)
    wall = extrude_blocks(bm, thickness=0.1, n_layers=1)
    split_boundary_group(
        wall, "outer_rim", axis="y", threshold=1.0 - 1e-9, name="lid"
    )
    return wall


def _cavity_spec(**overrides) -> CfdSpec:
    base = dict(
        solver="simpleFoam",
        nu=0.01,
        bcs={
            "lid": CfdBC("moving_wall", (1.0, 0.0, 0.0)),
            "front": CfdBC("empty"),
            "back": CfdBC("empty"),
        },
        end_time=2000.0,
        closed_cavity=True,
    )
    base.update(overrides)
    return CfdSpec(**base)


# ---------------------------------------------------------------------------
# BC kinds (ungated)


def test_moving_wall_and_empty_bc_validation():
    with pytest.raises(ValueError, match=r"\(vx, vy, vz\)"):
        CfdBC("moving_wall", (1.0, 0.0))
    with pytest.raises(ValueError, match="takes no value"):
        CfdBC("empty", 1.0)
    assert CfdBC("moving_wall", (1, 0, 0)).value == (1.0, 0.0, 0.0)
    assert CfdBC("empty").value is None


# ---------------------------------------------------------------------------
# closed-cavity refusals (ungated)


def test_closed_cavity_refuses_pressure_bc():
    with pytest.raises(ValueError, match="contradiction"):
        _cavity_spec(bcs={
            "lid": CfdBC("moving_wall", (1, 0, 0)),
            "out": CfdBC("pressure", 0.0),
        })


def test_closed_cavity_refuses_velocity_inlet():
    with pytest.raises(ValueError, match="net inflow"):
        _cavity_spec(bcs={
            "lid": CfdBC("moving_wall", (1, 0, 0)),
            "jet": CfdBC("velocity", (0, 0, 1)),
        })


def test_closed_cavity_needs_a_moving_wall():
    with pytest.raises(ValueError, match="nothing driving"):
        _cavity_spec(bcs={"front": CfdBC("empty")})


def test_closed_cavity_p_ref_cell_non_negative():
    with pytest.raises(ValueError, match="non-negative"):
        _cavity_spec(p_ref_cell=-1)


def test_open_case_refusal_names_the_knob():
    with pytest.raises(ValueError, match="closed_cavity"):
        CfdSpec(bcs={"front": CfdBC("velocity", (0, 0, 1))})


# ---------------------------------------------------------------------------
# written case files (ungated)


@pytest.fixture(scope="module")
def cavity_case(tmp_path_factory):
    case = tmp_path_factory.mktemp("cavity") / "cav"
    wall = _cavity_wall(8)
    kinds = write_case(wall, case, _cavity_spec())
    return case, kinds


def test_cavity_patch_kinds_and_boundary_types(cavity_case):
    case, kinds = cavity_case
    assert kinds == {
        "front": "empty", "back": "empty",
        "lid": "moving_wall", "outer_rim": "wall",
    }
    boundary = (case / "constant" / "polyMesh" / "boundary").read_text()
    assert boundary.count("type            empty;") == 2
    assert boundary.count("type            wall;") == 2  # lid + outer_rim


def test_cavity_field_entries(cavity_case):
    case, _ = cavity_case
    u = (case / "0" / "U").read_text()
    assert "lid { type fixedValue; value uniform (1 0 0); }" in u
    assert "outer_rim { type noSlip; }" in u
    assert "front { type empty; }" in u
    assert "internalField   uniform (0 0 0)" in u  # cavity starts at rest
    p = (case / "0" / "p").read_text()
    assert "lid { type zeroGradient; }" in p
    assert "back { type empty; }" in p


def test_cavity_fvsolution_carries_pref(cavity_case):
    case, _ = cavity_case
    sol = (case / "system" / "fvSolution").read_text()
    assert "pRefCell        0;" in sol
    assert "pRefValue       0;" in sol
    assert "__PREF__" not in sol


def test_pref_value_and_cell_knobs(tmp_path):
    case = tmp_path / "cav"
    write_case(
        _cavity_wall(4), case, _cavity_spec(p_ref_cell=7, p_ref_value=2.5)
    )
    sol = (case / "system" / "fvSolution").read_text()
    assert "pRefCell        7;" in sol
    assert "pRefValue       2.5;" in sol


def test_open_cases_carry_no_pref_and_no_token(tmp_path):
    """The V30 token must vanish from every template on open cases."""
    for solver, extra in (
        ("simpleFoam", {}),
        ("icoFoam", {"end_time": 0.2, "delta_t": 0.01}),
        ("simpleFoam", {"turbulence": "kOmegaSST", "turb_length": 0.14}),
    ):
        spec = CfdSpec(
            solver=solver,
            nu=0.1,
            bcs={
                "front": CfdBC("velocity", (0, 0, 1)),
                "back": CfdBC("pressure", 0.0),
            },
            turbulence=extra.pop("turbulence", "laminar"),
            turbulence_length=extra.pop("turb_length", None),
            **extra,
        )
        case = tmp_path / f"open_{solver}_{spec.turbulence}"
        loop = np.array([[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0]])
        wall = extrude_blocks(
            build_block_mesh([loop], target_size=0.5), thickness=1.0,
            n_layers=1,
        )
        write_case(wall, case, spec)
        sol = (case / "system" / "fvSolution").read_text()
        assert "__PREF__" not in sol
        assert "pRefCell" not in sol


# ---------------------------------------------------------------------------
# split_boundary_group (ungated)


def test_split_boundary_group_exactness():
    loop = np.array([[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0]])
    bm = build_block_mesh([loop], target_size=0.25)
    wall = extrude_blocks(bm, thickness=0.1, n_layers=1)
    n_rim = wall.boundary["outer_rim"].n_faces
    assert n_rim == 16  # 4 sides x 4 faces

    split_boundary_group(
        wall, "outer_rim", axis="y", threshold=1.0 - 1e-9, name="lid"
    )
    assert wall.boundary["lid"].n_faces == 4
    assert wall.boundary["outer_rim"].n_faces == 12
    pts = np.asarray(wall.grid.points)
    lid_cy = pts[wall.boundary["lid"].face_nodes].mean(axis=1)[:, 1]
    assert np.allclose(lid_cy, 1.0)
    assert any("split_patch" in n for n in wall.notes)


def test_split_boundary_group_below_side():
    wall = extrude_blocks(
        build_block_mesh(
            [np.array([[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0]])],
            target_size=0.25,
        ),
        thickness=0.1, n_layers=1,
    )
    split_boundary_group(
        wall, "outer_rim", axis="y", threshold=1e-9, name="floor",
        side="below",
    )
    assert wall.boundary["floor"].n_faces == 4


def test_split_boundary_group_is_loud():
    wall = _cavity_wall(4)  # already carries a lid
    with pytest.raises(ValueError, match="unknown boundary group"):
        split_boundary_group(wall, "nope", axis="y", threshold=0.5, name="x")
    with pytest.raises(ValueError, match="axis must be"):
        split_boundary_group(
            wall, "outer_rim", axis="r", threshold=0.5, name="x"
        )
    with pytest.raises(ValueError, match="side must be"):
        split_boundary_group(
            wall, "outer_rim", axis="y", threshold=0.5, name="x", side="up"
        )
    with pytest.raises(ValueError, match="already exists"):
        split_boundary_group(
            wall, "outer_rim", axis="y", threshold=0.5, name="lid"
        )
    with pytest.raises(ValueError, match="wrong threshold"):
        split_boundary_group(
            wall, "outer_rim", axis="y", threshold=99.0, name="x"
        )
    with pytest.raises(ValueError, match="wrong threshold"):
        split_boundary_group(
            wall, "outer_rim", axis="y", threshold=-99.0, name="x"
        )


# ---------------------------------------------------------------------------
# session + CLI plumbing (ungated)


def test_session_split_patch_step():
    from meshforge.pipeline import STEP_KNOBS, _WORKFILE_STEPS, MeshingSession

    assert "split_patch" in STEP_KNOBS
    assert "split_patch" in _WORKFILE_STEPS
    for knob in ("moving_wall", "empty", "closed_cavity", "p_ref_cell",
                 "p_ref_value"):
        assert knob in STEP_KNOBS["cfd"]

    session = MeshingSession()
    with pytest.raises(RuntimeError, match="volmesh"):
        session.compute_split_patch(threshold=1.0, name="lid")

    session.volmesh = _cavity_wall(4)  # lid exists; split walls again
    with pytest.raises(ValueError, match="threshold"):
        session.compute_split_patch(name="floor")
    with pytest.raises(ValueError, match="group name"):
        session.compute_split_patch(threshold=1e-9)
    result = session.compute_split_patch(
        threshold=1e-9, name="floor", side="below"
    )
    assert result.summary["groups"]["floor"] == 4
    assert session.journal[-1]["step"] == "split_patch"


def test_session_cfd_closed_cavity_knob_errors():
    from meshforge.pipeline import MeshingSession

    session = MeshingSession()
    with pytest.raises(ValueError, match="already carries its BCs"):
        session.compute_cfd(case="somewhere", moving_wall="lid=1,0,0")
    with pytest.raises(ValueError, match="already carries its fvSolution"):
        session.compute_cfd(case="somewhere", closed_cavity=1)


def test_cli_closed_cavity_flag_errors(tmp_path, capsys):
    from meshforge.cli import main

    rc = main(["cfd", str(tmp_path), "--closed-cavity"])
    assert rc == 1
    assert "solver files are built here" in capsys.readouterr().err

    rc = main([
        "cfd", str(tmp_path),
        "--moving-wall", "lid=1,0,0", "--empty", "front,back",
    ])  # closed flag missing -> open-case refusal, loud
    assert rc == 1
    assert "closed_cavity" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# OpenFOAM-gated: the Ghia benchmark (trap #16: skip cleanly when absent)

needs_foam = pytest.mark.skipif(
    find_foam() is None, reason="OpenFOAM not available"
)


def _profiles(grid):
    zmid = 0.05
    uline = grid.sample_over_line((0.5, 0, zmid), (0.5, 1, zmid), resolution=400)
    y, u = uline.points[:, 1], uline["U"][:, 0]
    ok = np.isfinite(u)
    ghia_y = np.array([p[0] for p in GHIA_Y_U])
    du = np.interp(ghia_y, y[ok], u[ok]) - np.array([p[1] for p in GHIA_Y_U])

    vline = grid.sample_over_line((0, 0.5, zmid), (1, 0.5, zmid), resolution=400)
    x, v = vline.points[:, 0], vline["U"][:, 1]
    ok = np.isfinite(v)
    ghia_x = np.array([p[0] for p in GHIA_X_V])
    dv = np.interp(ghia_x, x[ok], v[ok]) - np.array([p[1] for p in GHIA_X_V])
    return np.abs(du).max(), np.abs(dv).max()


@needs_foam
def test_ghia_lid_driven_cavity_benchmark():
    """Re=100 closed cavity vs Ghia et al. (1982): 64^2 blocks mesh, one
    moving wall, empty front/back, pRefCell — max centerline deviation
    < 0.02 of lid speed (probe: 0.0055 / 0.0085; the plateau is the
    reference's own 129^2 accuracy, not ours)."""
    from meshforge.pipeline import MeshingSession

    session = MeshingSession()
    session.volmesh = _cavity_wall(64)
    result = session.compute_cfd(
        nu=0.01,
        moving_wall="lid=1,0,0",
        empty="front,back",
        closed_cavity=1,
        end_time=2000.0,
    )
    assert any("converged" in n for n in result.summary["notes"])
    assert session.journal[-1]["step"] == "cfd"

    du_max, dv_max = _profiles(session.cfd.grid)
    assert du_max < 0.02, f"u profile off Ghia by {du_max}"
    assert dv_max < 0.02, f"v profile off Ghia by {dv_max}"


@needs_foam
def test_cli_cavity_e2e(tmp_path, capsys):
    from meshforge.cli import main
    from meshforge.export.foam import write_polymesh

    case = tmp_path / "cavity_cli"
    wall = _cavity_wall(16)
    write_polymesh(
        wall, case,
        patch_types={"front": "empty", "back": "empty", "lid": "wall"},
    )
    rc = main([
        "cfd", str(case),
        "--nu", "0.01",
        "--moving-wall", "lid=1,0,0",
        "--empty", "front,back",
        "--closed-cavity",
        "--end-time", "2000",
        "--outputs", str(tmp_path / "outputs"),
    ])
    out = capsys.readouterr().out
    assert rc == 0
    assert "cfd[simpleFoam]" in out
    assert (
        tmp_path / "outputs" / "cfd" / "cavity_cli" / "cavity_cli_results.vtu"
    ).is_file()

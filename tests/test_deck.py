"""V2 deck builder: materials, BCs, loads, full decks, Lamé ccx benchmark."""
from __future__ import annotations

import subprocess
from pathlib import Path

import numpy as np
import pytest
import pyvista as pv

from meshforge.cli import main
from meshforge.export.deck import (
    BoundaryCondition,
    DeckSpec,
    Load,
    Material,
    Step,
    lateral_pins,
    write_deck,
)
from meshforge.geometry import compute_centerline
from meshforge.meshing import get_backend
from meshforge.meshing.base import WallMesh
from meshforge.synth import generate


@pytest.fixture(scope="module")
def hex_wall() -> WallMesh:
    surface, _ = generate("straight_tube")
    cl = compute_centerline(surface)
    return get_backend("swept").mesh_wall(
        surface, 0.6, centerline=cl, n_theta=12, n_axial=8, n_layers=2
    )


def _basic_spec(**over) -> DeckSpec:
    kw = dict(
        material=Material.elastic(1000.0, 0.3),
        step=Step(),
        boundary_conditions=(BoundaryCondition("inlet", 3),),
        loads=(Load.pressure("inner", 1.0),),
        target="abaqus",
    )
    kw.update(over)
    return DeckSpec(**kw)


# ---------------------------------------------------------------------------
# materials


def test_material_golden_blocks():
    el = Material.elastic(200e3, 0.3, name="STEEL", density=7.85e-9)
    assert el.lines() == [
        "*MATERIAL, NAME=STEEL",
        "*ELASTIC",
        "200000, 0.3",
        "*DENSITY",
        "7.85e-09",
    ]
    nh = Material.neo_hookean(0.5, 0.06)
    assert nh.lines() == ["*MATERIAL, NAME=MAT", "*HYPERELASTIC, NEO HOOKE", "0.5, 0.06"]
    assert nh.is_hyperelastic
    mr = Material.mooney_rivlin(0.3, 0.1, 0.02, name="RUBBER")
    assert mr.lines()[-1] == "0.3, 0.1, 0.02"
    assert not el.is_hyperelastic


def test_material_validation():
    with pytest.raises(ValueError, match="positive"):
        Material.elastic(0.0, 0.3)
    with pytest.raises(ValueError, match="nu"):
        Material.elastic(1000.0, 0.5)
    with pytest.raises(ValueError, match="nu"):
        Material.elastic(1000.0, -1.0)
    with pytest.raises(ValueError, match="C10"):
        Material.neo_hookean(-1.0, 0.1)
    with pytest.raises(ValueError, match="D1"):
        Material.neo_hookean(1.0, -0.1)
    with pytest.raises(ValueError, match="C10"):
        Material.mooney_rivlin(-0.3, 0.1, 0.02)


# ---------------------------------------------------------------------------
# BCs + loads + spec validation


def test_bc_lines_and_validation():
    assert BoundaryCondition("inlet", 3).line() == "INLET, 3, 3"
    assert BoundaryCondition("outlet", 1, 3).line() == "OUTLET, 1, 3"
    assert BoundaryCondition(41, 2).line() == "42, 2, 2"  # 0-based -> 1-based
    assert BoundaryCondition("inlet", 3, value=0.5).line() == "INLET, 3, 3, 0.5"
    with pytest.raises(ValueError, match="DOF"):
        BoundaryCondition("inlet", 0)
    with pytest.raises(ValueError, match="DOF"):
        BoundaryCondition("inlet", 4)
    with pytest.raises(ValueError, match="last_dof"):
        BoundaryCondition("inlet", 3, 1)


def test_load_validation():
    with pytest.raises(ValueError, match="DOF"):
        Load.concentrated("inlet", 6, 1.0)


def test_load_direct_construction_validated():
    """Direct Load(...) must be as safe as the factories (V44 review)."""
    with pytest.raises(ValueError, match="unknown load kind"):
        Load(kind="torque", target="inner", magnitude=1.0)
    with pytest.raises(ValueError, match="DOFs are 1..3"):
        Load(kind="cload", target="inlet", magnitude=1.0, dof=5)
    with pytest.raises(ValueError, match="DOFs are 1..3"):
        Load(kind="cload", target="inlet", magnitude=1.0)  # dof missing
    with pytest.raises(ValueError, match="named boundary surface"):
        Load(kind="pressure", target=3, magnitude=1.0)
    with pytest.raises(ValueError, match="only to concentrated"):
        Load(kind="pressure", target="inner", magnitude=1.0, dof=1)
    with pytest.raises(ValueError, match="film coefficient"):
        Load(kind="film", target="outer", magnitude=20.0)  # h missing
    with pytest.raises(ValueError, match="only to film"):
        Load(kind="pressure", target="inner", magnitude=1.0, h=5.0)
    with pytest.raises(ValueError, match="emissivity"):
        Load(kind="radiate", target="outer", magnitude=20.0)  # ε missing
    with pytest.raises(ValueError, match="only to radiate"):
        Load(kind="dflux", target="outer", magnitude=1.0, emissivity=0.5)
    # the factories still construct fine
    assert Load.concentrated(0, 3, -1.5).dof == 3


@pytest.mark.parametrize("target", ["abaqus", "ccx", "nastran", "lsdyna", "ansys"])
def test_cload_int_target_out_of_range_refused_every_target(hex_wall, tmp_path, target):
    """An int cload node id past n_points was fail-open in EVERY target's
    deck (V44 review follow-up) — now refused pre-dispatch like the
    BoundaryCondition int check."""
    spec = _basic_spec(loads=(Load.concentrated(999_999, 3, -1.5),), target=target)
    with pytest.raises(ValueError, match="out of range"):
        write_deck(hex_wall, spec, tmp_path / f"oob_{target}")
    ok = _basic_spec(loads=(Load.concentrated(0, 3, -1.5),), target=target)
    assert write_deck(hex_wall, ok, tmp_path / f"ok_{target}").exists()


def test_spec_ccx_refuses_hybrid(hex_wall, tmp_path):
    spec = _basic_spec(target="ccx", formulation="hybrid")
    with pytest.raises(ValueError, match="hybrid"):
        write_deck(hex_wall, spec, tmp_path / "x")


def test_spec_hyperelastic_needs_nlgeom(hex_wall, tmp_path):
    spec = _basic_spec(material=Material.neo_hookean(0.5, 0.06), step=Step(nlgeom=False))
    with pytest.raises(ValueError, match="NLGEOM"):
        write_deck(hex_wall, spec, tmp_path / "x")


def test_spec_ccx_hyperelastic_needs_positive_d1(hex_wall, tmp_path):
    spec = _basic_spec(
        material=Material.neo_hookean(0.5, 0.0),
        step=Step(nlgeom=True),
        target="ccx",
    )
    with pytest.raises(ValueError, match="D1"):
        write_deck(hex_wall, spec, tmp_path / "x")


def test_unknown_surface_and_nset_are_loud(hex_wall, tmp_path):
    with pytest.raises(ValueError, match="unknown surface"):
        write_deck(hex_wall, _basic_spec(loads=(Load.pressure("nope", 1.0),)), tmp_path / "a")
    with pytest.raises(ValueError, match="unknown group"):
        write_deck(
            hex_wall,
            _basic_spec(boundary_conditions=(BoundaryCondition("nope", 3),)),
            tmp_path / "b",
        )
    with pytest.raises(ValueError, match="unknown NSET"):
        write_deck(
            hex_wall,
            _basic_spec(step=Step(node_print={"nope": ("U",)})),
            tmp_path / "c",
        )
    with pytest.raises(ValueError, match="unknown ELSET"):
        write_deck(
            hex_wall,
            _basic_spec(step=Step(el_print={"nope": ("S",)})),
            tmp_path / "d",
        )
    with pytest.raises(ValueError, match="out of range"):
        write_deck(
            hex_wall,
            _basic_spec(
                boundary_conditions=(BoundaryCondition(10**7, 1),)
            ),
            tmp_path / "e",
        )


# ---------------------------------------------------------------------------
# full deck structure


def test_full_deck_structure_abaqus_hybrid(hex_wall, tmp_path):
    spec = _basic_spec(
        boundary_conditions=(
            BoundaryCondition("inlet", 3),
            BoundaryCondition("outlet", 3),
            BoundaryCondition(0, 2, value=0.1),
        ),
        loads=(Load.pressure("inner", 1.5), Load.concentrated("outlet", 1, 2.0)),
        heading="lame check",
    )
    out = write_deck(hex_wall, spec, tmp_path / "deck")
    text = out.read_text()

    assert text.startswith("*HEADING\nlame check\n")
    assert "** UNITS: length=mm, force=N -> stress = N/mm^2" in text
    assert "*ELEMENT, TYPE=C3D8H, ELSET=WALL" in text  # abaqus default: hybrid
    for name in ("INNER", "OUTER", "INLET", "OUTLET"):
        assert f"*NSET, NSET={name}" in text
        assert f"*SURFACE, NAME={name}_SURF, TYPE=ELEMENT" in text
        assert f"*ELSET, ELSET={name}_ELS" in text
    assert "*SOLID SECTION, ELSET=WALL, MATERIAL=MAT" in text
    assert "\n*STEP\n*STATIC\n" in text
    assert "\nINLET, 3, 3\n" in text
    assert "\n1, 2, 2, 0.1\n" in text  # node id 0 -> 1-based, with value
    assert "\n*DSLOAD\nINNER_SURF, P, 1.5\n" in text
    assert "\n*CLOAD\nOUTLET, 1, 2\n" in text
    assert "\n*NODE FILE\nU\n" in text
    assert "\n*EL FILE\nS, E\n" in text
    assert text.rstrip().endswith("*END STEP")


def test_nlgeom_written(hex_wall, tmp_path):
    spec = _basic_spec(material=Material.neo_hookean(0.5, 0.06), step=Step(nlgeom=True))
    text = write_deck(hex_wall, spec, tmp_path / "nl").read_text()
    assert "*STEP, NLGEOM" in text
    assert "*HYPERELASTIC, NEO HOOKE" in text
    assert "*SURFACE, NAME=INNER_SURF, TYPE=ELEMENT" in text  # sets intact


def test_ccx_deck_meshio_roundtrip(hex_wall, tmp_path):
    import meshio

    spec = _basic_spec(target="ccx")
    out = write_deck(hex_wall, spec, tmp_path / "ccx_deck")
    text = out.read_text()
    assert "TYPE=C3D8," in text  # ccx default: full (no hybrid library)
    back = meshio.read(out)
    assert len(back.points) == hex_wall.grid.n_points
    assert (
        sum(len(b.data) for b in back.cells if b.type == "hexahedron")
        == hex_wall.grid.n_cells
    )


def test_bare_grid_deck(hex_wall, tmp_path):
    bare = pv.UnstructuredGrid(hex_wall.grid)
    bare.cell_data.clear()
    spec = DeckSpec(material=Material.elastic(1000.0, 0.3), step=Step())
    out = write_deck(bare, spec, tmp_path / "bare")
    text = out.read_text()
    assert "** NOTE: bare grid: region defaulted to 1" in text
    assert "*ELSET, ELSET=REGION1" in text
    # name-based references must fail loudly on a bare grid
    with pytest.raises(ValueError, match="unknown group"):
        write_deck(
            bare,
            DeckSpec(
                material=Material.elastic(1000.0, 0.3),
                step=Step(),
                boundary_conditions=(BoundaryCondition("inlet", 3),),
            ),
            tmp_path / "bare2",
        )


def test_lateral_pins_positions_and_dofs(hex_wall):
    pins = lateral_pins(hex_wall)
    assert [p.first_dof for p in pins] == [2, 2, 1]
    ids = [int(p.target) for p in pins]
    assert len(set(ids)) == 3
    pts = np.asarray(hex_wall.grid.points)[ids]
    inlet_nodes = np.unique(hex_wall.boundary["inlet"].face_nodes)
    center = np.asarray(hex_wall.grid.points)[inlet_nodes].mean(axis=0)
    ang = np.arctan2(pts[:, 1] - center[1], pts[:, 0] - center[0])
    assert abs(ang[0]) < 0.3  # near theta = 0
    assert abs(abs(ang[1]) - np.pi) < 0.3  # near theta = pi
    assert abs(ang[2] - np.pi / 2) < 0.3  # near theta = pi/2
    with pytest.raises(ValueError, match="not found"):
        lateral_pins(hex_wall, nset="nope")


# ---------------------------------------------------------------------------
# CLI


def test_cli_deck_happy_path(tmp_path, capsys):
    surface, _ = generate("straight_tube")
    vtp = tmp_path / "tube.vtp"
    surface.save(vtp)
    out_dir = tmp_path / "outputs"
    rc = main(
        [
            "deck",
            str(vtp),
            "--thickness", "0.6",
            "--n-theta", "12", "--n-axial", "8", "--n-layers", "2",
            "--E", "1000", "--nu", "0.3",
            "--fix", "inlet,3", "--fix", "outlet,3",
            "--pin-lateral",
            "--pressure", "inner,1.0",
            "--target", "ccx",
            "--outputs", str(out_dir),
        ]
    )
    assert rc == 0
    text = (out_dir / "tube_ccx.inp").read_text()
    assert "TYPE=C3D8," in text
    assert "INNER_SURF, P, 1" in text
    assert capsys.readouterr().out.count("boundary condition(s)")


def test_cli_deck_missing_material_params(tmp_path, capsys):
    surface, _ = generate("straight_tube")
    vtp = tmp_path / "tube.vtp"
    surface.save(vtp)
    rc = main(["deck", str(vtp), "--thickness", "0.6", "--nu", "0.3"])
    assert rc == 1
    assert "--E" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# ccx-gated benchmarks (skip cleanly when the solver is absent — trap #16)


# V13 promoted the ccx lookup + .dat parsers into meshforge.solve; the
# benchmarks below now exercise the shared implementations.
from meshforge.solve import find_ccx as _find_ccx  # noqa: E402
from meshforge.solve.dat import (  # noqa: E402
    parse_displacements as _parse_dat_displacements,
    parse_stresses as _parse_dat_stresses,
)


def _run_ccx(deck: Path) -> str:
    ccx = _find_ccx()
    proc = subprocess.run(
        [ccx, "-i", deck.stem], cwd=deck.parent, capture_output=True, text=True,
        timeout=600,
    )
    assert proc.returncode == 0, proc.stdout[-2000:] + proc.stderr[-2000:]
    return (deck.parent / f"{deck.stem}.dat").read_text()


# CalculiX C3D8: 2x2x2 Gauss points at +-1/sqrt(3), xi fastest, then eta, zeta.
_GP = 1.0 / np.sqrt(3.0)
_IP_LOCAL = np.array(
    [
        [-_GP, -_GP, -_GP], [_GP, -_GP, -_GP], [-_GP, _GP, -_GP], [_GP, _GP, -_GP],
        [-_GP, -_GP, _GP], [_GP, -_GP, _GP], [-_GP, _GP, _GP], [_GP, _GP, _GP],
    ]
)
# VTK/Abaqus hex node local coordinates (bottom 0-3 CCW, top 4-7).
_NODE_LOCAL = np.array(
    [
        [-1, -1, -1], [1, -1, -1], [1, 1, -1], [-1, 1, -1],
        [-1, -1, 1], [1, -1, 1], [1, 1, 1], [-1, 1, 1],
    ],
    dtype=float,
)


def _ip_positions(cell_pts: np.ndarray) -> np.ndarray:
    """Global positions of the 8 Gauss points via the trilinear map."""
    out = []
    for xi in _IP_LOCAL:
        shape = np.prod(1.0 + _NODE_LOCAL * xi, axis=1) / 8.0
        out.append(shape @ cell_pts)
    return np.array(out)


def _lame_wall() -> WallMesh:
    surface, _ = generate("straight_tube")  # a = 3, L = 20
    cl = compute_centerline(surface)
    wall = get_backend("swept").mesh_wall(
        surface, 1.0, centerline=cl, n_theta=32, n_axial=6, n_layers=6
    )
    assert wall.quality.n_inverted == 0
    return wall


def test_ccx_lame_thick_wall_cylinder(tmp_path):
    """Flagship V2 benchmark: thick-walled cylinder under internal pressure,
    plane strain (u_z = 0 at both ends + tangential pins). Radial displacement
    at both walls and integration-point hoop stress must match Lamé."""
    if _find_ccx() is None:
        pytest.skip("CalculiX (ccx) not available")

    wall = _lame_wall()
    E, nu, p = 1000.0, 0.3, 1.0
    spec = DeckSpec(
        material=Material.elastic(E, nu, name="LAME"),
        step=Step(
            node_print={"inner": ("U",), "outer": ("U",)},
            el_print={"inner_els": ("S",)},
        ),
        boundary_conditions=(
            BoundaryCondition("inlet", 3),
            BoundaryCondition("outlet", 3),
            *lateral_pins(wall),
        ),
        loads=(Load.pressure("inner", p),),
        target="ccx",
    )
    deck = write_deck(wall, spec, tmp_path / "lame")
    dat = _run_ccx(deck)

    pts = np.asarray(wall.grid.points)
    inner_nodes = np.unique(wall.boundary["inner"].face_nodes)
    outer_nodes = np.unique(wall.boundary["outer"].face_nodes)
    r_node = np.hypot(pts[:, 0], pts[:, 1])
    a = float(r_node[inner_nodes].mean())  # honest mesh radii, not nominal
    b = float(r_node[outer_nodes].mean())

    A = p * a**2 / (b**2 - a**2)
    B = p * a**2 * b**2 / (b**2 - a**2)

    def u_r_analytic(r: float) -> float:
        return (1 + nu) / E * p * a**2 / (b**2 - a**2) * ((1 - 2 * nu) * r + b**2 / r)

    u = _parse_dat_displacements(dat)
    assert u, "no displacement block in .dat"

    def mean_u_r(nodes: np.ndarray) -> float:
        vals = []
        for n in nodes:
            ux, uy, _ = u[int(n) + 1]
            r = r_node[n]
            vals.append((ux * pts[n, 0] + uy * pts[n, 1]) / r)
        return float(np.mean(vals))

    got_a, want_a = mean_u_r(inner_nodes), u_r_analytic(a)
    got_b, want_b = mean_u_r(outer_nodes), u_r_analytic(b)
    assert got_a == pytest.approx(want_a, rel=0.03), (got_a, want_a)
    assert got_b == pytest.approx(want_b, rel=0.03), (got_b, want_b)

    # Hoop stress at integration points of the innermost element layer,
    # evaluated against the analytic curve AT each ip's own radius (no nodal
    # extrapolation, no .frd parsing).
    stresses = _parse_dat_stresses(dat)
    assert stresses, "no stress block in .dat"
    conn = wall.grid.cells.reshape(-1, 9)[:, 1:]
    rel_errs = []
    for (eid, ip), s in stresses.items():
        cell_pts = pts[conn[eid - 1]]
        pos = _ip_positions(cell_pts)[ip - 1]
        r_ip = float(np.hypot(pos[0], pos[1]))
        th = np.arctan2(pos[1], pos[0])
        sxx, syy, _szz, sxy, _sxz, _syz = s
        sin, cos = np.sin(th), np.cos(th)
        sigma_theta = sxx * sin**2 + syy * cos**2 - 2 * sxy * sin * cos
        want = A + B / r_ip**2
        rel_errs.append(abs(sigma_theta - want) / abs(want))
    rel_errs = np.array(rel_errs)
    assert len(rel_errs) == 8 * len(np.unique(wall.boundary["inner"].cell_ids))
    # Tolerance justified by measured convergence (linear C3D8, radial stress
    # gradient): mean/max rel err 2.10%/3.57% @ 24x4 layers, 1.45%/2.53%
    # @ 32x6, 1.12%/1.81% @ 48x8 — first-order, so 2%/3.5% at 32x6 has
    # ~35% headroom while still failing on any ip-ordering or transform bug
    # (those produce ~10%+ errors).
    assert rel_errs.mean() < 0.02, rel_errs.mean()
    assert rel_errs.max() < 0.035, rel_errs.max()


def test_ccx_neo_hookean_small_strain_matches_linear_lame(tmp_path):
    """Consistency: neo-Hookean with mu = 2*C10 = E/(2(1+nu)), K = 2/D1 at a
    tiny pressure must reproduce the LINEAR Lamé displacement."""
    if _find_ccx() is None:
        pytest.skip("CalculiX (ccx) not available")

    wall = _lame_wall()
    E, nu, p = 1000.0, 0.3, 0.01  # strains ~1e-5: geometrically linear
    c10 = E / (4 * (1 + nu))
    d1 = 6 * (1 - 2 * nu) / E
    spec = DeckSpec(
        material=Material.neo_hookean(c10, d1, name="NEO"),
        step=Step(nlgeom=True, node_print={"inner": ("U",)}),
        boundary_conditions=(
            BoundaryCondition("inlet", 3),
            BoundaryCondition("outlet", 3),
            *lateral_pins(wall),
        ),
        loads=(Load.pressure("inner", p),),
        target="ccx",
    )
    deck = write_deck(wall, spec, tmp_path / "neo")
    dat = _run_ccx(deck)

    pts = np.asarray(wall.grid.points)
    inner_nodes = np.unique(wall.boundary["inner"].face_nodes)
    outer_nodes = np.unique(wall.boundary["outer"].face_nodes)
    r_node = np.hypot(pts[:, 0], pts[:, 1])
    a = float(r_node[inner_nodes].mean())
    b = float(r_node[outer_nodes].mean())
    want = (1 + nu) / E * p * a**2 / (b**2 - a**2) * ((1 - 2 * nu) * a + b**2 / a)

    u = _parse_dat_displacements(dat)
    got = float(
        np.mean(
            [
                (u[int(n) + 1][0] * pts[n, 0] + u[int(n) + 1][1] * pts[n, 1]) / r_node[n]
                for n in inner_nodes
            ]
        )
    )
    assert got == pytest.approx(want, rel=0.04), (got, want)

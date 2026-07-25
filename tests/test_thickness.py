from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pytest

from meshforge.geometry import ThicknessSpec, compute_centerline, offset_wall
from meshforge.meshing import get_backend
from meshforge.pipeline import MeshingSession
from meshforge.synth import generate

R_IN, R_OUT, LENGTH = 3.0, 3.55, 20.0


@pytest.fixture(scope="module")
def coterminal() -> tuple:
    """The hard case, verbatim from trap #2: the measured outer surface is
    CO-TERMINAL with the inner one (same length) — the caps coincide and
    nearest-point thickness collapses there. Do not lengthen it."""
    inner, _ = generate("straight_tube", radius=R_IN, length=LENGTH)
    outer, _ = generate("straight_tube", radius=R_OUT, length=LENGTH)
    return inner, outer


def test_coterminal_outer_guard_and_repair(coterminal) -> None:
    inner, outer = coterminal
    spec = ThicknessSpec.from_outer_surface(outer)
    n = inner.compute_normals(point_normals=True, cell_normals=False,
                              auto_orient_normals=True)
    t = spec.sample(np.asarray(n.points), normals=n["Normals"])

    true_t = R_OUT - R_IN
    assert any("repaired" in note for note in spec.notes)
    # Every collapsed cap sample must be repaired to a side-wall value.
    assert t.min() > 0.8 * true_t
    assert t.max() < 1.2 * true_t
    assert np.median(t) == pytest.approx(true_t, rel=0.05)


def test_measured_outer_wall_ingest(coterminal) -> None:
    inner, outer = coterminal
    spec = ThicknessSpec.from_outer_surface(outer)
    result = offset_wall(inner, thickness=None, thickness_spec=spec)

    assert result.outer is not inner
    assert result.outer.volume == pytest.approx(outer.volume, rel=1e-6)
    assert any("measured outer surface ingested" in n for n in result.notes)
    assert any("repaired" in n for n in result.notes)


def test_precedence_is_loud_everywhere(coterminal, tmp_path: Path) -> None:
    """Trap #5: a non-uniform wall must never silently fall back to a
    scalar — both/neither is an error in core, session, and CLI."""
    inner, outer = coterminal
    spec = ThicknessSpec.from_outer_surface(outer)

    with pytest.raises(ValueError, match="both a scalar thickness and a thickness field"):
        offset_wall(inner, 0.5, thickness_spec=spec)
    with pytest.raises(ValueError, match="no thickness given"):
        offset_wall(inner)

    inner_path, outer_path = tmp_path / "inner.vtp", tmp_path / "outer.vtp"
    inner.save(inner_path)
    outer.save(outer_path)

    s = MeshingSession()
    s.load(inner_path)
    with pytest.raises(ValueError, match="both a scalar thickness"):
        s.compute_wall(thickness=0.5, outer_surface=str(outer_path))

    from meshforge.cli import main

    rc = main(["volmesh", str(inner_path), "--thickness", "0.5",
               "--outer-surface", str(outer_path), "--outputs", str(tmp_path)])
    assert rc == 1
    rc = main(["volmesh", str(inner_path), "--outputs", str(tmp_path)])
    assert rc == 1


def test_session_measured_outer_end_to_end(coterminal, tmp_path: Path) -> None:
    inner, outer = coterminal
    inner_path, outer_path = tmp_path / "inner.vtp", tmp_path / "outer.vtp"
    inner.save(inner_path)
    outer.save(outer_path)

    s = MeshingSession()
    s.load(inner_path)
    result = s.compute_wall(outer_surface=str(outer_path))  # thickness omitted
    assert any("measured outer" in n for n in result.summary["notes"])
    assert result.summary["thickness_mean"] == pytest.approx(R_OUT - R_IN, rel=0.07)


def test_vertex_array_spec_warns_and_validates(coterminal) -> None:
    inner, _ = coterminal
    good = np.full(inner.n_points, 0.4)

    with pytest.warns(UserWarning, match="index-mapped"):
        spec = ThicknessSpec.from_vertex_array(good, inner)
    np.testing.assert_allclose(spec.sample(np.asarray(inner.points)), 0.4)

    with pytest.raises(ValueError, match="values for"):
        ThicknessSpec.from_vertex_array(good[:-3], inner)
    # Validation fires BEFORE the index-mapping warning: no warning here.
    with pytest.raises(ValueError, match="strictly positive"):
        ThicknessSpec.from_vertex_array(np.zeros(inner.n_points), inner)


def test_callable_field_drives_swept_lattice() -> None:
    surface, _ = generate("straight_tube")
    cl = compute_centerline(surface)
    spec = ThicknessSpec.from_callable(lambda p: 0.3 + 0.02 * p[:, 2])
    wall = get_backend("swept").mesh_wall(surface, spec, centerline=cl,
                                          n_theta=24, n_axial=40)

    assert wall.quality.n_inverted == 0
    assert any("thickness field (callable)" in n for n in wall.notes)

    z = np.asarray(wall.grid.points)[:, 2]
    z0, z1 = float(z.min()), float(z.max())
    zz = np.linspace(z0, z1, 2001)
    t = 0.3 + 0.02 * zz
    expected = float(np.trapezoid(math.pi * ((R_IN + t) ** 2 - R_IN**2), zz))
    assert wall.quality.total_volume == pytest.approx(expected, rel=0.03)


def test_field_error_branches(coterminal) -> None:
    inner, _ = coterminal
    with pytest.raises(ValueError, match="sdf mode needs a scalar"):
        offset_wall(inner, thickness=None, mode="sdf",
                    thickness_spec=ThicknessSpec.from_callable(lambda p: 0.3 + 0 * p[:, 2]))
    bad = ThicknessSpec.from_callable(lambda p: 0.0 * p[:, 2])
    with pytest.raises(ValueError, match="non-positive"):
        bad.sample(np.zeros((5, 3)))
    with pytest.raises(ValueError, match="watertight"):
        import pyvista as pv

        ThicknessSpec.from_outer_surface(pv.Plane().triangulate())

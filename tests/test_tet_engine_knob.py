"""P4 (D119): the ``tet_engine`` knob (default gmsh) from the Workbench, the
journal and the CLI down to ``fill_shells(engine=...)`` at the three sites
(lumen fill, thrombus fill, tet wall), and the rule that nothing silently
switches from TetGen to gmsh.

The TetGen branch of ``fill_shells`` is another worker's: these tests spy on
``fill_shells`` (and make it raise a TetGen RuntimeError) instead of running
TetGen. The gmsh calls the spy forwards are real.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("gmsh")
import pyvista as pv  # noqa: E402

from digitwin.cli import main  # noqa: E402
from digitwin.meshing import tetfill  # noqa: E402
from digitwin.meshing.lumen import fill_lumen  # noqa: E402
from digitwin.pipeline import STEP_KNOBS, MeshingSession, replay  # noqa: E402

_REAL_FILL = tetfill.fill_shells


class Spy:
    """Stands in for ``fill_shells``: records every call; gmsh calls run for
    real, TetGen calls raise a TetGen RuntimeError (never a gmsh fill)."""

    def __init__(self, monkeypatch):
        self.calls: list[dict] = []
        monkeypatch.setattr(tetfill, "fill_shells", self)

    def __call__(self, shells, size, holes=(), engine="gmsh", *, site, **opts):
        self.calls.append({"engine": engine, "site": site, "holes": list(holes), "size": size})
        if str(engine).lower() == "tetgen":
            raise RuntimeError("TetGen could not fill the shell (spy)")
        return _REAL_FILL(shells, size, holes, engine, site=site, **opts)

    @property
    def engines(self) -> list[str]:
        return [c["engine"] for c in self.calls]


@pytest.fixture()
def spy(monkeypatch) -> Spy:
    return Spy(monkeypatch)


def _tube_session() -> MeshingSession:
    s = MeshingSession()
    s.generate_synth("straight_tube")
    return s


def _journal_session(*steps) -> MeshingSession:
    """A session whose journal is hand-written (as an old .mfwork would be)."""
    s = MeshingSession()
    s.journal = [{"step": n, "params": dict(p), "summary": {}} for n, p in steps]
    return s


# --- the knob is declared once, per step ------------------------------------
@pytest.mark.parametrize("step", ["volmesh", "lumen", "aaa_stress"])
def test_knob_is_declared_with_default_gmsh_and_a_clean_help(step):
    typ, default, help_ = STEP_KNOBS[step]["tet_engine"]
    assert typ is str and default == "gmsh"
    assert "gmsh" in help_ and "tetgen" in help_
    assert not re.search(r"\(V\d", help_)  # no internal ids in user-facing text


def test_knob_is_in_the_workbench_schema_for_the_three_steps():
    from fastapi.testclient import TestClient

    from digitwin.web.app import create_app

    schema = TestClient(create_app()).get("/schema").json()
    for step in ("volmesh", "lumen", "aaa_stress"):
        assert schema[step]["tet_engine"]["default"] == "gmsh"
        assert schema[step]["tet_engine"]["type"] == "str"
    for step in set(schema) - {"volmesh", "lumen", "aaa_stress"}:
        assert "tet_engine" not in schema[step]


# --- replay: a journal written before the knob existed runs on gmsh ---------
def test_old_volmesh_journal_replays_on_gmsh(spy):
    s = _journal_session(
        ("generate_synth", {"shape": "straight_tube"}),
        ("volmesh", {"backend": "tet", "thickness": 0.5, "n_axial": 16}),
    )
    replay(s)
    assert spy.engines == ["gmsh"] and spy.calls[0]["site"] == "wall"


def test_old_lumen_journal_replays_on_gmsh(spy):
    s = _journal_session(
        ("generate_synth", {"shape": "straight_tube"}),
        ("lumen", {"method": "tet", "mesh_size": 3.0}),
    )
    (_, res) = replay(s)
    assert spy.engines == ["gmsh"] and spy.calls[0]["site"] == "lumen"
    assert res.summary["tet_engine"] == "gmsh"


def test_old_aaa_stress_journal_replays_on_gmsh(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)  # compute_aaa_stress writes ./outputs
    seen = {}

    class Fake:
        summary = {"fake": True}
        grid = None
        refused: list = []
        pws_mpa = None

        def to_text(self):
            return "fake"

    def fake_run(*a, **kw):
        seen.update(kw)
        return Fake()

    monkeypatch.setattr("digitwin.solve.aaa_metrics.run_aaa_stress", fake_run)
    s = _journal_session(
        ("generate_synth", {"shape": "straight_tube"}),
        ("volmesh", {"backend": "swept", "thickness": 0.5, "n_axial": 16}),
        ("aaa_stress", {"systolic_mmhg": 140.0, "whatif_mmhg": 120.0, "E": 100.0, "nu": 0.49,
                        "end_zone": "neck", "crop_report": "", "lumen": "", "sac": "",
                        "E_ilt": 5.0, "ccx": "", "timeout": 600.0}),
    )
    replay(s)
    assert seen["tet_engine"] == "gmsh"
    assert s.journal[-1]["params"]["tet_engine"] == "gmsh"  # and the new record carries it


# --- the journal records the engine only where it applies --------------------
def test_swept_volmesh_journal_has_no_tet_engine_even_with_the_form_defaults():
    s = _tube_session()
    knobs = {k: d for k, (_t, d, _h) in STEP_KNOBS["volmesh"].items() if d is not None}
    knobs.update(n_axial=16)
    s.compute_volmesh(**knobs)
    params = s.journal[-1]["params"]
    assert "tet_engine" not in params and params["backend"] == "swept"


def test_tet_volmesh_journal_and_summary_record_the_engine(spy):
    s = _tube_session()
    knobs = {k: d for k, (_t, d, _h) in STEP_KNOBS["volmesh"].items() if d is not None}
    knobs.update(backend="tet", n_axial=16, tet_engine="GMSH")  # any case
    res = s.compute_volmesh(**knobs)
    assert s.journal[-1]["params"]["tet_engine"] == "gmsh"
    assert "n_layers" not in s.journal[-1]["params"]  # swept-only defaults are not recorded
    assert res.summary["tet_engine"] == "gmsh"
    assert any(n == "tet engine: gmsh" for n in res.summary["notes"])
    assert spy.engines == ["gmsh"]


def test_swept_volmesh_refuses_a_tetgen_request_and_a_foreign_knob():
    s = _tube_session()
    with pytest.raises(ValueError, match="tet_engine is not a swept setting"):
        s.compute_volmesh(backend="swept", tet_engine="tetgen")
    with pytest.raises(ValueError, match="n_layers is not a tet setting"):
        s.compute_volmesh(backend="tet", n_layers=2)
    with pytest.raises(ValueError, match="mesh_size is not a swept setting"):
        s.compute_volmesh(backend="swept", mesh_size=1.0)
    assert s.volmesh is None and s.journal[-1]["step"] == "generate_synth"


# --- one validation, a ValueError that lists the choices ---------------------
@pytest.mark.parametrize("call", [
    lambda s: s.compute_volmesh(backend="tet", tet_engine="foo"),
    lambda s: s.compute_volmesh(backend="swept", tet_engine="foo"),
    lambda s: s.compute_lumen(method="tet", tet_engine="foo"),
    lambda s: s.compute_aaa_stress(tet_engine="foo"),
])
def test_a_bad_engine_is_a_value_error_naming_the_choices(call):
    s = _tube_session()
    with pytest.raises(ValueError, match=r"gmsh, tetgen"):
        call(s)


def test_a_bad_engine_is_http_400_on_the_three_steps():
    from fastapi.testclient import TestClient

    from digitwin.web.app import create_app

    c = TestClient(create_app())
    assert c.post("/source/synth", json={"shape": "straight_tube"}).status_code == 200
    for step, body in (("volmesh", {"backend": "tet"}), ("lumen", {"method": "tet"}), ("aaa_stress", {})):
        r = c.post(f"/step/{step}", json={**body, "tet_engine": "foo"})
        assert r.status_code == 400, (step, r.text)
        assert "gmsh, tetgen" in r.text


# --- TetGen never silently becomes gmsh --------------------------------------
@pytest.mark.parametrize("method", ["auto", "surface-tet", "ogrid"])
def test_tetgen_needs_method_tet_and_never_reaches_a_fill(spy, method):
    surf = pv.Sphere(radius=5.0)
    with pytest.raises(ValueError, match="requires method='tet'"):
        fill_lumen(surf, method=method, tet_engine="tetgen")
    with pytest.raises(ValueError, match="requires method='tet'"):
        fill_lumen(surf, method=method, tet_engine="TetGen")  # any case
    assert spy.calls == []


def test_tetgen_lumen_fill_failure_is_one_loud_tetgen_error_and_no_gmsh(spy):
    s = _tube_session()
    with pytest.raises(RuntimeError, match="TetGen"):
        s.compute_lumen(method="tet", tet_engine="tetgen")
    assert spy.engines == ["tetgen"]
    assert s.volmesh is None  # nothing was filled
    assert all(r["step"] != "lumen" for r in s.journal)


def test_tetgen_wall_fill_failure_is_one_loud_tetgen_error_and_no_gmsh(spy):
    s = _tube_session()
    with pytest.raises(RuntimeError, match="TetGen"):
        s.compute_volmesh(backend="tet", n_axial=16, tet_engine="tetgen")
    assert spy.engines == ["tetgen"] and spy.calls[0]["site"] == "wall"


def test_a_tetgen_error_that_does_not_name_tetgen_is_renamed(monkeypatch):
    def quiet(shells, size, holes=(), engine="gmsh", *, site, **opts):
        raise RuntimeError("PLC check failed")

    monkeypatch.setattr(tetfill, "fill_shells", quiet)
    with pytest.raises(RuntimeError, match=r"TetGen lumen fill failed: PLC check failed"):
        _tube_session().compute_lumen(method="tet", tet_engine="tetgen")


def test_build_lumen_case_with_tetgen_never_calls_a_gmsh_fill(spy, tmp_path):
    from digitwin.copilot import flow

    src = tmp_path / "crop_lumen.vtp"
    _tube_session().surface.save(src)
    with pytest.raises(RuntimeError, match="TetGen") as err:
        flow.build_lumen_case(src, tmp_path / "case", tet_engine="tetgen")
    assert "no fallback to gmsh" in str(err.value)
    assert spy.engines == ["tetgen"]  # one attempt: no tet -> auto -> surface-tet ladder
    assert not (tmp_path / "case").exists()


def test_build_lumen_case_default_engine_is_gmsh_with_its_ladder_unchanged(spy, tmp_path):
    from digitwin.copilot import flow

    src = tmp_path / "crop_lumen.vtp"
    _tube_session().surface.save(src)
    built = flow.build_lumen_case(src, tmp_path / "case")
    assert built["tet_engine"] == "gmsh" and built["n_cells"] > 0
    assert set(spy.engines) == {"gmsh"}
    # a mesh_size reaches the fill
    flow.build_lumen_case(src, tmp_path / "case2", mesh_size=4.0)
    assert spy.calls[-1]["size"] == 4.0


# --- the thrombus site --------------------------------------------------------
def _nested_spheres():
    return pv.Sphere(radius=10.0, theta_resolution=24, phi_resolution=24), \
        pv.Sphere(radius=5.0, theta_resolution=24, phi_resolution=24)


def test_thrombus_gmsh_fill_still_goes_through_the_guarded_child(monkeypatch):
    from digitwin.solve import ilt

    outer, inner = _nested_spheres()
    seen = {}

    def fake_child(job, what, *, size, **arrays):
        seen["job"] = job
        return {"points": np.zeros((4, 3)), "tets": np.array([[0, 1, 2, 3]])}

    monkeypatch.setattr(ilt, "_run_gmsh", fake_child)
    points, tets = ilt._fill_between(outer, inner, 3.0)  # default engine
    assert seen["job"] == "fill" and tets.shape == (1, 4)


def test_thrombus_tetgen_fill_calls_fill_shells_directly_with_a_hole_inside_the_lumen(monkeypatch):
    from digitwin.solve import ilt

    outer, inner = _nested_spheres()
    calls = []

    def fake_fill(shells, size, holes=(), engine="gmsh", *, site, **opts):
        calls.append((engine, site, [np.asarray(h) for h in holes], len(shells)))
        return tetfill.TetFill(points=np.zeros((4, 3)), tets=np.array([[0, 1, 2, 3]]))

    monkeypatch.setattr(tetfill, "fill_shells", fake_fill)
    monkeypatch.setattr(ilt, "_run_gmsh", lambda *a, **k: pytest.fail("the gmsh child must not run"))
    ilt._fill_between(outer, inner, 3.0, "tetgen")
    (engine, site, holes, n_shells), = calls
    assert (engine, site, n_shells) == ("tetgen", "thrombus", 2)
    assert len(holes) == 1 and np.linalg.norm(holes[0]) < 4.0  # inside the radius-5 lumen


def test_thrombus_tetgen_failure_names_tetgen_and_is_not_retried_with_gmsh(monkeypatch):
    from digitwin.solve import ilt

    outer, inner = _nested_spheres()

    def quiet(*a, **k):
        raise RuntimeError("PLC check failed")

    monkeypatch.setattr(tetfill, "fill_shells", quiet)
    monkeypatch.setattr(ilt, "_run_gmsh", lambda *a, **k: pytest.fail("no gmsh fill after TetGen"))
    with pytest.raises(RuntimeError, match=r"TetGen thrombus fill failed"):
        ilt._fill_between(outer, inner, 3.0, "tetgen")


def test_build_ilt_mesh_rejects_a_bad_engine_and_records_the_engine_in_its_summary():
    from digitwin.solve import ilt

    with pytest.raises(ValueError, match="gmsh, tetgen"):
        ilt.build_ilt_mesh(None, None, [], tet_engine="foo")
    mesh = ilt.IltMesh(points=np.zeros((4, 3)), tets=np.zeros((1, 10), dtype=int), inner_faces=[],
                       outer_faces=[], volume_ml=1.0, expected_volume_ml=1.0, sj_min=0.5)
    assert mesh.summary()["tet_engine"] == "gmsh"  # the default


def test_run_aaa_stress_validates_the_engine_first_even_without_a_thrombus():
    from digitwin.solve.aaa_metrics import run_aaa_stress

    with pytest.raises(ValueError, match="gmsh, tetgen"):
        run_aaa_stress(None, None, out_dir="x", tet_engine="nope")


def test_aaa_stress_step_passes_the_normalised_engine_down_and_records_it(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    seen = {}

    class Fake:
        summary = {"thrombus": {"modelled": False}}
        grid = None
        refused: list = []

        def to_text(self):
            return "fake"

    monkeypatch.setattr("digitwin.solve.aaa_metrics.run_aaa_stress",
                        lambda *a, **kw: seen.update(kw) or Fake())
    s = _tube_session()
    s.compute_volmesh(backend="swept", n_axial=16)
    s.compute_aaa_stress(tet_engine="TetGen")
    assert seen["tet_engine"] == "tetgen"
    assert s.journal[-1]["params"]["tet_engine"] == "tetgen"


# --- the lumen result records its engine ---------------------------------------
def test_gmsh_lumen_notes_and_summary_name_the_engine():
    s = _tube_session()
    res = s.compute_lumen(method="tet", mesh_size=3.0)
    assert res.summary["tet_engine"] == "gmsh"
    assert "tet engine: gmsh" in res.summary["notes"]
    assert "tet engine: gmsh" in res.text
    og = _tube_session().compute_lumen(method="ogrid", n_axial=10, n_theta=16)
    assert "tet_engine" not in og.summary  # no tet fill, no engine


# --- the CLI -------------------------------------------------------------------
@pytest.fixture()
def tube(tmp_path) -> Path:
    path = tmp_path / "tube.vtp"
    _tube_session().surface.save(path)
    return path


def test_cli_lumen_tet_engine_flag_reaches_the_fill(spy, tube, tmp_path, capsys):
    rc = main(["lumen", str(tube), "--method", "tet", "--tet-engine", "TetGen",
               "--outputs", str(tmp_path / "o")])
    assert rc == 1 and spy.engines == ["tetgen"]
    assert "TetGen" in capsys.readouterr().err


def test_cli_lumen_tetgen_with_auto_is_refused_before_any_fill(spy, tube, tmp_path, capsys):
    rc = main(["lumen", str(tube), "--tet-engine", "tetgen", "--outputs", str(tmp_path / "o")])
    assert rc == 1 and spy.calls == []
    assert "requires method='tet'" in capsys.readouterr().err


def test_cli_lumen_default_engine_is_gmsh_and_is_in_the_summary(spy, tube, tmp_path):
    out = tmp_path / "o" / "lumen.vtu"
    rc = main(["lumen", str(tube), "--method", "tet", "--mesh-size", "3", "--out", str(out)])
    assert rc == 0 and spy.engines == ["gmsh"]
    assert json.loads(out.with_suffix(".summary.json").read_text())["tet_engine"] == "gmsh"


def test_cli_volmesh_tet_engine_flag_reaches_the_wall_fill(spy, tube, tmp_path, capsys):
    rc = main(["volmesh", str(tube), "--backend", "tet", "--thickness", "0.5", "--n-axial", "16",
               "--tet-engine", "tetgen", "--outputs", str(tmp_path / "o")])
    assert rc == 1 and spy.engines == ["tetgen"] and spy.calls[0]["site"] == "wall"
    assert "TetGen" in capsys.readouterr().err


def test_cli_volmesh_tet_default_engine_is_gmsh_and_is_in_the_summary(spy, tube, tmp_path):
    out = tmp_path / "o" / "wall.vtu"
    rc = main(["volmesh", str(tube), "--backend", "tet", "--thickness", "0.5", "--n-axial", "16",
               "--out", str(out)])
    assert rc == 0 and spy.engines == ["gmsh"]
    assert json.loads(out.with_suffix(".summary.json").read_text())["tet_engine"] == "gmsh"


def test_cli_volmesh_swept_with_tetgen_is_an_error(spy, tube, tmp_path, capsys):
    rc = main(["volmesh", str(tube), "--thickness", "0.5", "--tet-engine", "tetgen",
               "--outputs", str(tmp_path / "o")])
    assert rc == 1 and spy.calls == []
    assert "--tet-engine is tet-only" in capsys.readouterr().err
    # the default engine on the swept backend is fine
    assert main(["volmesh", str(tube), "--thickness", "0.5", "--n-axial", "16", "--tet-engine",
                 "gmsh", "--outputs", str(tmp_path / "o")]) == 0


def test_cli_tet_engine_rejects_an_unknown_choice(tube, tmp_path):
    for argv in (["lumen", str(tube)], ["volmesh", str(tube), "--thickness", "0.5"],
                 ["aaa-stress", str(tube)]):
        with pytest.raises(SystemExit) as exc:
            main([*argv, "--tet-engine", "foam"])
        assert exc.value.code == 2


def test_cli_aaa_stress_tet_engine_flag_reaches_run_aaa_stress(monkeypatch, tmp_path):
    s = _tube_session()
    s.compute_volmesh(backend="swept", thickness=0.5, n_axial=16)
    wall = tmp_path / "wall.vtu"
    s.volmesh.grid.save(wall)
    wall.with_suffix(".summary.json").write_text(json.dumps(s.volmesh.summary()))
    seen = {}

    def stop(*a, **kw):
        seen.update(kw)
        raise RuntimeError("stop here")

    monkeypatch.setattr("digitwin.solve.aaa_metrics.run_aaa_stress", stop)
    assert main(["aaa-stress", str(wall), "--tet-engine", "tetgen", "--outputs", str(tmp_path / "o")]) == 1
    assert seen["tet_engine"] == "tetgen"
    seen.clear()
    assert main(["aaa-stress", str(wall), "--outputs", str(tmp_path / "o")]) == 1
    assert seen["tet_engine"] == "gmsh"

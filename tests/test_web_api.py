"""Headless HTTP tests for the V35 web layer (increment 1, backend only).

No browser: everything drives the FastAPI app through ``TestClient``. The
last test is the rule-4 guard — a static scan proving the web package holds
no meshing/geometry/solve logic and only calls ``MeshingSession``.
"""
from __future__ import annotations

import inspect
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from meshforge.pipeline import STEP_KNOBS  # noqa: E402
from meshforge.web.app import create_app  # noqa: E402


@pytest.fixture()
def client() -> TestClient:
    # A fresh app => a fresh in-process MeshingSession per test.
    return TestClient(create_app())


def _drive_to_volmesh(client: TestClient) -> None:
    assert client.post("/source/synth", json={"shape": "straight_tube"}).status_code == 200
    assert client.post("/step/centerline", json={}).status_code == 200
    assert client.post("/step/sections", json={}).status_code == 200
    assert client.post("/step/wall", json={}).status_code == 200


def test_full_pipeline_over_http(client: TestClient) -> None:
    _drive_to_volmesh(client)
    r = client.post("/step/volmesh", json={"n_axial": 30})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["step"] == "volmesh"
    # Clean synthetic geometry: zero inverted cells (definition of done).
    assert body["summary"]["quality"]["n_inverted"] == 0
    assert body["summary"]["n_cells"] > 0
    assert "volmesh[swept]" in body["text"]


def test_journal_mirrors_calls(client: TestClient) -> None:
    _drive_to_volmesh(client)
    client.post("/step/volmesh", json={"n_axial": 30})
    steps = [rec["step"] for rec in client.get("/journal").json()["journal"]]
    assert steps == ["generate_synth", "centerline", "sections", "wall", "volmesh"]
    state = client.get("/state").json()
    assert state["artifacts"]["volmesh"] is True
    assert state["journal_len"] == 5


def test_schema_covers_every_step(client: TestClient) -> None:
    schema = client.get("/schema").json()
    assert set(schema) == set(STEP_KNOBS)
    # The schema is the form contract: type/default/help per knob.
    thickness = schema["wall"]["thickness"]
    assert thickness["type"] == "float"
    assert thickness["default"] == 0.5
    assert thickness["help"]  # non-empty help text
    # A "compute-a-default" knob advertises a null default.
    assert schema["centerline"]["voxel_size"]["default"] is None


def test_health_lists_steps(client: TestClient) -> None:
    body = client.get("/health").json()
    assert body["ok"] is True
    assert set(body["steps"]) == set(STEP_KNOBS)


def test_missing_prerequisite_is_loud_400(client: TestClient) -> None:
    r = client.post("/step/centerline", json={})  # no surface yet
    assert r.status_code == 400
    # Same message the CLI/GUI print (fail-loud, rule 6).
    assert "no surface available" in r.json()["detail"]
    assert "generate_synth" in r.json()["detail"]


def test_bad_parameter_type_is_422(client: TestClient) -> None:
    client.post("/source/synth", json={"shape": "straight_tube"})
    r = client.post("/step/centerline", json={"n_samples": "not_a_number"})
    assert r.status_code == 422


def test_unknown_knob_is_422(client: TestClient) -> None:
    client.post("/source/synth", json={"shape": "straight_tube"})
    r = client.post("/step/centerline", json={"bogus_knob": 1})
    assert r.status_code == 422


def test_unset_knob_uses_step_knobs_default(client: TestClient) -> None:
    # wall.thickness STEP_KNOBS default is 0.5 but compute_wall's Python
    # default is None; the web layer must send 0.5 (GUI parity), not omit it.
    client.post("/source/synth", json={"shape": "straight_tube"})
    client.post("/step/centerline", json={})
    r = client.post("/step/wall", json={})  # thickness unset -> 0.5
    assert r.status_code == 200, r.text
    s = r.json()["summary"]
    assert 0.4 < s["thickness_max"] < 0.6


def test_workfile_save_reset_load_roundtrip(client: TestClient, tmp_path: Path) -> None:
    _drive_to_volmesh(client)
    client.post("/step/volmesh", json={"n_axial": 30})

    out = tmp_path / "session.mfwork"
    r = client.post("/workfile/save", json={"path": str(out)})
    assert r.status_code == 200, r.text
    assert Path(r.json()["path"]).is_file()

    # Reset wipes state.
    client.post("/session/reset")
    assert client.get("/state").json()["artifacts"]["volmesh"] is False

    # Load replays the journal into a fresh session.
    r = client.post("/workfile/load", json={"path": str(out)})
    assert r.status_code == 200, r.text
    replayed = r.json()
    assert [rec["step"] for rec in replayed["journal"]] == [
        "generate_synth",
        "centerline",
        "sections",
        "wall",
        "volmesh",
    ]
    vol = [res for res in replayed["results"] if res["step"] == "volmesh"][0]
    assert vol["summary"]["quality"]["n_inverted"] == 0
    assert client.get("/state").json()["artifacts"]["volmesh"] is True


def test_workfile_save_empty_journal_is_loud_400(client: TestClient, tmp_path: Path) -> None:
    r = client.post("/workfile/save", json={"path": str(tmp_path / "empty.mfwork")})
    assert r.status_code == 400
    assert "empty" in r.json()["detail"].lower()


def test_write_path_guard_refuses_overwriting_input(client: TestClient, tmp_path: Path) -> None:
    # The screenshot path is client-supplied -> it must reuse guard_output_path.
    # (The guard fires before any render, so this stays fast/headless.)
    import pyvista as pv

    surf = tmp_path / "in.stl"
    pv.Sphere().save(str(surf))
    assert client.post("/source/load", json={"path": str(surf)}).status_code == 200

    r = client.post("/step/screenshot", json={"path": str(surf)})
    assert r.status_code == 400
    assert "refusing to overwrite input file" in r.json()["detail"]


# ---- increment 2: the Workbench frontend -------------------------------


def _static_dir() -> Path:
    import meshforge.web as web_pkg

    return Path(web_pkg.__file__).parent / "static"


def test_catalog_lists_synth_shapes(client: TestClient) -> None:
    # The frontend's shape dropdown source: the web layer can't import synth
    # (rule 4), so it exposes the catalog by asking the session.
    from meshforge.synth import GENERATORS

    body = client.get("/catalog").json()
    assert body["shapes"] == sorted(GENERATORS)
    assert "straight_tube" in body["shapes"]


def test_workbench_served_not_placeholder(client: TestClient) -> None:
    # GET /app/ now serves the real Workbench, not the increment-1 placeholder.
    r = client.get("/app/")
    assert r.status_code == 200, r.text
    html = r.text
    assert "this is a placeholder" not in html.lower()
    assert 'src="app.js"' in html
    # The 3-pane structure is present.
    for marker in ("pane-left", "pane-center", "pane-right", "step-panels", "history"):
        assert marker in html, marker


def test_help_affordance_is_static_and_local(client: TestClient) -> None:
    # V36: the in-app quick-start popover is present, and the whole page stays
    # self-contained — 127.0.0.1-only, no external navigation/fetch (D53 posture).
    html = client.get("/app/").text
    assert 'id="help"' in html and "Quick start" in html
    assert "TUTORIAL.md" in html and "APP_GUIDE.md" in html
    # No external scheme anywhere in the page (no CDN/font/link the app fetches).
    assert "http://" not in html and "https://" not in html


def test_frontend_endpoints_all_exist_on_app() -> None:
    """Every endpoint the frontend calls is a real route (catches a typo'd
    path without a browser), and the frontend leaks no meshing logic."""
    js = (_static_dir() / "app.js").read_text()
    route_paths = {r.path for r in create_app().routes}
    for endpoint in (
        "/schema",
        "/catalog",
        "/journal",
        "/state",
        "/source/synth",
        "/source/load",
        "/workfile/save",
        "/workfile/load",
        "/session/reset",
    ):
        assert endpoint in js, f"frontend never calls {endpoint}"
        assert endpoint in route_paths, f"{endpoint} is not a real route"
    # The per-step family is dynamic (/step/{step}).
    assert "/step/" in js
    assert any(p.startswith("/step/") for p in route_paths)
    # Rule 4, mirrored for the static frontend: no meshing internals.
    for forbidden in ("meshforge.geometry", "meshforge.meshing", "offset_wall"):
        assert forbidden not in js


def test_web_layer_holds_no_meshing_logic() -> None:
    """Rule 4: src/meshforge/web/ imports no meshing/geometry/solve internals;
    it only calls MeshingSession (+ the shared guard_output_path, sanctioned).
    AST-based (V43): only real imports/name references count — a token in a
    docstring or comment no longer trips the scan (the scanner itself is
    exercised in test_rule4_scan.py)."""
    from rule4_scan import assert_holds_no_meshing_logic

    import meshforge.web as web_pkg

    web_dir = Path(web_pkg.__file__).parent
    py_files = sorted(web_dir.glob("*.py"))
    assert py_files, "no web modules found to scan"
    assert_holds_no_meshing_logic(*py_files)
    combined = "".join(path.read_text() for path in py_files)
    # Positive contract: the compute entrypoint is MeshingSession, and the
    # one sanctioned export touchpoint is the shared output guard.
    assert "MeshingSession" in combined
    assert "guard_output_path" in combined


# ---- increment 3: the 3-D viewport geometry endpoint -------------------


def test_geometry_empty_before_source(client: TestClient) -> None:
    # Nothing rendered yet -> a loud, empty 204 (never a 500).
    r = client.get("/geometry")
    assert r.status_code == 204
    assert r.content == b""


def test_geometry_after_source(client: TestClient) -> None:
    assert client.post("/source/synth", json={"shape": "straight_tube"}).status_code == 200
    body = client.get("/geometry").json()
    assert body["kind"] == "surface"
    assert body["n_frames"] == 0
    # The serialized vtk.js mesh state is present and well-formed.
    assert isinstance(body["state"], dict) and "mesh" in body["state"]


def test_geometry_volmesh_has_region_field(client: TestClient) -> None:
    _drive_to_volmesh(client)
    assert client.post("/step/volmesh", json={"n_axial": 30}).status_code == 200
    body = client.get("/geometry").json()
    assert body["kind"] == "volume"
    # extract_surface exposes the cell field the volmesh carries.
    assert "region" in body["fields"]
    assert body["field"] == "region"  # server picks the first field by default
    assert body["data_range"] and len(body["data_range"]) == 2


def test_geometry_unknown_field_is_loud_400(client: TestClient) -> None:
    assert client.post("/source/synth", json={"shape": "straight_tube"}).status_code == 200
    _drive_to_volmesh(client)
    client.post("/step/volmesh", json={"n_axial": 30})
    r = client.get("/geometry", params={"field": "not_a_field"})
    assert r.status_code == 400
    assert "unknown field" in r.json()["detail"]


def test_geometry_frame_endpoint_bounds() -> None:
    # Inject a tiny synthetic 2-frame series (no OpenFOAM solve in CI): the
    # frame endpoint's index handling is what we exercise, headless.
    import pyvista as pv

    app = create_app()
    client = TestClient(app)
    m0 = pv.Sphere()
    m0["f"] = m0.points[:, 0]
    m1 = pv.Sphere()
    m1["f"] = m1.points[:, 1]
    app.state.holder.last_render = {
        "mesh": m0,
        "kind": "surface",
        "label": "cfd",
        "frames": [(0.0, m0), (1.0, m1)],
    }
    meta = client.get("/geometry").json()
    assert meta["n_frames"] == 2 and meta["times"] == [0.0, 1.0]

    f0 = client.get("/geometry/frame/0").json()
    assert f0["index"] == 0 and f0["time"] == 0.0 and "mesh" in f0["state"]
    assert client.get("/geometry/frame/1").status_code == 200
    # out of range -> loud 400, no IndexError/traceback leak.
    over = client.get("/geometry/frame/5")
    assert over.status_code == 400 and "out of range" in over.json()["detail"]


def test_geometry_frame_404_without_series(client: TestClient) -> None:
    assert client.post("/source/synth", json={"shape": "straight_tube"}).status_code == 200
    r = client.get("/geometry/frame/0")
    assert r.status_code == 404


def test_vendor_assets_served_and_locked(client: TestClient) -> None:
    # The two viewport vendor files are served read-only from the installed
    # trame packages; any other name is refused (the dict lookup makes path
    # traversal structurally impossible).
    for name in ("vue.global.js", "trame-vtk.js"):
        r = client.get(f"/vendor/{name}")
        assert r.status_code == 200, name
        assert "javascript" in r.headers["content-type"]
        assert len(r.content) > 0
    assert client.get("/vendor/bogus.js").status_code == 404
    assert client.get("/vendor/app.py").status_code == 404


def test_viewport_module_is_rule4_clean() -> None:
    # The display module is scanned by the rule-4 guard (globs web/*.py); assert
    # it exists and calls the serializer, not any meshing internal.
    import meshforge.web as web_pkg

    vp = Path(web_pkg.__file__).parent / "viewport.py"
    assert vp.is_file(), "web/viewport.py missing"
    src = vp.read_text()
    assert "vtk_mesh" in src  # the display serializer it delegates to
    for token in ("offset_wall", "mesh_wall(", "get_backend", "meshforge.synth"):
        assert token not in src


def test_frontend_viewport_wiring() -> None:
    # The Workbench JS drives the geometry endpoint and the viewport glue holds
    # no meshing logic (rule-4 mirror for the new static files).
    app_js = (_static_dir() / "app.js").read_text()
    assert "/geometry" in app_js
    assert "/geometry" in {r.path for r in create_app().routes} or any(
        r.path.startswith("/geometry") for r in create_app().routes
    )
    vp_js = (_static_dir() / "viewport.js").read_text()
    for forbidden in ("meshforge.geometry", "meshforge.meshing", "offset_wall"):
        assert forbidden not in vp_js


def test_frontend_browse_button_wiring() -> None:
    """Task 2: the native 'Browse…' affordance is wired to the desktop bridge
    and hides itself when no bridge is present (plain browser / headless). It
    only moves a string — no backend contract change (rule 4)."""
    js = (_static_dir() / "app.js").read_text()
    # gated on the QWebChannel transport the shell injects
    assert "window.qt" in js and "webChannelTransport" in js
    assert "window.meshforgeBridge" in js
    # calls the two bridge slots the shell exposes
    assert "open_file" in js and "open_directory" in js
    # buttons hide without a bridge
    assert "hidden = !bridgeAvailable()" in js
    # attached to the source path field and import_* path knobs
    assert "addBrowseButton" in js
    assert 'step.startsWith("import_")' in js
    # still no meshing logic and no new fetch endpoint smuggled in
    for forbidden in ("meshforge.geometry", "meshforge.meshing", "offset_wall"):
        assert forbidden not in js


# ---- session-34 item 1: contour QC overlay + auto-refresh ----------------


def test_geometry_overlay_after_import_cso(client: TestClient, tmp_path: Path) -> None:
    """import_cso's raw parsed rings ride the /geometry payload as a QC
    overlay (a display passthrough); a non-CSO artifact carries none."""
    from test_io_cso import _straight_tube_cso

    cso = _straight_tube_cso(tmp_path / "tube.cso")
    # method="loft" keeps the heavy auto trial-sweep out of TestClient's
    # worker thread (the cascade is covered in test_io_cso.py); the overlay
    # contract is identical for every method.
    r = client.post("/step/import_cso", json={"path": str(cso), "method": "loft"})
    assert r.status_code == 200, r.text
    body = client.get("/geometry").json()
    overlay = body.get("overlay")
    assert overlay, "import_cso geometry payload must carry the QC overlay"
    assert isinstance(overlay["state"], dict) and "mesh" in overlay["state"]
    assert "contour" in overlay["label"]
    # a synth source has no overlay (and none leaks from the previous artifact)
    assert client.post("/source/synth", json={"shape": "straight_tube"}).status_code == 200
    assert "overlay" not in client.get("/geometry").json()


def test_state_rev_bumps_on_every_session_change(client: TestClient, tmp_path: Path) -> None:
    """The monotonic /state rev is the frontend's auto-refresh signal: every
    state-changing call bumps it; read-only calls (and a work-file SAVE,
    which mutates nothing) do not."""
    r0 = client.get("/state").json()["rev"]
    assert client.post("/source/synth", json={"shape": "straight_tube"}).status_code == 200
    r1 = client.get("/state").json()["rev"]
    assert r1 > r0
    assert client.post("/step/centerline", json={}).status_code == 200
    r2 = client.get("/state").json()["rev"]
    assert r2 > r1
    # reads do not bump
    client.get("/journal")
    client.get("/geometry")
    assert client.get("/state").json()["rev"] == r2
    # a work-file save mutates nothing -> no bump
    wf = tmp_path / "s.mfwork"
    assert client.post("/workfile/save", json={"path": str(wf)}).status_code == 200
    assert client.get("/state").json()["rev"] == r2
    # reset and work-file load both swap the session -> both bump
    assert client.post("/session/reset", json={}).status_code == 200
    r3 = client.get("/state").json()["rev"]
    assert r3 > r2
    assert client.post(
        "/workfile/load", json={"path": str(wf), "replay": True}
    ).status_code == 200
    assert client.get("/state").json()["rev"] > r3


def test_frontend_autorefresh_and_overlay_wiring() -> None:
    """Session-34 item 1: the Workbench polls the session revision and paints
    the QC overlay client-side; the glue stays meshing-free (rule-4 mirror)."""
    app_js = (_static_dir() / "app.js").read_text()
    assert "startAutoRefresh" in app_js
    assert 'apiGet("/state")' in app_js
    assert "setupOverlay" in app_js
    vp_js = (_static_dir() / "viewport.js").read_text()
    assert "setOverlay" in vp_js
    html = (_static_dir() / "index.html").read_text()
    assert "vp-overlay" in html
    for js in (app_js, vp_js):
        for forbidden in ("meshforge.geometry", "meshforge.meshing", "offset_wall"):
            assert forbidden not in js


# ---- session 39: a fresh clone must be told what to install ---------------


def _block_imports(monkeypatch, *prefixes):
    """Make `import <prefix>[...]` raise ImportError, as it does on a machine
    where only the BASE deps are installed."""
    import builtins

    real = builtins.__import__

    def fake(name, *a, **k):
        if any(name == p or name.startswith(p + ".") for p in prefixes):
            raise ImportError(f"no module named {name}")
        return real(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake)


def test_serve_without_the_web_extra_names_the_fix(monkeypatch) -> None:
    """`conda env create -f environment.yml` installs the base deps only, and
    `-e .` pulls no extras — so a fresh clone reaches serve() with no FastAPI.
    That must name the install, not raise a bare ModuleNotFoundError."""
    import sys

    from meshforge.web import server

    for mod in [m for m in sys.modules if m.startswith(("uvicorn", "fastapi"))]:
        monkeypatch.delitem(sys.modules, mod, raising=False)
    monkeypatch.delitem(sys.modules, "meshforge.web.app", raising=False)
    _block_imports(monkeypatch, "uvicorn", "fastapi")

    with pytest.raises(RuntimeError) as exc:
        server.serve(port=1)
    msg = str(exc.value)
    assert "[web] extra" in msg
    assert "pip install -e '.[web]'" in msg
    assert "trame-vtk" in msg  # the viewport is a separate, conda-forge install
    assert isinstance(exc.value.__cause__, ImportError)  # chained, not swallowed

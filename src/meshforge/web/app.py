"""FastAPI app factory for the local meshforge web layer (V35, increment 1).

Exposes ``MeshingSession`` 1:1 as JSON. **Rule 4: zero meshing/solve/export-
writer logic here.** Every route only calls a ``MeshingSession`` step (or
``load_workfile``/``replay``/``save_workfile``) and returns its ``StepResult``/
journal as JSON. Domain errors from the compute layer surface as loud HTTP 400
carrying the same message the CLI prints (fail-loud, rule 6). The one shared
I/O primitive reused here is ``guard_output_path`` (the exact guard every
exporter calls) on client-supplied write paths.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from meshforge.pipeline import STEP_KNOBS, MeshingSession
from meshforge.web.schema import build_request_models, drop_unset, schema_json

# Compute-layer errors we translate into a loud HTTP 400. Anything else is a
# genuine bug and is allowed to become a 500 (also loud).
_DOMAIN_ERRORS = (
    ValueError,
    RuntimeError,
    FileNotFoundError,
    KeyError,
    OSError,
    NotImplementedError,
)


class _SessionHolder:
    """Mutable box for the single in-process session so reset / workfile-load
    can swap the whole ``MeshingSession`` atomically. ``last_render`` caches the
    most recent step's ``render_hints`` (the pyvista mesh) so the geometry
    endpoint can serve the *current* artifact to the viewport."""

    def __init__(self) -> None:
        self.session = MeshingSession()
        self.last_render: dict[str, Any] | None = None
        # Monotonic session revision. Bumped on every state-changing call so a
        # frontend can cheaply poll GET /state and re-sync when ANOTHER client
        # (or a replay) changed the shared in-process session (auto-refresh).
        self.rev = 0


class SynthBody(BaseModel):
    shape: str
    params: dict[str, Any] = {}


class PathBody(BaseModel):
    path: str


class WorkfileLoadBody(BaseModel):
    path: str
    replay: bool = True


def _result_json(result) -> dict[str, Any]:
    """StepResult -> JSON. ``render_hints`` (the pyvista meshes) are **omitted**
    from the step body on purpose — the mesh travels to the viewport over the
    separate binary ``GET /geometry`` endpoint, not in every step response."""
    return {"step": result.step, "summary": result.summary, "text": result.text}


def _capture(holder: "_SessionHolder", result) -> dict[str, Any]:
    """Stash the step's ``render_hints`` (the already-computed pyvista mesh) on
    the holder so ``GET /geometry`` can serve the current artifact, then return
    the ordinary JSON body (which still omits ``render_hints``). No meshing
    logic — it only carries a mesh the session already produced (rule 4)."""
    hints = getattr(result, "render_hints", None)
    if hints and hints.get("mesh") is not None:
        holder.last_render = hints
    holder.rev += 1
    return _result_json(result)


def _run(work):
    """Run ``work`` mapping compute-layer domain errors to a loud HTTP 400."""
    try:
        return work()
    except HTTPException:
        raise
    except _DOMAIN_ERRORS as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _guard_write(session: MeshingSession, path: str | Path) -> None:
    """Reuse the exporters' output guard on a client-supplied write path
    (shared safety primitive, not meshing logic — rule 4 intact)."""
    from meshforge.export import guard_output_path

    guard_output_path(path, input_paths=[session.input_path])


def _vendor_assets() -> dict[str, Path]:
    """Map the viewport's vendored JS filenames -> their on-disk paths in the
    installed trame packages (single source of truth; nothing committed to the
    repo). Empty if trame-vtk isn't installed, in which case the viewport
    degrades to an 'install the viewport extra' note in the frontend."""
    assets: dict[str, Path] = {}
    try:
        import trame_client
        import trame_vtk

        candidates = {
            "trame-vtk.js": Path(trame_vtk.__file__).parent
            / "modules" / "common" / "serve" / "trame-vtk.js",
            "vue.global.js": Path(trame_client.__file__).parent
            / "module" / "vue3-www" / "vue.global.js",
        }
        for name, path in candidates.items():
            if path.is_file():
                assets[name] = path
    except Exception:
        pass
    return assets


def create_app() -> FastAPI:
    app = FastAPI(
        title="meshforge web",
        version="0.1.0",
        description="Local JSON layer over MeshingSession (127.0.0.1 only).",
    )
    holder = _SessionHolder()
    app.state.holder = holder  # tests reach the live session through this
    models = build_request_models()

    # -- sources (not in STEP_KNOBS) -------------------------------------
    @app.post("/source/synth")
    def source_synth(body: SynthBody):
        session = holder.session
        return _run(
            lambda: _capture(holder, session.generate_synth(body.shape, **body.params))
        )

    @app.post("/source/load")
    def source_load(body: PathBody):
        # A user input file: read-only + checksummed inside session.load (rule 1).
        session = holder.session
        return _run(lambda: _capture(holder, session.load(body.path)))

    # -- one POST per STEP_KNOBS step ------------------------------------
    for step, model in models.items():
        app.add_api_route(
            f"/step/{step}",
            _make_step_endpoint(holder, step, model),
            methods=["POST"],
            name=f"step_{step}",
            response_model=None,
            summary=f"compute_{step}",
        )

    # -- history / work files / session ----------------------------------
    @app.get("/schema")
    def get_schema():
        return schema_json()

    @app.get("/catalog")
    def get_catalog():
        # Synth shape names for the frontend's source dropdown. The web layer
        # never imports the synth package (rule 4) -> it asks the session,
        # which owns the catalog (MeshingSession.available_shapes()).
        return {"shapes": holder.session.available_shapes()}

    @app.get("/journal")
    def get_journal():
        return {"journal": holder.session.journal}

    @app.get("/state")
    def get_state():
        s = holder.session
        return {
            "rev": holder.rev,
            "input_path": str(s.input_path) if s.input_path else None,
            "input_sha256": s.input_sha256,
            "journal_len": len(s.journal),
            "artifacts": {
                name: getattr(s, name) is not None
                for name in (
                    "surface",
                    "centerline",
                    "sections",
                    "wall",
                    "volmesh",
                    "solve",
                    "cfd",
                    "profile",
                )
            },
        }

    @app.post("/workfile/save")
    def workfile_save(body: PathBody):
        session = holder.session

        def work():
            # Guard the ACTUAL output path (save_workfile forces .mfwork).
            out = Path(body.path)
            if out.suffix != ".mfwork":
                out = out.with_suffix(".mfwork")
            _guard_write(session, out)
            written = session.save_workfile(body.path)
            return {"path": str(written)}

        return _run(work)

    @app.post("/workfile/load")
    def workfile_load(body: WorkfileLoadBody):
        def work():
            from meshforge.pipeline import load_workfile, replay

            loaded = load_workfile(body.path)
            results = replay(loaded) if body.replay else []
            holder.session = loaded  # only after a clean load+replay
            holder.rev += 1
            # Show the replayed artifact in the viewport: cache the last result
            # that carries a mesh (fall back to clearing if the replay was empty).
            holder.last_render = None
            for r in results:
                hints = getattr(r, "render_hints", None)
                if hints and hints.get("mesh") is not None:
                    holder.last_render = hints
            return {
                "journal": loaded.journal,
                "results": [_result_json(r) for r in results],
            }

        return _run(work)

    @app.post("/session/reset")
    def session_reset():
        holder.session = MeshingSession()
        holder.last_render = None
        holder.rev += 1
        return {"ok": True}

    @app.get("/health")
    def health():
        return {"ok": True, "name": "meshforge-web", "steps": sorted(STEP_KNOBS)}

    # -- viewport geometry (V35 increment 3) -----------------------------
    # Serialises the CURRENT artifact's already-computed mesh to the vtk.js
    # state the browser renders client-side. Read-only, in-memory only: no
    # client path, no upload, no write, no WebSocket. The one display module
    # (web/viewport.py) is lazy-imported so the base layer stays trame-free.
    @app.get("/geometry")
    def get_geometry(field: str | None = None):
        from fastapi.responses import Response

        hints = holder.last_render
        if not hints or hints.get("mesh") is None:
            return Response(status_code=204)  # nothing rendered yet

        def work():
            from meshforge.web import viewport

            payload = viewport.serialize(
                hints["mesh"], hints.get("kind", "surface"), field
            )
            frames = hints.get("frames")
            payload["label"] = hints.get("label")
            payload["n_frames"] = len(frames) if frames else 0
            payload["times"] = [float(t) for t, _ in frames] if frames else []
            # QC overlay (e.g. import_cso's raw contour polylines): already
            # built by the step — serialized here, painted client-side.
            overlay = hints.get("overlay")
            if overlay is not None:
                payload["overlay"] = {
                    "state": viewport.serialize_overlay(overlay),
                    "label": hints.get("overlay_label"),
                }
            return payload

        return _run(work)

    @app.get("/geometry/frame/{index}")
    def get_geometry_frame(index: int, field: str | None = None):
        hints = holder.last_render
        if not hints or not hints.get("frames"):
            raise HTTPException(404, "no animation frames in the current artifact")
        frames = hints["frames"]
        if index < 0 or index >= len(frames):
            raise HTTPException(
                400, f"frame {index} out of range 0..{len(frames) - 1}"
            )

        def work():
            from meshforge.web import viewport

            t, mesh = frames[index]
            payload = viewport.serialize(mesh, hints.get("kind", "volume"), field)
            payload["time"] = float(t)
            payload["index"] = index
            return payload

        return _run(work)

    # -- viewport vendor JS (served read-only from the installed trame pkgs) --
    # Exactly two files (the vtk.js/vue_vtk bundle + the Vue UMD), resolved at
    # startup from the installed packages -> single source of truth, no stale
    # committed blob, and the served paths carry NO user input (no traversal).
    # Gated on trame-vtk being present so the base install stays light.
    _vendor = _vendor_assets()

    @app.get("/vendor/{name}", include_in_schema=False)
    def vendor(name: str):
        from fastapi.responses import FileResponse

        target = _vendor.get(name)
        if target is None:
            raise HTTPException(404, "unknown viewport asset")
        return FileResponse(target, media_type="application/javascript")

    # -- static Workbench frontend (V35 increment 2) --------------------
    @app.get("/", include_in_schema=False)
    def root():
        # The server root lands on the Workbench (served at /app/).
        from fastapi.responses import RedirectResponse

        return RedirectResponse(url="/app/")

    static_dir = Path(__file__).parent / "static"
    static_dir.mkdir(parents=True, exist_ok=True)
    from fastapi.staticfiles import StaticFiles

    app.mount("/app", StaticFiles(directory=static_dir, html=True), name="app")
    return app


def _make_step_endpoint(holder: _SessionHolder, step: str, model: type):
    """Build the POST handler for one step. Its ``body`` param is annotated
    with the step's generated pydantic model (set at runtime) so FastAPI
    validates the request body against STEP_KNOBS and documents it."""

    def endpoint(body):
        session = holder.session

        def work():
            params = drop_unset(body.model_dump())
            # screenshot writes a client-supplied path -> reuse the guard.
            if step == "screenshot" and params.get("path"):
                _guard_write(session, params["path"])
            method = getattr(session, f"compute_{step}")
            return _capture(holder, method(**params))

        return _run(work)

    endpoint.__name__ = f"step_{step}"
    endpoint.__annotations__ = {"body": model, "return": dict}
    return endpoint

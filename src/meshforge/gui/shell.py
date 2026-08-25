"""meshforge desktop shell (V35b) — a native window over the local web UI.

This is the QtWebEngine desktop shell decided in ``docs/specs/V35B_SPEC.md``
(D54). It wraps the *completed* 127.0.0.1 web Workbench in a native window:

  1. start the existing web server (``web/server.py``) as a **child process**
     on a free loopback port — the same staged-runner discipline as the
     solvers (argv list, no shell, free port, reaped on close);
  2. host a ``QWebEngineView`` pointed at ``http://127.0.0.1:<port>/app/``.

**Rule 4:** the shell contains NO meshing/solve logic. It only launches the
GUI-agnostic JSON server and points a webview at it — every domain call still
goes through a ``MeshingSession`` step behind that server.

Qt / QtWebEngine are imported **lazily inside the functions that need them**
(mirroring ``gui/app.py:main``), so ``import meshforge.gui.shell`` — and the
``ServerProcess`` lifecycle helper below — stay Qt-free and headless-testable.
QtWebEngine needs the conda-forge ``qt6-webengine`` native package (the
``desktop`` extra); never pip PySide6 (macOS exit-134 rule).
"""
from __future__ import annotations

import argparse
import atexit
import subprocess
import sys
import time
import urllib.request

from meshforge.web.server import find_free_port

_LOOPBACK = "127.0.0.1"


class ServerProcess:
    """The child ``meshforge.web.server`` process, with a loud lifecycle.

    Headless and Qt-free on purpose: the shell's server management is unit
    tested without opening a window. ``stop()`` is idempotent.
    """

    def __init__(self, port: int | None = None) -> None:
        self.port: int = int(port) if port else find_free_port()
        self.base_url: str = f"http://{_LOOPBACK}:{self.port}"
        self._proc: subprocess.Popen | None = None

    # -- argv discipline: fixed list, no shell (mirrors the solver runners) --
    def _argv(self) -> list[str]:
        return [sys.executable, "-m", "meshforge.web.server", "--port", str(self.port)]

    def start(self) -> "ServerProcess":
        if self._proc is not None:
            return self
        self._proc = subprocess.Popen(self._argv())
        atexit.register(self.stop)  # belt-and-suspenders against a hard exit
        return self

    def wait_until_ready(self, timeout: float = 30.0) -> bool:
        """Poll ``GET /health`` until it answers 200, or time out."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._proc is not None and self._proc.poll() is not None:
                return False  # child died before it was ready
            try:
                with urllib.request.urlopen(self.base_url + "/health", timeout=2) as r:
                    if r.status == 200:
                        return True
            except Exception:
                time.sleep(0.25)
        return False

    def poll(self) -> int | None:
        return self._proc.poll() if self._proc is not None else None

    def stop(self, timeout: float = 5.0) -> None:
        """Terminate the child and reap it — no orphan process, port freed.

        Idempotent: the ``Popen`` handle is kept after reaping (so ``poll()``
        still reports the return code) and a second call is a no-op because the
        process already has an exit status.
        """
        proc = self._proc
        if proc is None or proc.poll() is not None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=timeout)


def run(
    port: int | None = None,
    *,
    settle_ms: int = 3000,
    auto_close_ms: int | None = None,
    on_probe=None,
) -> int:
    """Launch the desktop shell and run the Qt event loop until the window closes.

    ``auto_close_ms`` / ``on_probe`` / ``settle_ms`` exist only for the opt-in
    live smoke test: when ``on_probe`` is given, after the page loads a small
    JavaScript probe reports ``{title, webgl2, canvas_present, ...}`` and the
    app quits; ``auto_close_ms`` is a hard safety net so a test can never hang.
    In normal use both are ``None`` and the window lives until the user closes
    it, at which point the child server is reaped.
    """
    import json

    # Lazy by design (see the module docstring), which means a fresh clone gets
    # here with no Qt at all: `conda env create -f environment.yml` installs the
    # BASE deps only. Name the conda-forge packages, and say NOT pip — a pip
    # PySide6 bundles a second Qt beside conda's qt6-main and hard-crashes with
    # exit 134 on macOS. QtWebEngine's native libs are a separate package.
    try:
        from PySide6.QtCore import (
            QFile,
            QIODevice,
            QObject,
            Qt,
            QTimer,
            QUrl,
            Slot,
        )
        from PySide6.QtWebChannel import QWebChannel
        from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineScript
        from PySide6.QtWebEngineWidgets import QWebEngineView
        from PySide6.QtWidgets import QApplication, QFileDialog
    except ImportError as exc:
        raise RuntimeError(
            "the desktop shell needs PySide6 + QtWebEngine — install from "
            "conda-forge, NOT pip:\n"
            "  conda install -c conda-forge pyside6 qt6-webengine\n"
            "It also serves the web UI, so the [web] extra is required too: "
            "pip install -e '.[web]'\n"
            "No Qt available? The same Workbench runs in a browser: "
            "meshforge-web"
        ) from exc

    # Native file-open bridge (Task 2). The desktop window loads sources by
    # SERVER-SIDE PATH; a browser <input type=file> would hand over the file
    # *contents*, not a path the Python backend can read. So we expose a
    # QFileDialog over QWebChannel that returns the chosen ABSOLUTE path, and the
    # frontend's "Browse…" button drops that string into a path field. Rule 4:
    # this only moves a string — zero meshing/IO logic here or in the frontend.
    _OPEN_FILTER = (
        "Meshes & scans (*.stl *.vtp *.ply *.obj *.vtk *.cso *.dwg *.dxf "
        "*.step *.stp *.iges *.igs *.brep *.npy *.nii *.nii.gz);;All files (*)"
    )

    class _FileBridge(QObject):
        @Slot(result=str)
        def open_file(self) -> str:  # noqa: D401 - JS-callable slot
            path, _ = QFileDialog.getOpenFileName(
                None, "Choose a file", "", _OPEN_FILTER
            )
            return path or ""

        @Slot(result=str)
        def open_directory(self) -> str:
            return (
                QFileDialog.getExistingDirectory(None, "Choose a directory", "")
                or ""
            )

    server = ServerProcess(port).start()
    if not server.wait_until_ready():
        server.stop()
        raise RuntimeError(
            f"meshforge desktop: the web server did not become ready at "
            f"{server.base_url} — refusing to open a blank window"
        )

    # QtWebEngine needs shared GL contexts set before the QApplication exists.
    QApplication.setAttribute(Qt.AA_ShareOpenGLContexts)
    app = QApplication.instance() or QApplication(sys.argv)

    class _LoopbackPage(QWebEnginePage):
        """Defence-in-depth: keep the webview on the loopback origin we started."""

        def acceptNavigationRequest(self, url: QUrl, _type, _is_main_frame) -> bool:  # noqa: N802
            host = url.host()
            return host in ("", _LOOPBACK) and url.scheme() in ("", "http", "about")

    view = QWebEngineView()
    view.setPage(_LoopbackPage(view))
    view.setWindowTitle("meshforge")
    view.resize(1280, 800)

    # Expose the native file bridge to the page over QWebChannel, and inject
    # Qt's qwebchannel.js + a bootstrap at document creation so the frontend
    # sees ``window.meshforgeBridge`` before app.js runs (no CSP on the local
    # app, so main-world injection is fine).
    bridge = _FileBridge(view)
    channel = QWebChannel(view)
    channel.registerObject("filebridge", bridge)
    view.page().setWebChannel(channel)
    _qwc = QFile(":/qtwebchannel/qwebchannel.js")
    if _qwc.open(QIODevice.ReadOnly):
        boot = bytes(_qwc.readAll().data()).decode("utf-8") + (
            "\nnew QWebChannel(qt.webChannelTransport, function (channel) {"
            "\n  window.meshforgeBridge = channel.objects.filebridge;"
            "\n});\n"
        )
        _qwc.close()
        script = QWebEngineScript()
        script.setName("meshforge_filebridge")
        script.setInjectionPoint(QWebEngineScript.InjectionPoint.DocumentCreation)
        script.setWorldId(QWebEngineScript.ScriptWorldId.MainWorld)
        script.setRunsOnSubFrames(False)
        script.setSourceCode(boot)
        view.page().scripts().insert(script)

    view.setUrl(QUrl(f"{server.base_url}/app/"))
    view.show()

    _PROBE_JS = r"""
    (function () {
      var slot = document.querySelector('#viewport-slot');
      var canvas = slot ? slot.querySelector('canvas') : null;
      var c = document.createElement('canvas');
      return JSON.stringify({
        title: document.title,
        webgl2: !!c.getContext('webgl2'),
        webgl: !!(c.getContext('webgl2') || c.getContext('webgl')),
        canvas_present: !!canvas
      });
    })();
    """

    def _do_probe() -> None:
        def _cb(result) -> None:
            try:
                on_probe(json.loads(result))
            except Exception:  # noqa: BLE001 - never let the probe wedge shutdown
                on_probe({"raw": result})
            app.quit()

        view.page().runJavaScript(_PROBE_JS, _cb)

    def _on_load(ok: bool) -> None:
        if on_probe is not None:
            QTimer.singleShot(settle_ms, _do_probe)

    view.loadFinished.connect(_on_load)
    if auto_close_ms is not None:
        QTimer.singleShot(auto_close_ms, app.quit)

    app.aboutToQuit.connect(server.stop)
    try:
        return app.exec()
    finally:
        server.stop()  # idempotent; covers non-normal exits too


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="meshforge-desktop",
        description="Open the meshforge Workbench in a native window "
        "(QtWebEngine over the local 127.0.0.1 web server). Deps: the "
        "'desktop' extra (conda-forge qt6-webengine).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="port for the child web server (default: an OS-assigned free port)",
    )
    args = parser.parse_args(argv)
    return run(port=args.port)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

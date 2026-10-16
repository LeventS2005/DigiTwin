"""V35b desktop-shell tests (D54).

Three headless checks that run everywhere + one opt-in live smoke:

  * ``ServerProcess`` lifecycle — a real child web server is started on a free
    loopback port, becomes ready, serves ``/app/``, and is reaped on ``stop()``
    (no orphan, port freed). No Qt.
  * the shell module imports **Qt-free** (lazy PySide6) so the lifecycle helper
    and ``import meshforge.cli`` never pull QtWebEngine.
  * rule 4 — ``gui/shell.py`` holds no meshing/solve logic; it only launches the
    GUI-agnostic web server and hosts a webview.
  * live smoke (opt-in, own process) — the native window loads ``/app/`` and
    WebGL2 is available.
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import urllib.request
from pathlib import Path

import pytest


def _get(url: str, timeout: float = 5.0) -> int:
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.status


def test_server_process_lifecycle() -> None:
    """Start a real child server on a free port, serve /app/, reap it cleanly."""
    from meshforge.gui.shell import ServerProcess

    server = ServerProcess()
    port = server.port
    server.start()
    try:
        assert server.wait_until_ready(timeout=30), "child web server never became ready"
        assert _get(server.base_url + "/health") == 200
        assert _get(server.base_url + "/app/") == 200
        assert server.poll() is None  # still running
    finally:
        server.stop()

    # reaped: the child has a return code, and the port is bindable again.
    assert server.poll() is not None, "child web server was not reaped (orphan)"
    # SO_REUSEADDR bypasses the just-closed listener's TIME_WAIT but still fails
    # against a live orphaned LISTEN socket -> a real "port freed" check.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", port))


def test_stop_is_idempotent_without_start() -> None:
    from meshforge.gui.shell import ServerProcess

    server = ServerProcess()
    server.stop()  # never started -> no-op, no error
    server.stop()
    assert server.poll() is None


def test_shell_import_is_qt_free() -> None:
    """Importing the shell (and the CLI) must not pull PySide6/QtWebEngine.

    Fresh interpreter so a Qt import from another test can't mask a regression.
    """
    code = (
        "import sys\n"
        "import meshforge.cli\n"
        "import meshforge.gui.shell\n"
        "bad = [m for m in sys.modules if m.startswith('PySide6')]\n"
        "assert not bad, bad\n"
        "print('ok')\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True
    )
    assert proc.returncode == 0, proc.stderr
    assert "ok" in proc.stdout


def test_shell_holds_no_meshing_logic() -> None:
    """Rule 4: gui/shell.py only launches the web server + hosts a webview.
    AST-based (V43): only real imports/name references count — a token in a
    docstring or comment no longer trips the scan (the scanner itself is
    exercised in test_rule4_scan.py)."""
    from rule4_scan import assert_holds_no_meshing_logic

    import meshforge.gui.shell as shell_mod

    shell_path = Path(shell_mod.__file__)
    assert_holds_no_meshing_logic(shell_path)
    source = shell_path.read_text()
    # Positive contract: it launches the existing server as a child process.
    assert "meshforge.web.server" in source
    assert "ServerProcess" in source


def test_shell_wires_native_file_bridge() -> None:
    """Task 2: the shell exposes a QFileDialog over QWebChannel so the frontend
    can fill a server-side path field. Still Qt-free to import (the bridge lives
    inside run()), and holds no meshing logic."""
    import meshforge.gui.shell as shell_mod

    source = Path(shell_mod.__file__).read_text()
    for token in ("QWebChannel", "registerObject", "open_file", "open_directory",
                  "save_file", "QFileDialog", "qwebchannel.js", "getOpenFileName",
                  "getExistingDirectory", "getSaveFileName"):
        assert token in source, f"shell.py missing file-bridge wiring: {token}"
    # the filter set the brief specifies
    for ext in (".stl", ".vtp", ".cso", ".dwg", ".dxf", ".step", ".nii.gz"):
        assert ext in source, f"file filter missing {ext}"


# ---- opt-in live smoke: real QtWebEngine window (own process) --------------

live_only = pytest.mark.skipif(
    os.environ.get("MESHFORGE_LIVE_SHELL") != "1",
    reason="live shell smoke: set MESHFORGE_LIVE_SHELL=1 and run this file alone",
)


@live_only
def test_shell_live_loads_app_with_webgl() -> None:
    assert os.environ.get("QT_QPA_PLATFORM") != "offscreen", (
        "QtWebEngine WebGL needs a real platform — run this file alone"
    )
    from meshforge.gui.shell import run

    captured: dict = {}

    def on_probe(result: dict) -> None:
        captured.update(result)

    # auto_close_ms is the hard net; on_probe quits right after it captures.
    run(settle_ms=3000, auto_close_ms=20000, on_probe=on_probe)

    assert "Workbench" in str(captured.get("title", "")), captured
    assert captured.get("webgl2") is True, captured
    assert captured.get("canvas_present") is True, captured


# ---- session 39: a fresh clone must be told what to install ---------------


def test_run_without_qt_names_the_conda_forge_fix(monkeypatch) -> None:
    """Qt is imported lazily inside run(), so on a machine with only the base
    deps this is where a fresh clone lands. The message must name conda-forge
    (a pip PySide6 bundles a second Qt beside conda's qt6-main -> exit 134),
    name qt6-webengine separately, and offer the browser fallback."""
    import builtins

    from meshforge.gui.shell import run

    real = builtins.__import__

    def fake(name, *a, **k):
        if name == "PySide6" or name.startswith("PySide6."):
            raise ImportError(f"no module named {name}")
        return real(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake)

    with pytest.raises(RuntimeError) as exc:
        run()
    msg = str(exc.value)
    assert "conda-forge" in msg and "NOT pip" in msg
    assert "qt6-webengine" in msg
    assert "meshforge-web" in msg  # the no-Qt fallback
    assert isinstance(exc.value.__cause__, ImportError)

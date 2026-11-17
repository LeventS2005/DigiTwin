"""uvicorn launcher for the local meshforge web layer.

Binds **127.0.0.1 only** (a non-loopback host is refused loudly) on a free
port (OS-assigned unless ``--port`` is given) and prints the URL. Argv/staged
discipline mirrors the solver runners. Imported lazily by the CLI so
``import digitwin.cli`` never pulls FastAPI/uvicorn.
"""
from __future__ import annotations

import argparse
import socket

_LOOPBACK = "127.0.0.1"


def find_free_port() -> int:
    """An OS-assigned free TCP port on the loopback interface."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((_LOOPBACK, 0))
        return sock.getsockname()[1]


def serve(port: int | None = None, *, host: str = _LOOPBACK) -> None:
    """Start uvicorn serving the app on 127.0.0.1:<port>."""
    if host != _LOOPBACK:
        raise ValueError(
            f"meshforge web binds {_LOOPBACK} only (local single-user tool); "
            f"refusing host {host!r}"
        )
    # The web extra is lazy (the CLI must never pay for FastAPI/uvicorn), and
    # `conda env create -f environment.yml` installs the BASE deps only — so a
    # fresh clone reaches this line without them. Fail with the fix, not a bare
    # ModuleNotFoundError (same idiom as the DICOM/NIfTI loaders).
    try:
        import uvicorn

        from digitwin.web.app import create_app
    except ImportError as exc:
        raise RuntimeError(
            "the meshforge web app needs the [web] extra (FastAPI/uvicorn) — "
            "install with: pip install -e '.[web]'\n"
            "For the 3-D viewport add, from conda-forge only: "
            "conda install -c conda-forge trame-vtk trame-vuetify"
        ) from exc

    port = int(port) if port else find_free_port()
    app = create_app()
    url = f"http://{host}:{port}"
    print(f"meshforge web -> {url}  ({_LOOPBACK} only; Ctrl-C to stop)")
    print(f"  Workbench: {url}/app/   OpenAPI docs: {url}/docs   schema: {url}/schema")
    uvicorn.run(app, host=host, port=port, log_level="info")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="meshforge-web",
        description="Serve the local meshforge web app on 127.0.0.1.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="port to bind (default: an OS-assigned free port)",
    )
    args = parser.parse_args(argv)
    serve(port=args.port)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

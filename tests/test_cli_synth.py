from __future__ import annotations

import json
from pathlib import Path

from digitwin.cli import main


def test_synth_end_to_end(tmp_path: Path, capsys) -> None:
    outputs = tmp_path / "outputs"
    rc = main(["synth", "torus", "--outputs", str(outputs), "--set", "tube_radius=1.0"])
    assert rc == 0

    surface_path = outputs / "torus.vtp"
    truth_path = outputs / "torus_truth.json"
    assert surface_path.is_file()
    truth = json.loads(truth_path.read_text())
    assert truth["shape"] == "torus"
    assert truth["params"]["tube_radius"] == 1.0
    assert truth["closed"] is True
    assert "truth volume" in capsys.readouterr().out

    # The generated file must round-trip through our own loader/inspector.
    from digitwin.io import file_sha256, load_surface
    from digitwin.quality import inspect_surface

    report = inspect_surface(
        load_surface(surface_path),
        path=str(surface_path),
        sha256=file_sha256(surface_path),
    )
    assert report.watertight
    assert report.volume == __import__("pytest").approx(truth["volume"], rel=0.01)


def test_synth_unknown_shape(tmp_path: Path, capsys) -> None:
    rc = main(["synth", "banana", "--outputs", str(tmp_path)])
    assert rc == 1
    assert "unknown shape" in capsys.readouterr().err


def test_synth_bad_param_value(tmp_path: Path, capsys) -> None:
    rc = main(["synth", "torus", "--outputs", str(tmp_path), "--set", "tube_radius=abc"])
    assert rc == 1
    assert "must be numeric" in capsys.readouterr().err


def test_synth_unknown_param_name(tmp_path: Path, capsys) -> None:
    rc = main(["synth", "torus", "--outputs", str(tmp_path), "--set", "bananas=3"])
    assert rc == 1
    assert "bananas" in capsys.readouterr().err

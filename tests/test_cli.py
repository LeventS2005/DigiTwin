from __future__ import annotations

import json
from pathlib import Path

import pytest

from digitwin.cli import main
from digitwin.io import file_sha256


def test_inspect_end_to_end(sphere_stl: Path, tmp_path: Path, capsys) -> None:
    outputs = tmp_path / "outputs"
    checksum_before = file_sha256(sphere_stl)

    rc = main(["inspect", str(sphere_stl), "--outputs", str(outputs)])
    assert rc == 0

    # Input is byte-for-byte unchanged; reports land only in outputs/.
    assert file_sha256(sphere_stl) == checksum_before
    json_path = outputs / "sphere_inspect.json"
    txt_path = outputs / "sphere_inspect.txt"
    assert json_path.is_file()
    assert txt_path.is_file()

    parsed = json.loads(json_path.read_text())
    assert parsed["sha256"] == checksum_before
    assert parsed["watertight"] is True
    assert parsed["volume"] is not None

    out = capsys.readouterr().out
    assert "watertight  : True" in out
    assert str(json_path) in out


def test_inspect_missing_file_exits_nonzero(tmp_path: Path, capsys) -> None:
    rc = main(["inspect", str(tmp_path / "nope.stl"), "--outputs", str(tmp_path / "outputs")])
    assert rc == 1
    assert "error:" in capsys.readouterr().err
    assert not (tmp_path / "outputs").exists()  # no report for a failed inspect


def test_unknown_command_is_rejected() -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["frobnicate"])
    assert excinfo.value.code == 2

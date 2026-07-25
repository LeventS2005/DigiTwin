from __future__ import annotations

import json
from pathlib import Path

from meshforge.cli import main
from meshforge.io import file_sha256


def test_export_end_to_end(sphere_stl: Path, tmp_path: Path, capsys) -> None:
    out = tmp_path / "outputs" / "sphere_neutral.vtu"
    checksum_before = file_sha256(sphere_stl)

    rc = main(["export", str(sphere_stl), "--out", str(out)])
    assert rc == 0
    assert out.is_file()
    assert file_sha256(sphere_stl) == checksum_before
    assert "exported" in capsys.readouterr().out


def test_export_cli_refuses_input_overwrite(sphere_stl: Path, tmp_path: Path, capsys) -> None:
    import pyvista as pv

    ply_input = tmp_path / "input.ply"
    pv.Sphere().save(ply_input)
    raw_before = ply_input.read_bytes()

    rc = main(["export", str(ply_input), "--out", str(ply_input)])
    assert rc == 1
    assert "refusing to overwrite input" in capsys.readouterr().err
    assert ply_input.read_bytes() == raw_before


def test_export_cli_rejects_unknown_format(sphere_stl: Path, tmp_path: Path, capsys) -> None:
    rc = main(["export", str(sphere_stl), "--out", str(tmp_path / "x.step")])
    assert rc == 1
    assert "unknown export format" in capsys.readouterr().err


def test_check_end_to_end(open_plane_ply: Path, tmp_path: Path, capsys) -> None:
    outputs = tmp_path / "outputs"
    rc = main(["check", str(open_plane_ply), "--outputs", str(outputs)])
    assert rc == 0  # the check itself succeeded; readiness lives in the report

    parsed = json.loads((outputs / "plane_check.json").read_text())
    assert parsed["mesh_ready"] is False
    assert parsed["n_open_edges"] > 0
    assert "mesh_ready         : False" in capsys.readouterr().out

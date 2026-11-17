"""V7 parametric recipes: schema validation, sweeps, failure isolation."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

from digitwin.cli import main
from digitwin.recipes import (
    expand_variants,
    run_recipe,
    validate_recipe,
    variant_slug,
)

BASE_STEPS = [
    {"step": "synth", "shape": "straight_tube", "n_theta": 32, "n_axial": 21},
    {"step": "centerline", "voxel_size": 0.5, "n_samples": 100},
    {
        "step": "volmesh",
        "backend": "swept",
        "thickness": 0.5,
        "n_theta": 16,
        "n_axial": 20,
        "n_layers": 2,
    },
    {"step": "save", "what": "volmesh"},
]


def _recipe(tmp_path: Path, doc: dict, name: str = "r.yaml") -> Path:
    p = tmp_path / name
    p.write_text(yaml.safe_dump(doc))
    return p


# ---------------------------------------------------------------------------
# schema validation (loud on unknown keys)


def test_unknown_top_level_key_loud():
    with pytest.raises(ValueError, match="unknown recipe key"):
        validate_recipe({"steps": BASE_STEPS, "bogus": 1})


def test_unknown_step_and_param_loud():
    with pytest.raises(ValueError, match="unknown step"):
        validate_recipe({"steps": [{"step": "teleport"}]})
    with pytest.raises(ValueError, match="unknown param"):
        validate_recipe(
            {
                "steps": [
                    {"step": "synth", "shape": "straight_tube"},
                    {"step": "centerline", "smoothingg": 1.0},  # typo
                ]
            }
        )


def test_source_step_required_first():
    with pytest.raises(ValueError, match="first step must be a source"):
        validate_recipe({"steps": [{"step": "centerline"}]})


def test_sweep_validation_loud():
    steps = [{"step": "synth", "shape": "straight_tube"}]
    with pytest.raises(ValueError, match="not in steps"):
        validate_recipe({"steps": steps, "sweep": {"volmesh.thickness": [1]}})
    with pytest.raises(ValueError, match="non-empty list"):
        validate_recipe({"steps": steps, "sweep": {"synth.radius": []}})
    with pytest.raises(ValueError, match="unknown param"):
        validate_recipe(
            {
                "steps": BASE_STEPS,
                "sweep": {"volmesh.thicknesss": [0.4]},
            }
        )


def test_expand_and_slug():
    variants = expand_variants({"a.x": [1, 2], "b.y": [0.4, 0.6]})
    assert len(variants) == 4
    slugs = {variant_slug(v) for v in variants}
    assert len(slugs) == 4
    assert variant_slug({}) == "base"
    assert variant_slug({"synth.radius": 2.0}) == "radius-2"


# ---------------------------------------------------------------------------
# execution


def test_2x2_sweep_produces_variant_dirs_and_manifest(tmp_path):
    recipe = _recipe(
        tmp_path,
        {
            "name": "tube_study",
            "steps": BASE_STEPS,
            "sweep": {
                "synth.radius": [2.0, 3.0],
                "volmesh.thickness": [0.4, 0.6],
            },
        },
    )
    out = tmp_path / "outputs"
    manifest = run_recipe(recipe, outputs=out)

    assert manifest["n_ok"] == 4 and manifest["n_failed"] == 0
    assert len(manifest["variants"]) == 4
    for v in manifest["variants"]:
        assert v["status"] == "ok"
        assert v["quality"]["n_inverted"] == 0
        vdir = out / "tube_study" / v["slug"]
        assert vdir.is_dir()
        assert len(v["outputs"]) == 1
        assert Path(v["outputs"][0]).is_file()
        assert Path(v["outputs"][0]).is_relative_to(vdir)
    # Params actually vary: 4 distinct (radius, thickness) combos.
    combos = {
        (v["params"]["synth.radius"], v["params"]["volmesh.thickness"])
        for v in manifest["variants"]
    }
    assert len(combos) == 4

    disk = json.loads((out / "tube_study" / "manifest.json").read_text())
    assert disk["n_ok"] == 4


def test_failing_variant_does_not_kill_batch(tmp_path):
    recipe = _recipe(
        tmp_path,
        {
            "name": "faulty",
            "steps": BASE_STEPS,
            "sweep": {"volmesh.thickness": [-1.0, 0.5]},
        },
    )
    manifest = run_recipe(recipe, outputs=tmp_path / "outputs")
    statuses = sorted(v["status"] for v in manifest["variants"])
    assert statuses == ["failed", "ok"]
    failed = next(v for v in manifest["variants"] if v["status"] == "failed")
    assert failed["error"]  # loud, recorded
    assert manifest["n_failed"] == 1


def test_output_path_cannot_escape_variant_dir(tmp_path):
    steps = [
        {"step": "synth", "shape": "straight_tube", "n_theta": 32, "n_axial": 21},
        {"step": "save", "what": "surface", "path": "../../evil.vtp"},
    ]
    manifest = run_recipe(
        {"name": "escape", "steps": steps}, outputs=tmp_path / "outputs"
    )
    assert manifest["variants"][0]["status"] == "failed"
    assert "escapes" in manifest["variants"][0]["error"]
    assert not (tmp_path / "evil.vtp").exists()


def test_cli_run_e2e(tmp_path, capsys):
    recipe = _recipe(
        tmp_path,
        {
            "name": "cli_study",
            "steps": BASE_STEPS,
            "sweep": {"volmesh.thickness": [0.4, 0.6]},
        },
    )
    out = tmp_path / "outputs"
    rc = main(["run", str(recipe), "--outputs", str(out)])
    text = capsys.readouterr().out
    assert rc == 0
    assert text.count("variant ok") == 2
    assert "2 ok, 0 failed" in text

    # --set overrides win over the sweep (both variants collapse to 0.5).
    rc = main(
        ["run", str(recipe), "--outputs", str(out), "--set", "volmesh.n_layers=3"]
    )
    assert rc == 0
    manifest = json.loads((out / "cli_study" / "manifest.json").read_text())
    assert all(v["status"] == "ok" for v in manifest["variants"])

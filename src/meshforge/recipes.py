"""Parametric recipes: YAML pipelines with sweeps (V7).

A recipe is a YAML document driving MeshingSession end to end:

    name: tube_study            # optional, defaults to the file stem
    steps:
      - step: synth
        shape: straight_tube
        radius: 3.0
      - step: centerline
      - step: volmesh
        thickness: 0.5
      - step: save
        what: volmesh
    sweep:                      # named parameter lists -> cartesian grid
      synth.radius: [2.0, 3.0]
      volmesh.thickness: [0.4, 0.6]

Every variant runs in its own ``outputs/<recipe>/<slug>/`` directory; a
``manifest.json`` records params, output paths, volmesh quality, and
pass/fail per variant. One failing variant never kills the batch — it is
reported loudly and the rest continue. yaml.safe_load only, no eval;
recipe output paths may not escape the variant directory.
"""
from __future__ import annotations

import itertools
import json
import re
from copy import deepcopy
from pathlib import Path
from typing import Any

from meshforge.pipeline import STEP_KNOBS, MeshingSession

_SOURCE_STEPS = ("synth", "load")
_KNOWN_STEPS = ("synth", "load", "centerline", "sections", "wall", "volmesh", "save", "deck")
_TOP_KEYS = {"name", "steps", "sweep"}

# Parameter allowlists for steps with fixed knob sets. synth/load params are
# validated by their implementations (unknown generator params raise there).
_FIXED_PARAMS: dict[str, set[str]] = {
    "centerline": set(STEP_KNOBS["centerline"])
    | {"extend_to_caps", "cap_normal_tol", "cap_standoff"},
    "sections": set(STEP_KNOBS["sections"]),
    "wall": set(STEP_KNOBS["wall"]),
    "volmesh": set(STEP_KNOBS["volmesh"]) | {"mesh_size", "optimize"},
    "save": {"what", "path", "fmt"},
    "deck": {
        "material",
        "fixes",
        "pressures",
        "cloads",
        "target",
        "formulation",
        "nlgeom",
        "pin_lateral",
        "path",
    },
}

_DEFAULT_SAVE_NAME = {
    "surface": "surface.vtp",
    "wall_outer": "wall_outer.vtp",
    "volmesh": "volmesh.vtu",
    "abaqus": "mesh.inp",
}


def load_recipe(path: str | Path) -> dict:
    import yaml

    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"recipe file not found: {path}")
    data = yaml.safe_load(path.read_text())
    if not isinstance(data, dict):
        raise ValueError(f"recipe must be a YAML mapping, got {type(data).__name__}")
    data.setdefault("name", path.stem)
    validate_recipe(data)
    return data


def validate_recipe(data: dict) -> None:
    """Loud schema validation: unknown keys/steps/params are errors."""
    unknown = set(data) - _TOP_KEYS
    if unknown:
        raise ValueError(
            f"unknown recipe key(s) {sorted(unknown)}; allowed: {sorted(_TOP_KEYS)}"
        )
    steps = data.get("steps")
    if not isinstance(steps, list) or not steps:
        raise ValueError("recipe needs a non-empty 'steps' list")
    names: list[str] = []
    for i, entry in enumerate(steps):
        if not isinstance(entry, dict) or "step" not in entry:
            raise ValueError(f"steps[{i}] must be a mapping with a 'step' key")
        name = entry["step"]
        if name not in _KNOWN_STEPS:
            raise ValueError(
                f"steps[{i}]: unknown step {name!r}; known: {list(_KNOWN_STEPS)}"
            )
        names.append(name)
        params = set(entry) - {"step"}
        if name in _FIXED_PARAMS:
            bad = params - _FIXED_PARAMS[name]
            if bad:
                raise ValueError(
                    f"steps[{i}] ({name}): unknown param(s) {sorted(bad)}; "
                    f"allowed: {sorted(_FIXED_PARAMS[name])}"
                )
        elif name == "synth" and "shape" not in entry:
            raise ValueError(f"steps[{i}] (synth): missing required 'shape'")
        elif name == "load" and "path" not in entry:
            raise ValueError(f"steps[{i}] (load): missing required 'path'")
    if names[0] not in _SOURCE_STEPS:
        raise ValueError(
            f"first step must be a source ({'/'.join(_SOURCE_STEPS)}), got {names[0]!r}"
        )

    sweep = data.get("sweep", {})
    if sweep is None:
        return
    if not isinstance(sweep, dict):
        raise ValueError("'sweep' must be a mapping of step.param -> list")
    for key, values in sweep.items():
        if not isinstance(values, list) or not values:
            raise ValueError(f"sweep[{key!r}] must be a non-empty list")
        step_name, _, param = key.partition(".")
        if not param:
            raise ValueError(f"sweep key {key!r} must be 'step.param'")
        if step_name not in names:
            raise ValueError(
                f"sweep key {key!r} references step {step_name!r} which is "
                f"not in steps {names}"
            )
        if step_name in _FIXED_PARAMS and param not in _FIXED_PARAMS[step_name]:
            raise ValueError(
                f"sweep key {key!r}: unknown param for {step_name!r}; "
                f"allowed: {sorted(_FIXED_PARAMS[step_name])}"
            )


def expand_variants(sweep: dict | None) -> list[dict[str, Any]]:
    if not sweep:
        return [{}]
    keys = sorted(sweep)
    return [
        dict(zip(keys, combo))
        for combo in itertools.product(*(sweep[k] for k in keys))
    ]


def variant_slug(params: dict[str, Any]) -> str:
    if not params:
        return "base"
    parts = []
    for key in sorted(params):
        v = params[key]
        txt = f"{v:g}" if isinstance(v, float) else str(v)
        parts.append(f"{key.split('.')[-1]}-{txt}")
    return re.sub(r"[^A-Za-z0-9_.-]", "-", "_".join(parts))


def _apply_params(steps: list[dict], assignments: dict[str, Any]) -> list[dict]:
    out = deepcopy(steps)
    for key, value in assignments.items():
        step_name, _, param = key.partition(".")
        hit = False
        for entry in out:
            if entry["step"] == step_name:
                entry[param] = value
                hit = True
        if not hit:
            raise ValueError(f"assignment {key!r}: no step named {step_name!r}")
    return out


def _safe_out(vdir: Path, rel: str) -> Path:
    p = (vdir / rel).resolve()
    if not p.is_relative_to(vdir.resolve()):
        raise ValueError(f"recipe output path escapes the variant directory: {rel!r}")
    return p


def _run_steps(steps: list[dict], vdir: Path) -> tuple[list[str], dict | None]:
    """Execute one variant; returns (output paths, volmesh quality)."""
    session = MeshingSession()
    outputs: list[str] = []
    quality: dict | None = None
    for entry in steps:
        entry = dict(entry)
        name = entry.pop("step")
        if name == "synth":
            session.generate_synth(entry.pop("shape"), **entry)
        elif name == "load":
            session.load(entry.pop("path"))
        elif name == "centerline":
            session.compute_centerline(**entry)
        elif name == "sections":
            session.compute_sections(**entry)
        elif name == "wall":
            session.compute_wall(**entry)
        elif name == "volmesh":
            result = session.compute_volmesh(**entry)
            quality = result.summary["quality"]
        elif name == "save":
            what = entry.pop("what")
            rel = entry.pop("path", None) or _DEFAULT_SAVE_NAME.get(what, what)
            out = _safe_out(vdir, rel)
            result = session.save(what, out, **entry)
            outputs.append(result.summary["path"])
        elif name == "deck":
            outputs.append(str(_run_deck_step(session, entry, vdir)))
        else:  # pragma: no cover - validate_recipe guards this
            raise ValueError(f"unknown step {name!r}")
    return outputs, quality


def _run_deck_step(session: MeshingSession, params: dict, vdir: Path) -> Path:
    from meshforge.export.deck import (
        BoundaryCondition,
        DeckSpec,
        Load,
        Material,
        Step,
        lateral_pins,
        write_deck,
    )

    vol = session.volmesh
    if vol is None:
        raise RuntimeError("deck step needs a volmesh — add a volmesh step first")
    mat = dict(params.get("material") or {})
    kind = mat.pop("kind", "elastic")
    if kind == "elastic":
        material = Material.elastic(**mat)
    elif kind == "neo_hooke":
        material = Material.neo_hookean(**mat)
    elif kind == "mooney_rivlin":
        material = Material.mooney_rivlin(**mat)
    else:
        raise ValueError(f"unknown material kind {kind!r}")

    bcs = [BoundaryCondition(*bc) for bc in params.get("fixes", [])]
    if params.get("pin_lateral"):
        bcs.extend(lateral_pins(vol))
    loads = [Load.pressure(*p) for p in params.get("pressures", [])]
    loads += [Load.concentrated(*c) for c in params.get("cloads", [])]
    spec = DeckSpec(
        material=material,
        step=Step(nlgeom=bool(params.get("nlgeom", False))),
        boundary_conditions=tuple(bcs),
        loads=tuple(loads),
        target=params.get("target", "abaqus"),
        formulation=params.get("formulation"),
    )
    out = _safe_out(vdir, params.get("path") or "deck.inp")
    return write_deck(vol, spec, out)


def run_recipe(
    recipe: str | Path | dict,
    *,
    outputs: str | Path = "outputs",
    overrides: dict[str, Any] | None = None,
) -> dict:
    """Run every sweep variant; returns (and writes) the manifest."""
    data = load_recipe(recipe) if not isinstance(recipe, dict) else recipe
    if isinstance(recipe, dict):
        data = dict(recipe)
        data.setdefault("name", "recipe")
        validate_recipe(data)

    name = str(data["name"])
    root = Path(outputs) / name
    root.mkdir(parents=True, exist_ok=True)

    variants = expand_variants(data.get("sweep"))
    manifest: dict[str, Any] = {"recipe": name, "variants": []}
    for vparams in variants:
        slug = variant_slug(vparams)
        vdir = root / slug
        vdir.mkdir(parents=True, exist_ok=True)
        record: dict[str, Any] = {
            "slug": slug,
            "params": vparams,
            "status": "ok",
            "error": None,
            "outputs": [],
            "quality": None,
        }
        try:
            steps = _apply_params(data["steps"], vparams)
            if overrides:
                steps = _apply_params(steps, overrides)
            record["outputs"], record["quality"] = _run_steps(steps, vdir)
            if record["quality"] and record["quality"].get("n_inverted", 0) > 0:
                record["status"] = "failed"
                record["error"] = (
                    f"{record['quality']['n_inverted']} inverted cell(s)"
                )
        except Exception as exc:  # loud per-variant isolation, batch continues
            record["status"] = "failed"
            record["error"] = f"{type(exc).__name__}: {exc}"
        manifest["variants"].append(record)

    manifest["n_ok"] = sum(1 for v in manifest["variants"] if v["status"] == "ok")
    manifest["n_failed"] = len(manifest["variants"]) - manifest["n_ok"]
    (root / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest

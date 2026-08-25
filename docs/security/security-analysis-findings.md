# Security analysis findings — meshforge

**Audit pass, session 11 (2026-07-07).** Reviewer: cybersecurity analyst
(Claude Opus 4.8). Scope: every attack surface added since the D6 baseline,
per the "SECURITY SESSION BRIEFING" in `HANDOFF.md` (parts 1–5). This is a
**read-and-report** pass — no source was modified. Session-start protocol was
run: `CHECKPOINT.md`/`HANDOFF.md` read, `git status` clean (`main` ==
`origin/main`), and the read-only input checksum verified unchanged
(`Cottage_Pane_Window.dwg` sha256 `a4cb1d40…d9068`, matches `INPUTS.md`).

## Threat model (what "problematic" means here)

meshforge is a **local, single-user, offline** meshing/FEA/CFD tool. The user's
own CLI arguments are **not** an attack vector — the user is trusted. The real
trust boundaries are:

1. **Crafted input files the user opens**: the DWG/DXF drawing, recipe YAML,
   `.mfwork` work files, image volumes (`.npy`/NIfTI/DICOM), and solver output
   files (`.frd`/`.dat`/`.F06`/OpenFOAM cases). A malicious or malformed file
   should fail loudly, never silently corrupt output or execute code.
2. **The environment**: the `MESHFORGE_CCX` / `MESHFORGE_MYSTRAN` /
   `MESHFORGE_OPENFOAM` executable-path overrides and PATH discovery. These are
   documented, deliberately-trusted knobs — a hostile binary placed there is
   outside the model, but its *output* is still parsed and echoed.

Findings are ordered by importance. Nothing found is a remote-code-execution or
privilege-escalation hole; the codebase is disciplined (list-argv subprocess,
`yaml.safe_load`, `json.loads`, `allow_pickle=False`, output-path guards). The
items below are integrity / robustness / defense-in-depth gaps.

---

## F1 — MEDIUM — Boundary/patch-group names are written into solver decks and OpenFOAM dictionaries with no content validation (card/dict injection)

**Where (sinks):**
- `src/meshforge/export/deck.py:715` — `f"*ELSET, ELSET={name.upper()}_ELS"`
- `src/meshforge/export/abaqus.py:98` — `f"*NSET, NSET={name.upper()}"`
- `src/meshforge/export/abaqus.py:102` — `f"*SURFACE, NAME={name.upper()}_SURF, TYPE=ELEMENT"`
- `src/meshforge/export/nastran.py`, `src/meshforge/export/lsdyna.py` — same
  `name.upper()` pattern for set names
- `src/meshforge/export/foam.py` / `src/meshforge/solve/foam_case.py` — patch
  names written into `constant/polyMesh/boundary` and the `0/{U,p,k,omega,nut}`
  `boundaryField` dictionaries

The set/patch names come from `wall.boundary` keys. `write_deck`
(`deck.py:626`, `640-680`) carefully validates that BCs/loads/prints *reference*
an existing group — but the group **names themselves** are emitted verbatim,
transformed only by `.upper()`. `.upper()` does not strip newlines or control
characters.

**Where (source reachable from a crafted input file):**
`split_boundary_group` (`src/meshforge/meshing/base.py:57`) accepts a new group
`name` and checks only that the name is not already taken — **not its content**:

```python
if name in wall.boundary:
    raise ValueError(f"target group {name!r} already exists")
...
wall.boundary[name] = BoundaryGroup(name=name, ...)   # name unvalidated
```

This is reachable from a **`.mfwork` work file**, which is a crafted-input
vector. `load_workfile` (`pipeline.py:1311`) validates the step *name* and that
`params` is a dict, but **not the parameter values**:

```python
if (not isinstance(rec, dict)
        or rec.get("step") not in _WORKFILE_STEPS
        or not isinstance(rec.get("params"), dict)):
    raise ValueError(...)
```

`split_patch` is in `_WORKFILE_STEPS` (`pipeline.py:1307`). A hand-edited
`.mfwork` with a `split_patch` step whose `name` param is
`"lid\n*CLOAD\n1,1,1.0e30"` (or, for CFD, a name containing a newline plus a
forged `boundaryField` entry) replays through `compute_split_patch` into
`wall.boundary`, and any subsequent `deck`/`foam` export writes the newline
straight into the solver input. There is **no name content-validator anywhere
in `src/`** (confirmed by grep for `isidentifier`/`fullmatch`/etc.).

**Impact:** a newline in a group name injects arbitrary solver cards
(`*BOUNDARY`, `*CLOAD`, `*STEP`, `*DLOAD` for CalculiX; extra `boundaryField`
entries / dict keys for OpenFOAM). The result is a silently corrupted or
maliciously altered solver deck — a "fail-open" outcome that contradicts the
project's **fail-loudly** hard rule (CLAUDE.md rule 6). In the worst case a
shared `.mfwork` becomes a way to make another user's run compute the wrong
physics without any visible error.

**Severity rationale:** requires a crafted `.mfwork` (or the user's own hostile
CLI arg), so not trivially remote — hence Medium, not High. But it is a genuine
integrity hole with a concrete crafted-input path, and the fix is cheap.

**Fix (recommended):** add one small validator and call it at the *source*
(where names are minted), so bad names are rejected before they ever reach a
writer:

```python
# meshforge/meshing/base.py  (or a shared util)
import re
_GROUP_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,63}$")

def valid_group_name(name: str) -> str:
    if not isinstance(name, str) or not _GROUP_NAME_RE.match(name):
        raise ValueError(
            f"invalid boundary-group name {name!r}: must match "
            f"{_GROUP_NAME_RE.pattern} (letters/digits/_.- , <=64 chars, "
            f"no whitespace/newlines) — refusing to write it into a deck"
        )
    return name
```

Call it in `split_boundary_group` (before creating the new group), in the
`BoundaryGroup` constructor (belt-and-suspenders for every group), and in the
CLI parse of `--default-patch` / `--bc-type NAME,TYPE` / `--inlet`/`--outlet`/
`--moving-wall` names (`cli.py` ~1483/1524/1581). Add a test that a newline-
bearing name is refused at both `split_boundary_group` and `write_deck`. Keep it
in the core (rule-4-safe: it's meshing/export logic, not GUI).

---

## F2 — LOW — Raw external-tool output / raw file bytes are embedded in exception messages (terminal escape-sequence injection)

**Where:**
- `src/meshforge/solve/runner.py:164, 169, 174` — `…\n{output[-2000:]}` (ccx stdout+stderr)
- `src/meshforge/solve/mystran.py:153, 157` — `{(f06_text or output)[-2000:]}`
- `src/meshforge/solve/foam_runner.py:109, 161` — `{output[-2000:]}`
- `src/meshforge/solve/frd.py:129`, `src/meshforge/solve/dat.py` — parse errors echo `{line!r}`
- `src/meshforge/io/cad.py:364` — `{proc.stderr.strip()[-500:]}` (dwg2dxf)

**Impact:** these strings are surfaced to a terminal (CLI) via `RuntimeError`.
A hostile solver binary (installed through a `MESHFORGE_*` override or placed
first on PATH) or a **crafted `.frd`/`.F06`/`.dat`** can embed ANSI/OSC control
sequences in that text. When the exception prints, the terminal interprets them
— screen clearing, cursor manipulation, title/clipboard changes, or output
spoofing (making a failed run look successful). This is the classic "log
injection / terminal escape" class. Low severity for a single-user local tool,
but it is real and the briefing flagged it explicitly.

Note the Qt GUI is **not** affected: `QPlainTextEdit`/status bar render text,
not a terminal, so escape sequences are inert there (see the F-clean notes).

**Fix:** scrub non-printable control characters before embedding untrusted text
in any message. A tiny helper:

```python
def _scrub(text: str, limit: int = 2000) -> str:
    tail = text[-limit:]
    # keep \n and \t; strip other C0/C1 control chars incl. ESC (0x1b)
    return "".join(c if c == "\n" or c == "\t" or c.isprintable() else "�"
                   for c in tail)
```

Apply it to every `output[-2000:]` / `f06_text` / `line!r` / `stderr` embed in
the four subprocess modules and the two solver-output parsers.

---

## F3 — LOW — `dwg2dxf` is the only subprocess without a timeout (hang / DoS on a malformed DWG)

**Where:** `src/meshforge/io/cad.py:358`

```python
proc = subprocess.run(
    [exe, "-o", str(out), str(path)], capture_output=True, text=True
)   # <-- no timeout=
```

The three solver runners all cap execution at `timeout=600` and handle
`subprocess.TimeoutExpired` loudly. This DWG→DXF conversion — which runs GNU
LibreDWG, a C library parsing **hostile binary CAD**, the highest-risk parser in
the tree — has **no timeout**. A malformed/adversarial `.dwg` that drives
LibreDWG into a hang or pathological loop will hang the whole meshforge process
indefinitely with no diagnostic.

**Impact:** denial-of-service / robustness. Combined with the fact that
LibreDWG's own memory-safety is outside our control, an unbounded call is the
worst place to omit a timeout.

**Fix:** mirror the solver runners — add `timeout=120` (conversion is fast for
legitimate files) and wrap in `try/except subprocess.TimeoutExpired` raising a
loud `RuntimeError` naming the file.

---

## F4 — LOW — Image ingest has no `MAX_VOXELS` ceiling (memory exhaustion), unlike the geometry paths

**Where:** `src/meshforge/io/image.py` — `volume_to_surface` (line 20) and
`load_volume` (line 100) validate `ndim`, finiteness, spacing, and level range,
but never bound the total voxel count.

The geometry paths **do** guard this: `centerline.py:13` defines
`MAX_VOXELS = 40_000_000` and both `centerline.py:50` and `sdf.py:41` refuse
grids above it. The V18 image path bypasses that discipline — a large `.npy`,
NIfTI, or DICOM series flows straight into `pv.ImageData` + marching cubes
(`contour`) with no ceiling, which can exhaust memory.

**Impact:** resource-exhaustion DoS from a crafted/oversized volume file. Low
(single-user, and marching-cubes cost scales with the file the user chose), but
it's an inconsistency the briefing pointed at directly ("MAX_VOXELS guards exist
in geometry").

**Fix:** import the existing constant and enforce it in `volume_to_surface`
right after the shape check:

```python
from meshforge.geometry.centerline import MAX_VOXELS
if int(np.prod(array.shape)) > MAX_VOXELS:
    raise ValueError(
        f"volume {array.shape} exceeds {MAX_VOXELS} voxels; downsample first"
    )
```

---

## F5 — INFORMATIONAL — `guard_output_path` is exact-resolved-path only; several case-dir writers don't call it

**Where:** `src/meshforge/export/neutral.py:17`

```python
def guard_output_path(out_path, *, input_paths):
    out = Path(out_path).resolve()
    for p in input_paths:
        if p is not None and out == Path(p).resolve():
            raise ValueError(f"refusing to overwrite input file: {p}")
```

Two observations, both acceptable under the documented model but worth recording:

1. **Exact-match only.** The guard refuses writing over *the exact resolved
   input path*. It does not confine writes to `outputs/`, and it will not catch
   a **hardlink** to an input (a hardlink has a different path but the same
   inode; `resolve()` only collapses symlinks). Today the only declared input is
   the read-only `.dwg`, whose path never collides with a `.vtu`/`.inp`/case
   output, so this is safe in practice. It becomes relevant only if a future
   input type can share a directory/name with a generated artifact.

2. **Case-dir writers bypass the guard.** `solve/foam_case.py` (controlDict,
   fvSchemes, fvSolution, transportProperties, turbulenceProperties, and
   `0/{U,p,k,omega,nut}`) and `solve/foam_runner.py:140-157` (the checkMesh
   stubs) write into a user-chosen case directory **without** calling
   `guard_output_path`. This is fine — a case dir is a working directory, not a
   declared input — but it means rule-2 ("generated files go only into
   `outputs/`") is enforced by convention (CLI defaults, the recipe jail), not
   by these writers. If a user points `cfd`/`foam` at an arbitrary directory,
   files land there.

**Fix (optional / low priority):** (a) pass `input_paths` consistently even when
it feels redundant, so future inputs are covered for free; (b) consider having
`guard_output_path` additionally assert the target resolves under the intended
output root when one is known; (c) if hardlink safety is ever required, compare
`os.stat().st_ino`/`st_dev` rather than paths. None of this is urgent given the
current single-input reality.

---

## Confirmed clean (recorded so the next session can trust these)

These were checked and hold up — they are the reason nothing rises above Medium:

- **No dangerous primitives anywhere in `src/`.** Grep confirms zero
  `eval`/`exec`/`pickle`/`marshal`/`yaml.load`/`os.system`/`Popen`/`shell=True`.
  The single `np.load` is `allow_pickle=False` (`image.py:110`); `app.exec()` is
  the Qt event loop.
- **All 4 subprocess sites use list argv, never a shell string**, stage the
  input into a fresh temp dir (repo hygiene / paths-with-spaces), and 3 of 4
  enforce a 600 s timeout (the exception is F3).
- **OpenFOAM launcher is well-contained.** `run_foam_app` enforces an
  `_ALLOWED_APPS` whitelist (`checkMesh`/`simpleFoam`/`icoFoam`,
  `foam_runner.py:34,69`), and the application name parsed out of a case's
  `controlDict` is restricted to `\w+` (`foam_runner.py:314`) *and then* re-checked
  against the whitelist. A hostile `controlDict` therefore cannot exec an
  arbitrary PATH binary through the launcher — good defense-in-depth.
- **Config/recipe/workfile parsing is safe.** `recipes.py` uses
  `yaml.safe_load` with strict key/step/param allowlisting and a real
  output-path jail (`_safe_out`, `is_relative_to` after `resolve()`, blocks both
  absolute-path and symlink escape). `.mfwork` uses `json.loads` with a
  version + known-step allowlist (its one gap is the *values*, see F1).
- **Solver-output parsers are strict and loud.** `frd.py`/`dat.py`/`mystran.py`
  parse fixed-width/tokenized numerics with `float()`/`int()` only, and fail
  loudly on truncated connectivity, unknown element types, unknown node ids, and
  missing/malformed headers. Magnitudes are trusted as physics once format
  checks pass — acceptable and documented.
- **The GUI genuinely holds the knob-values-only contract.** `gui/app.py`
  performs no I/O (`gather_knobs` collects text/typed values only, dispatching
  session steps on a worker); results render into a `QPlainTextEdit` and a Qt
  status bar, **neither of which interprets markup or terminal escapes**. The V34
  additions add no attack surface: `open_source_path` (`app.py:350`) routes
  `.dwg`/`.dxf` to the same checksummed read-only `compute_import_cad`; the
  `_error_hint` regex (`app.py:415`) is a bounded, non-greedy match on
  meshforge's *own* exception strings. Confirmed.
- **The `MESHFORGE_*` executable overrides + PATH discovery** are user-controlled
  executable paths that get executed — this is documented, deliberate,
  single-user trust, and the framing still holds. Each `find_*` verifies
  `Path(candidate).is_file()` before returning.

---

## Remediation priority

| # | Severity | Finding | Effort | Fix |
|---|---|---|---|---|
| F1 | **Medium** | Group/patch-name injection into decks & foam dicts | Small | `valid_group_name()` at name-mint sites + writers; test |
| F2 | Low | Terminal escape injection via raw tool output in exceptions | Small | `_scrub()` control chars before embedding |
| F3 | Low | `dwg2dxf` has no timeout (hang on hostile DWG) | Trivial | add `timeout=120` + `TimeoutExpired` handling |
| F4 | Low | Image ingest lacks `MAX_VOXELS` guard | Trivial | reuse the existing constant in `volume_to_surface` |
| F5 | Info | `guard_output_path` exact-match-only; case writers unguarded | — | pass `input_paths` consistently; optional output-root assert |

**Recommended action:** fix F1 first (integrity + a real crafted-input path),
then bundle F2/F3/F4 (all tiny, all robustness). F5 is a note, not a task, until
a second input type appears. All fixes stay in the core/service layer
(rule-4-safe) and should be landed with tests + a `pytest` re-run against the
476-passed/2-skipped baseline. **This audit made no code changes.**

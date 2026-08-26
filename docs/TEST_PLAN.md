# Test plan

60 test modules under [`tests/`](../tests/). CI runs the ungated set on every
push/PR; the rest gate on an optional dependency, an external solver, or a
local clinical data directory, and skip cleanly when it is absent.

## Strategy

- **Ground truth is analytic, not golden.** Every geometric and physical
  quantity is asserted against a closed-form value on synthetic geometry —
  sphere/cylinder area and volume, exact swept-tube centrelines, Lamé thick
  cylinder, Euler buckling, Hagen–Poiseuille, Blasius friction, lumped
  capacitance, Timoshenko hoop stress. Tolerances reflect discretization, not
  hand-tuned slack. Where no closed form exists, the reference is a published
  benchmark (Ghia et al. 1982 lid-driven cavity) or a second independent solver
  (MYSTRAN vs CalculiX on the same deck).
- **Format-only exporters say so.** LS-DYNA and ANSYS decks have no solver
  validating them, so their tests are golden-card + exact internal re-parse and
  the emitted deck header states the limitation. They are never described as
  solver-verified.
- **Hard cases are explicit.** Open surfaces, non-manifold edges, empty files,
  wrong data types, degenerate sections — constructed on purpose, never softened
  to make a test pass (trap #2).
- **Every failure branch is exercised**, not just the happy path (trap #3).
- **Read-only inputs are proven**, not assumed: byte-for-byte checksum before
  and after every load/CLI run (hard rule 1).
- **Adversarial inputs are first-class.** Crafted `.cso`/`.vtp`/`.mfwork`/DICOM
  fixtures — always tiny and synthetic, never a clinical file — assert that a
  malicious input fails loudly rather than producing a plausible mesh. See
  [`SECURITY.md`](../.github/SECURITY.md).
- **Backend-gated tests skip cleanly** when an optional dep or solver is missing
  (trap #16), and are never allowed to silently become no-ops.
- **UI tests inject a stub viewport** and run steps synchronously; no real
  offscreen QtInteractor (trap #15 — segfaults can't be caught in-process).

## Coverage by area

| Area | Modules | Ground truth |
|---|---|---|
| I/O + provenance | `test_io`, `test_io_cso`, `test_image`, `test_cad`, `test_cad3d` | checksum invariance, analytic surfaces, real-DWG block extraction |
| Geometry | `test_centerline`, `test_frames`, `test_sections`, `test_branching`, `test_closedloop`, `test_primitives` | exact swept centrelines, holonomy-corrected frames, analytic radii, watertight primitives with exact truths |
| Meshing | `test_volmesh`, `test_blocks`, `test_ogrid`, `test_smooth`, `test_lumen`, `test_blayers`, `test_wall`, `test_thickness` | 0 inverted cells, sj_min invariants, do-no-harm smoothing guards |
| Quality + repair | `test_check`, `test_inspect`, `test_repair`, `test_grading`, `test_fixpack` | analytic area/volume, manifoldness, non-manifold reported not "fixed" |
| Solver decks | `test_deck`, `test_abaqus`, `test_nastran`, `test_lsdyna`, `test_ansys`, `test_export`, `test_foam`, `test_fluent` | golden cards + exact re-parse; foam owner/neighbour divergence-verified |
| FEA solve | `test_solve`, `test_fea_depth`, `test_buckle`, `test_riks`, `test_thermal_stress`, `test_film_transient`, `test_transient_thermal`, `test_radiate_kt`, `test_mystran` | Lamé, Euler column, rod spectrum, log T(r), αΔT expansion |
| CFD solve | `test_cfd`, `test_cavity`, `test_turbulence`, `test_transient` | Hagen–Poiseuille, Ghia Re=100 cavity, Blasius friction |
| Session + pipeline | `test_pipeline`, `test_project`, `test_history`, `test_recipes`, `test_synth`, `test_cli`, `test_cli_m2`, `test_cli_synth`, `test_integration` | journal/replay round-trips, recipe sweeps, CLI end-to-end |
| UI layers | `test_web_api`, `test_desktop_shell`, `test_rule4_scan` | route/frontend contract, rule-4 AST scan (no meshing logic in UI) |
| Security | `test_security_fixes`, `test_cso_security`, `test_hardening_a` | crafted-input refusal, injection payloads, allocation ceilings |

## What is gated, and on what

| Gate | Effect when absent | Modules affected |
|---|---|---|
| **CalculiX** (`find_ccx()`, `MESHFORGE_CCX`) | FEA *solve* tests skip; deck *format* tests still run | `test_solve`, `test_fea_depth`, `test_buckle`, `test_riks`, `test_thermal_stress`, `test_film_transient`, `test_transient_thermal`, `test_radiate_kt` |
| **OpenFOAM** (`MESHFORGE_OPENFOAM` / sourced env) | CFD *solve* tests skip; case-builder + polyMesh tests still run | `test_cfd`, `test_cavity`, `test_turbulence`, `test_transient`, `test_blayers` |
| **MYSTRAN** (`MESHFORGE_MYSTRAN`) | cross-solver check skips | `test_mystran` |
| **gmsh** (`[meshing]` extra) | tet-backend tests skip; structured-hex paths still run | `test_volmesh`, `test_lumen`, `test_cad3d`, `test_branching`, `test_closedloop`, `test_fixpack`, `test_abaqus`, `test_ansys`, `test_lsdyna` |
| **pydicom / nibabel** (`[image]` extra) | DICOM/NIfTI tests skip — and the *install-hint* branch is tested with them deliberately absent | `test_image` |
| **ezdxf** (`[cad]` extra) + `dwg2dxf` | DWG/DXF import tests skip | `test_cad` |
| **Local clinical data** (`MESHFORGE_CSO_FAMILY_DIR`, `MESHFORGE_DICOM_DIR`, or a git-ignored pointer file) | real-family regression skips; synthetic CSO coverage still runs | `test_io_cso`, `test_image` |

Clinical inputs live **outside** the repo and are addressed by pseudonymous study
code only; no path or filename is ever hard-coded (see `tests/conftest.py`).

## How to run

```bash
conda activate meshforge
python -m pytest -q                      # everything available on this machine
python -m pytest tests/test_solve.py -v  # one area
```

Plus the per-milestone end-to-end CLI run on a generated surface, pasted into
`CHECKPOINT.md` each session, and
[`scripts/e2e_all_features.sh`](../scripts/e2e_all_features.sh) for the full
feature sweep.

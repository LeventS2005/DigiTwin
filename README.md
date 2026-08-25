# meshforge

[![CI](https://github.com/LeventS2005/meshforge/actions/workflows/ci.yml/badge.svg)](https://github.com/LeventS2005/meshforge/actions/workflows/ci.yml)

Research-grade meshing toolkit: turns CAD drawings and closed/tubular
triangulated surfaces (.dwg/.dxf/.stl/.ply/.vtp) into analysis-ready, tagged
volume meshes and complete solver input decks (CalculiX-verified Abaqus
`.inp`). Scriptable pipeline with swappable backends; GUI-agnostic core.

## Feature map (vs TrueGrid's advertised set)

| TrueGrid feature | meshforge | Where |
|---|---|---|
| The Projection Method | proxy block meshes snapped onto curved profiles, exact corner capture, TFI re-blend, quality gate | `meshing/project.py`, `blocks --project` |
| Multi-block Structures | rectilinear block decomposition → structured quads (exact index-arithmetic edge merge) → tagged hexes | `meshing/blocks.py`, `blocks` |
| Pre-processing | full runnable decks: materials (elastic/hyperelastic), BCs, pressure/point loads, steps; Lamé benchmark solved in CalculiX to 2-3% | `export/deck.py`, `deck` |
| Parametric & Scripting | YAML recipes with cartesian parameter sweeps, per-variant manifests, failure isolation | `recipes.py`, `run` |
| Eliminate Clean-up (CAD) | DWG/DXF import: segment chaining, T-junction splitting, planar face extraction; validated on a real architectural DWG | `io/cad.py`, `import-cad` |
| Eliminate Clean-up (surfaces) | vertex merge, degenerate/duplicate faces, manifold-safe hole fill; non-manifold reported, never silently "fixed" | `quality/repair.py`, `repair` |
| Geometry Library | tubes (straight/curved/tapered/constricted/torus), box/cylinder/sphere/plate, loop extrusion, revolution — all with analytic truths | `synth/`, `synth` |
| Modification Simplicity | session journal → `.mfwork` work files → selective replay with overrides | `pipeline.py`, `replay` |
| Application Versatility | Y-junction trees: SDF-union synth, skeleton tree extraction, shell-pair tet walls; closed loops (torus) swept with holonomy-corrected frames | `synth/branching.py`, `geometry/tree.py` |

### Beyond TrueGrid (roadmap v3)

| Capability | meshforge | Where |
|---|---|---|
| In-app solve + results read-back | CalculiX runner (temp-dir staged, fail-loud) + `.frd`/`.dat` parsers; displacements/stresses attached to the session mesh as point data (`DISP`, `U_mag`, `S_mises`), Lamé-validated end to end | `solve/`, `solve`, GUI "solve" step |
| CFD mesh output (OpenFOAM) | `constant/polyMesh` writer: owner/neighbour extraction with outward winding (divergence-theorem verified), named patches from boundary groups, hard error on uncovered faces | `export/foam.py`, `foam`, `save("foam", …)` |
| NASTRAN decks | SOL 101 linear statics: GRID*/CHEXA/CTETRA, MAT1+PSOLID per region, SPC1/SPC, solid-face PLOAD4, FORCE — solve-level validated in MYSTRAN vs CalculiX + Lamé (PLOAD4 itself is unimplemented in MYSTRAN: format-level only; use `pressure_to_nodal_forces`) | `export/nastran.py`, `deck --target nastran`, `solve DECK.bdf` |
| 3-D CAD import (STEP/IGES/BREP) | gmsh OCC kernel → watertight triangulated surface + provenance; feeds the whole pipeline (STEP cylinder → swept wall, 0 inverted) | `io/cad3d.py`, `import-cad3d`, GUI "import_cad3d" |
| Elliptic smoothing | Winslow pass over block-mesh interiors (rims exactly fixed, area preserved) with a do-no-harm quality guard: sj_min can never regress — blended back or fully reverted, reported in the notes | `meshing/smooth.py`, `blocks --smooth` |
| Transition grading | symmetric geometric spacing within each grid interval (fine cells at every feature line); counts stay global so block conformity is exact | `blocks --grade-ratio` |
| Image ingest (CT/MRI path) | volume → iso-surface: .npy always, NIfTI/DICOM via lazy nibabel/pydicom (orientation-safe: rotated affines refused); level always explicit | `io/image.py`, `import-image`, GUI "import_image" |
| History / replay | live journal mirror (step/params/summary) + save-workfile button — exactly what a `.mfwork` replays | web Workbench history panel (`web/static/app.js`) |
| Boundary-layer mesher (CFD) | geometric wedge layers grown outward from a closed body surface (smoothed normals, body/outer patches); chains straight into polyMesh export | `meshing/prism.py`, `blayers`, GUI "blayers" |
| In-app CFD solve (OpenFOAM) | laminar case builder (simpleFoam/icoFoam: system/0 files from declarative patch BCs) + staged runner (OpenFOAM refuses paths with spaces) + pyvista read-back (`U`, `p`, `U_mag` on the mesh); Hagen–Poiseuille-benchmarked (centerline U, dp/dz, profile within a few %) | `solve/foam_case.py`, `solve/foam_runner.py`, `cfd`, GUI "cfd" step |
| Modal + heat FEA (CalculiX) | `*FREQUENCY` (eigenfrequencies parsed from the .dat) and steady `*HEAT TRANSFER` (temperature DOF-11 BCs, `*DFLUX` surface flux, NDTEMP read-back); exact truths: rod spectrum ≤0.4%, radial log T(r) ≤0.3% | `export/deck.py` step kinds, `deck --analysis`, `solve`, session "solve" knobs |
| O-grid disk layout | butterfly block layout for star-shaped loops (core square + 4 rings, rims exactly on the curve): sj_min 0.7071 invariant under refinement where the projected disk decays to its 0.05 gate; H-P pipe benchmark errors cut 2–8× | `meshing/ogrid.py`, `blocks --ogrid` |
| Turbulent CFD (kOmegaSST) | steady RANS case templates: 0/{k,omega,nut} from inlet intensity + mixing length, Spalding all-y+ wall function (probe-verified: the log-layer wall function is −21% on friction at buffer-layer y+); Blasius pipe-friction benchmark −0.7% at Re=1e4 | `solve/foam_case.py`, `cfd --turbulence kOmegaSST` |
| FLUENT .msh export | face-based ASCII mesh with named boundary zones (hex indices, c0/c1 orientation pinned against fluent3DMeshToFoam — the wrong pairing converts to negative volumes); per-cell divergence volumes recomputed from the written file match VTK exactly | `export/fluent.py`, `fluent`, `save("fluent", …)` |
| Transient CFD time series | every written time step read back as a frame (sorted/complete, one mesh, loud checks); per-frame .vtu persistence; non-commensurate icoFoam time knobs refused (they silently lose the end state); GUI slider/play field animation; startup pipe flow rises monotonically to the exact Hagen–Poiseuille profile | `solve/foam_runner.py read_case_series`, `cfd --series`, session "series" knob, GUI animation panel |
| Closed-cavity CFD | sealed domains with the pressure level pinned by pRefCell/pRefValue (silently-open cavities stay loudly refused); moving-wall + 2-D `empty` BC kinds; patch splitting by face-centroid coordinate (`split_patch`); Ghia et al. (1982) Re=100 lid-driven cavity within 0.0085 of lid speed at 64² | `cfd --moving-wall --empty --closed-cavity`, `meshing/base.py split_boundary_group`, session "split_patch" step |
| Transient heat + film convection (CalculiX) | `*FILM` convection expanded element-wise (ccx refuses named surfaces on that card) and transient `*HEAT TRANSFER, DIRECT` with `*INITIAL CONDITIONS`; per-step temperatures read back from every 100CL frd block (`time_steps`); exact truths: steady film tube +0.34% of ΔT, lumped-capacitance decay 0.19% of ΔT at dt≪τ (ccx integrates backward Euler — dt is the accuracy dial) | `deck --film --transient --dt --period --t0`, `FrdModel.history`, session film/transient knobs |
| Buckling + preloaded modes (CalculiX) | `*BUCKLE` factors parsed from the .dat (factor × load = critical load; trap: too few requested factors makes ccx silently skip the lowest modes) and `*STATIC`→`*FREQUENCY, PERTURBATION` preloaded modes; exact truths: fixed-free Euler column +0.43% with reduced integration (full C3D8 locks +12%, C3D8I is broken under *BUCKLE), preload frequency shift within 1.2% of √(1−P/P_cr) | `deck --analysis buckle --preloaded`, `parse_buckling_factors`, session buckle/preloaded knobs |
| LS-DYNA keyword deck export | fixed small-format .k decks (*NODE/*ELEMENT_SOLID with collapsed-hex tets/*PART per region/*SECTION_SOLID ELFORM 1/2 hex, 10/16 tet/*MAT_ELASTIC/*BOUNDARY_SPC_NODE/unit-ramp *DEFINE_CURVE/*LOAD_NODE_POINT/*LOAD_SEGMENT wound outward for *DSLOAD sign parity); **format-level only — no LS-DYNA solver validated it** (golden cards + exact internal re-parse; the deck header says so) | `export/lsdyna.py`, `deck --target lsdyna` |
| ANSYS MAPDL .cdb deck export | CDWRITE,DB-style archives (NBLOCK `(3i9,6e20.13)`/EBLOCK `(19i9)` with degenerate-SOLID185 tets I J K K M M M M/KEYOPT(2) 0-full 1-reduced/CMBLOCK region+boundary components/MP material/per-node D with native nonzero enforced displacement/F/per-face SFE PRES from the SOLID185 face table); **format-level only — no ANSYS solver validated it** (golden cards + exact internal re-parse; ANSYS's own `ansys-mapdl-reader` parsed the emitted decks exactly in the V44 probe; the deck header says so) | `export/ansys.py`, `deck --target ansys` |
| Thermal stress — coupled heat→static (CalculiX) | sequential one-way coupling: a heat solve's NDTEMP field re-emitted as per-node `*TEMPERATURE` in a static step with `*EXPANSION` and an explicit `*INITIAL CONDITIONS` stress-free reference (REQUIRED — ccx silently ignores `*EXPANSION ZERO=` and a wrong datum is silently wrong stress; α(T) tables refused: ccx anchors their secant at numeric temperature zero, so the identical physics changes with the temperature scale — all V45-probe-verified); exact truths: free bar ε=αΔT, constrained bar σ=−EαΔT (integration-point exact), the V24 log-T(r) tube converging onto the Timoshenko hoop-stress closed form under radial refinement | `deck --expansion --temperature/--temperature-from --t-ref` (+ `--temperature-time` to pick one snapshot of a TRANSIENT heat history — V46, exact-match only against the frd's own recorded times), session `expansion`/`temperature`/`temperature_from`/`temperature_time`/`t_ref` knobs |
| Lumen (blood-volume) fill | fills the INSIDE of a tubular surface for cardiac FEA/CFD: swept O-grid (butterfly) hexahedra along the validated centreline with a pinned per-station block topology + a do-no-harm guarded smoothing pass (sj_min can never regress), automatic gmsh tet fallback with a loud fold-number note (F = r·κ — a planar-section sweep MUST fold where F ≥ 1) when a section defeats the O-grid; the mesh takes the session volmesh slot with `wall`/`inlet`/`outlet` groups so deck/solve/CFD work unchanged; all 20 real cardiac arches (both UCSF families) reach inverted 0 — 19 structured hex, 1 honest tet | `meshing/lumen.py`, `lumen`, session "lumen" step (V47) |

## Install
```bash
conda env create -f environment.yml --solver=libmamba
conda activate meshforge
```
(pip fallback: `pip install -r requirements.txt -e .`; extras: `[meshing]`
gmsh, `[cad]` ezdxf, `[recipes]` pyyaml, `[image]` pydicom/nibabel (DICOM +
NIfTI ingest), `[web]` FastAPI/uvicorn, `[viewport]` + `[desktop]` conda-forge
only on macOS (the web viewport + the native shell).
DWG import needs `dwg2dxf` from GNU LibreDWG, e.g. `brew install libredwg`.
CI runs the ungated suite on every push/PR — solver-gated tests need local
CalculiX/OpenFOAM/MYSTRAN installs.)

### Or: build a self-contained installer — no conda needed (macOS arm64 / Linux / Windows)

> **There are no published GitHub releases yet, so there is no installer to
> download** — you build it from this repo. Everything below describes that
> build, not a download link.

```bash
bash packaging/build.sh      # macOS arm64 -> dist/meshforge-1.0.0-MacOSX-arm64.pkg (+ .sh)
```

Double-click the resulting **`.pkg`** → the wizard lays down a self-contained
environment and a **meshforge** app; launch it to open the Workbench (a native
window over the local 127.0.0.1 web UI). See
[`packaging/README.md`](packaging/README.md) for the full recipe.
The FEA/CFD **solvers stay external and optional** (CalculiX/OpenFOAM/MYSTRAN,
auto-discovered at runtime — not bundled); meshing, export, and the 3-D viewport
work without them. The installer is unsigned — right-click → *Open* past
Gatekeeper. **Linux** (`.sh`) and **Windows** (`.exe`) installers build from
the same recipe via the manual-dispatch **"Linux installer"** / **"Windows
installer"** GitHub Actions workflows (constructor can't cross-build; the
unsigned `.exe` triggers a SmartScreen warning — "More info" → "Run anyway";
details in [`packaging/README.md`](packaging/README.md)).

## Use
```bash
meshforge import-cad drawing.dwg --list-blocks    # CAD → cleaned profile
meshforge blocks outputs/drawing_profile.json     # profile → hex mesh
meshforge synth y_junction                        # synthetic surfaces + truths
meshforge volmesh outputs/torus.vtp --thickness 0.4
meshforge deck tube.vtp --thickness 1 --E 1000 --nu 0.3 \
    --fix inlet,3 --fix outlet,3 --pin-lateral --pressure inner,1 --target ccx
meshforge solve outputs/tube_ccx.inp              # run CalculiX, read results back
meshforge foam tube.vtp --thickness 1 --case outputs/tube_case   # OpenFOAM polyMesh
meshforge cfd outputs/tube_case --nu 0.1 \
    --inlet inlet=0,0,1 --outlet outlet=0         # run OpenFOAM, read U/p back
meshforge run study.yaml                          # parameter sweep
meshforge repair damaged.stl                      # surface clean-up
meshforge replay outputs/session.mfwork --set volmesh.thickness=0.6
meshforge web                                     # web Workbench (browser, 127.0.0.1)
meshforge desktop                                 # same Workbench in a native window
python -m pytest                                  # test suite
```

## Clinical data

**No patient data lives in this repository, and none ever has.** The cardiac
`.cso` / DICOM inputs the pipeline was validated against are read-only files
kept *outside* the repo tree; they are checksummed on sight and never copied in
or committed (`CLAUDE.md` rule 1). Cases are referred to only by pseudonymous
study code (`VAM1`, `VAM997`, …) — never by a filename, because segmentation
filenames carry patient initials and scan dates. The gated family tests locate
that folder via `MESHFORGE_CSO_FAMILY_DIR` (or a git-ignored
`tests/.local_family_dir`) and skip cleanly when it is absent, which is what CI
does.

meshforge is research-grade software: **not a medical device**, not cleared or
approved by any regulatory body, and not for diagnosis or treatment.

## License

Apache License 2.0 — see [`LICENSE`](LICENSE) and [`NOTICE`](NOTICE). You may
use, modify, and redistribute it, including commercially, provided you keep the
copyright and license notices and state your changes.

## Security

Local, single-user, offline tool: the trust boundary is the **files you open**
and the **output of external solvers**, both treated as untrusted and required
to fail loudly rather than silently produce a plausible mesh. Threat model,
private reporting route, and the two written audits (with the `file:line`
closing every finding) are in [`SECURITY.md`](SECURITY.md) and
[`docs/security/`](docs/security/).

## Project docs
- **`docs/TUTORIAL.md`** — first-run tutorial (install → mesh → solve → replay) · **`docs/APP_GUIDE.md`** — complete usage reference
- [`CONTRIBUTING.md`](CONTRIBUTING.md) — environment, tests, architecture rules · [`CHANGELOG.md`](CHANGELOG.md) — release history
- [`SECURITY.md`](SECURITY.md) — threat model + reporting · [`docs/security/`](docs/security/) — audit reports
- `AGENT_GROUNDUP_PROMPT.md` — v1 build spec · `docs/specs/V2_ROADMAP.md` — TrueGrid-parity roadmap
- `CLAUDE.md` — operational contract (hard rules, session protocol)
- `DECISIONS.md` — decision log (per-milestone details in `docs/decisions/`)
- `BACKLOG.md` · `TEST_PLAN.md` · `docs/specs/` — per-milestone specs · [`CITATION.cff`](CITATION.cff) — how to cite

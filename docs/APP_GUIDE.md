# meshforge — complete usage guide

This is the DETAILED reference for driving meshforge — every step panel,
knob, subcommand, file location, and known quirk. It is deliberately
exhaustive rather than friendly; for a guided first run see
**[TUTORIAL.md](TUTORIAL.md)**. The feature set is roadmap v6 (solvers,
CFD, exporters); the UI is the web/desktop **Workbench** (roadmap
V35–V35b — one web UI codebase, in a browser or a native window).

---

## 1. What you are driving

meshforge turns a closed triangulated surface (or a 2-D CAD profile,
or a 3-D CAD solid, or a medical image volume) into an analysis-ready
volume mesh, writes complete solver input decks, and can RUN the
analysis in-app and show the results.

Architecture (this ordering explains every error message you'll see):

```
core (src/meshforge/…)   pure logic: geometry, meshing, export, solve
  └── MeshingSession     the service layer: ordered steps + journal
        ├── CLI           `meshforge <subcommand>` — one step per call
        └── Workbench     `meshforge web` / `meshforge desktop` — step
                          panels over the SAME steps (a browser tab or a
                          native window; one web UI codebase)
```

The UI contains ZERO meshing logic (enforced by tests). Every button
calls the same session step the CLI calls. Steps have PREREQUISITES:
running one before its input exists fails loudly (that is the
`ERROR: RuntimeError: no centerline available — run compute_centerline
first` you see in the log pane — run the step panels top-to-bottom).

All generated files land under `outputs/` RELATIVE TO THE DIRECTORY
YOU LAUNCHED FROM. Always start from the project folder.

---

## 2. Launching

```bash
cd /path/to/meshforge   # quote the path if it contains spaces
conda activate meshforge
meshforge web          # the Workbench in your browser (prints a 127.0.0.1 /app/ URL)
meshforge desktop      # the same Workbench in a native window
meshforge --help       # the CLI
```

`meshforge web` binds **127.0.0.1 only** (an OS-assigned free port unless
you pass `--port N`) and prints the URL to open, e.g.
`Workbench: http://127.0.0.1:PORT/app/`. `meshforge desktop` embeds that
same page in a QtWebEngine window (needs the `web` + `viewport` +
`desktop` extras and the conda-forge `qt6-webengine` package). Both run a
single web UI codebase — the old PySide6 knob panels were retired;
**`meshforge-gui` remains as a back-compat alias for `meshforge desktop`.**

No-activation one-liner:
`~/miniconda3/envs/meshforge/bin/meshforge web`

**No conda at all?** Build a self-contained macOS installer with
`bash packaging/build.sh` (→ `dist/meshforge-<ver>-MacOSX-arm64.pkg`); the user
double-clicks it, then launches the **meshforge** app — the Workbench opens with
no conda knowledge required. Solvers stay external/optional (see
`packaging/README.md`).

Solvers are auto-found: CalculiX (conda env `ccxsolve`), OpenFOAM
(brew launcher `openfoam` → v2512; `openfoam2506` kept as rollback),
MYSTRAN (conda env `mystran`). Overrides via env vars `MESHFORGE_CCX`,
`MESHFORGE_OPENFOAM`, `MESHFORGE_MYSTRAN` (each an executable path).

---

## 3. The Workbench, pane by pane

Layout — three panes:
- **Left — steps:** the **source** box + one **step panel** per pipeline
  step (built live from `/schema`), each with its knobs and a **Run …**
  button. The column scrolls; run it top-to-bottom.
- **Centre — viewport:** the interactive **3-D view** (orbit with the
  mouse), a **color by** field selector + colorbar, a CFD **play/slider**
  strip, and a **summary** line under the canvas. After an **import_cso**
  the toolbar also shows a **contours toggle** that draws the raw source
  rings (amber polylines) over the reconstructed surface — a visual QC of
  the loft against what was segmented.
- The Workbench **auto-refreshes**: it polls the session revision every
  couple of seconds, so when another client (a second tab, the desktop
  window, or a script driving the same server) changes the session, the
  view re-syncs itself — no manual ⌘R needed.
- **Right — log & history:** the **log**, the **history** table (the
  journal — it mirrors exactly what a `.mfwork` file replays; failed steps
  never enter it), and the **Save / Load work file** + **Reset session**
  actions.

The top bar shows the session **state**: the input path and an artifact
chip per step (`surface`, `centerline`, `wall`, `volmesh`, `solve`, …)
that lights up as it completes.

### 3.1 source
- **surface path + Load surface** — loads a TRIANGULATED 3-D SURFACE file
  by path: anything pyvista reads as PolyData (`.vtp`, `.stl`, `.ply`,
  `.obj`, `.vtk`). It rejects volume grids and point clouds loudly. (For a
  2-D `.dwg`/`.dxf` drawing use the **import_cad** step panel instead —
  §3.6 / §5 — which turns it into a profile for `blocks`.)
- **shape dropdown + Generate synth** — creates a synthetic surface
  with known ground truth. Shapes: `straight_tube`, `curved_tube`,
  `tapered_tube`, `constricted_tube`, `torus` (generators with
  analytic centerlines) and the primitives `box`, `solid_cylinder`,
  `solid_sphere`, `plate_with_hole`. The result is the session's
  active surface; subsequent step panels operate on it.

### 3.2 centerline  (needs: a loaded/generated surface)
- `voxel_size` — voxelization pitch for the distance field (blank =
  auto). Smaller = slower + more detail.
- `smoothing` — centerline smoothing passes (blank = default).
- `n_samples` — resampled points along the centerline (default 200).
- **Run centerline** → log reports samples, covered length, radius
  range, closed-loop flag. Caps are auto-extended (V4).

### 3.3 sections  (needs: centerline)
- `every` — evaluate a cross-section every N centerline samples.
- `rel_drop` — relative area drop that counts as a constriction
  (0.2 = flag ≥20% narrowing vs the local envelope).
- **Run sections** → log reports plane count, area range, constrictions.

### 3.4 wall  (needs: centerline; this makes the OUTER surface)
- `thickness` — uniform wall thickness. Mutually exclusive with:
- `outer_surface` — path to a measured outer surface (thickness is
  then sampled from it). Give exactly ONE of the two — both/neither
  is a loud error.
- `mode` — `sdf` (default: signed-distance offset, robust on bends) or
  normal-offset modes.
- `curvature_cap` / `floor_frac` — per-node thickness cap/floor on
  tight bends; the log reports how many nodes were capped/floored.
- **Run wall** → log reports thickness range + the sdf grid note.

### 3.5 volmesh  (needs: wall — OR skip 3.2–3.4 and use blocks, §5)
- `backend` — `swept` (structured hex: θ × axial × radial layers) or
  `tet` (gmsh tetrahedra; needs the gmsh extra).
- `thickness` — wall thickness for the volume mesh (same precedence
  rules as §3.4).
- `n_theta`, `n_axial`, `n_layers` — swept lattice resolution.
- `layer_ratio` — geometric grading of the radial layers (1.0 =
  uniform).
- **Run volmesh** → log reports cells, `inverted` (MUST be 0),
  `sj_min` (scaled Jacobian, quality floor), volume, and the named
  boundary groups (`inner/outer/inlet/outlet` for tubes) — these
  group names are what BCs and loads reference later.

### 3.5b lumen  (needs: a tubular surface; fills the INSIDE, V47)
Where `volmesh` meshes the vessel WALL, `lumen` fills the **blood
volume** — the domain cardiac CFD actually solves on.
- `method` — `auto` (default: swept O-grid hexahedra, automatic gmsh
  tet fallback with a loud note when the structured fill fails),
  `ogrid` (force structured, loud failure), `tet` (force tetrahedra).
- `n_theta` / `n_axial` — rays per station / stations along the
  centreline (the validated centreline embedded by `import_cso` is
  reused automatically).
- `smooth` — guarded smoothing iterations; the do-no-harm guard means
  `sj_min` can never regress (0 = off).
- `core_fraction`, `mesh_size` — O-grid core size; tet target size.
- **Run lumen** → the result REPLACES the session volume mesh (groups
  `wall`/`inlet`/`outlet`), so solve/cfd/deck/save run on it unchanged.
  The log always reports the fold number F = r·κ (a planar-section
  sweep must fold where F ≥ 1 — that is what pushes the most eccentric
  sections to honest tets).

### 3.6 import_cad (2-D DWG/DXF profile), import_cad3d (STEP/IGES/BREP),
### import_image (.npy/NIfTI/DICOM), import_cso (MeVisLab contours)
Step panels exist for all four (path + format knobs); they set the
session SOURCE (instead of Load surface). `import_cad` produces a 2-D
profile consumed by `blocks` (§5); `import_cad3d` meshes a CAD solid's
surface; `import_image` runs marching cubes at the required `level`;
`import_cso` turns a MeVisLab Binary CSO contour stack into a watertight
surface (methods `auto`/`loft`/`voxel`, open-contour policy, optional
DICOM anchoring) — **full walkthrough: [CSO_GUIDE.md](CSO_GUIDE.md)**.
In the desktop window every import path knob has a **Browse…** button.

### 3.7 blocks  (2-D profile → multi-block hex, the TrueGrid-style path)
- `target_size` — in-plane cell size; `thickness`/`n_layers` — extrusion.
- `holes_like` — "WxH" filter to treat matching components as holes.
- `project` — snap rims onto curved loops (projection method).
- `ogrid` + `core_fraction` — butterfly layout for disk-like loops.
- `smooth` — Winslow smoothing (quality-guarded: sj can never regress).
- `grade_ratio` — element-density transition grading.

### 3.8 repair / blayers
- `repair`: `merge_tol`, `max_hole_edges` — vertex merge, degenerate
  removal, manifold-safe hole fill (non-manifold is NEVER auto-fixed).
- `blayers`: `first`, `n_layers`, `growth`, `normal_smoothing` —
  external-flow boundary-layer wedges grown outward from a closed body.

### 3.9 solve  (CalculiX FEA — needs: volmesh, or an external deck)
Knobs: `deck` (external .inp — leave empty to build from the session
mesh), `analysis` = `static` | `frequency` | `heat` | `buckle`,
`preloaded` (1 = *STATIC preload then perturbation frequencies),
`E`, `nu`, `density`, `n_modes`, `conductivity`,
`temp` ("inner=100,outer=20"), `flux` ("outer=5"),
`film` ("outer=20,0.5" — NAME=SINK,H convection), `specific_heat`,
`transient` (1 = time stepping; needs `dt`, `period`, `t0`),
`pressure` ("inner=1.0"), `fix` ("inlet:3,outlet:3" — bare NAME clamps
DOFs 1–3; NALL valid for frequency), `pin_lateral`, `nlgeom`, `ccx`,
`timeout`. Results attach to the mesh (U_mag/S_mises/NDTEMP) and the
log prints |U| max, frequencies, T range, time steps, buckling factors.

### 3.10 cfd  (OpenFOAM — needs: volmesh saved as a foam case, or `case`)
Knobs: `case`, `solver` (`simpleFoam` steady / `icoFoam` transient),
`nu`, `inlet` ("front=0,0,1"), `outlet` ("back=0"), `end_time`,
`delta_t`, `write_interval`, `timeout`, `turbulence`
(`laminar`/`kOmegaSST`), `intensity`, `turb_length` (REQUIRED for
kOmegaSST, hint 0.07·D), `series` (1 = read every written time step
back as animation frames), `moving_wall` ("lid=1,0,0"), `empty`
("front,back"), `closed_cavity` + `p_ref_cell` + `p_ref_value`
(sealed domains). With `series=1` the viewport's **play/slider** arms —
step the U/p fields through time.

### 3.11 split_patch / screenshot / replay / recipe
- `split_patch`: carve a new boundary group (e.g. a cavity lid) from
  an existing one by face-centroid coordinate: `group`, `axis`,
  `threshold`, `name`, `side`.
- `screenshot`: offscreen PNG of the current scene (headless — no
  display needed): `path` (default outputs/session.png).
- `replay`: re-run a `.mfwork` work file with optional
  `STEP.KNOB=VALUE` override — only steps from the earliest override
  onward are recomputed.
- `recipe`: run a YAML recipe (§7).

### 3.12 History table + Save work file…
The right-pane **history** table lists journaled steps (step | params | summary).
"Save work file…" writes a `.mfwork` (JSON) capturing the journal;
`replay` or `meshforge replay` reproduces the session exactly.

---

## 4. The step order that always works (Workbench quick path)

1. source: pick `straight_tube` → **Generate synth**.
2. **Run centerline** → 3. **Run sections** (optional) →
4. **Run wall** (or skip) → 5. **Run volmesh** (`inverted=0` or stop).
6. Analyze: the **solve** panel (e.g. analysis=`static`, E=1000, nu=0.3,
   fix=`inlet:1-3,outlet:1-3`, pressure=`inner=1.0`) — or the **cfd** panel.
7. **screenshot** / **Save work file**.

Running a step panel before its prerequisite = the loud RuntimeError in
the log; nothing breaks — run the missing step and retry.

---

## 5. The DWG path (Cottage_Pane_Window.dwg)

A 2-D `.dwg`/`.dxf` drawing becomes a profile via **import_cad**, which
`blocks` then extrudes into hexes. The CLI path (also what the Workbench
runs under the hood):

```bash
meshforge import-cad Cottage_Pane_Window.dwg            # → outputs/Cottage_Pane_Window_profile.json
meshforge blocks outputs/Cottage_Pane_Window_profile.json \
    --holes-like 177.03x170.68 --no-component-holes \
    --target-size 40 --thickness 40 --n-layers 2        # 1220 hexes, 0 inverted
```

In the Workbench, use the **import_cad** step panel (type the path) then
**Run blocks**. Needs `dwg2dxf` (brew, installed) and ezdxf. The DWG is opened
READ-ONLY and checksummed on every load.

---

## 6. CLI reference (one canonical example each)

| Command | Does | Example |
|---|---|---|
| `inspect` | surface report (watertight, volume, area) | `meshforge inspect outputs/straight_tube.vtp` |
| `check` | mesh-readiness gate (exit 1 if not ready) | `meshforge check outputs/straight_tube.vtp` |
| `export` | convert surface (vtu/vtk/ply/obj) | `meshforge export s.vtp --format ply` |
| `synth` | generate shape + truth JSON | `meshforge synth curved_tube --set depth=0.4` |
| `repair` | fix a damaged surface | `meshforge repair broken.vtp` |
| `import-cad` | DWG/DXF → 2-D profile JSON | §5 |
| `import-cad3d` | STEP/IGES/BREP → surface | `meshforge import-cad3d part.step` |
| `import-image` | volume → iso-surface | `meshforge import-image vol.npy --level 0` |
| `import-cso` | MeVisLab contours → surface | `meshforge import-cso scan.cso --outputs outputs/mycase` (see [CSO_GUIDE.md](CSO_GUIDE.md)) |
| `blocks` | profile → multi-block hexes | §5; add `--project`, `--ogrid`, `--smooth`, `--grade-ratio` |
| `volmesh` | surface → wall volume mesh | `meshforge volmesh tube.vtp --thickness 0.5` |
| `lumen` | surface → blood-volume mesh (V47) | `meshforge lumen surface.vtp` (O-grid hexes, tet fallback; add `--method`, `--n-theta`, `--n-axial`, `--smooth 0`) |
| `blayers` | external boundary layers | `meshforge blayers body.vtp --first 0.1 --n-layers 5 --growth 1.3` |
| `deck` | full solver input deck | `meshforge deck tube.vtp --thickness 1 --material elastic --E 1000 --nu 0.3 --fix inlet,3 --pressure inner,1 --target ccx` (targets: abaqus, ccx, nastran, lsdyna, ansys; add `--analysis frequency/heat/buckle`, `--transient --dt --period --t0`, `--film outer,20,0.5`, `--preloaded`, `--formulation reduced`; thermal stress V45: `--expansion 1e-5 --t-ref 20` + `--temperature 120` or `--temperature-from heat.frd` chaining a solved heat run; V46: `--temperature-time 5.0` picks one snapshot of a TRANSIENT heat history — must match one of the frd's own recorded times, no interpolation) |
| `solve` | run ccx (.inp) or MYSTRAN (.bdf) | `meshforge solve outputs/tube_ccx.inp` (`.k`/`.cdb` refuse loudly — no LS-DYNA/ANSYS solver) |
| `foam` | mesh → OpenFOAM polyMesh case | `meshforge foam tube.vtp --thickness 1 --case outputs/tube_case` |
| `cfd` | run OpenFOAM on a case | `meshforge cfd outputs/tube_case --nu 0.1 --inlet inlet=0,0,1 --outlet outlet=0` (+ `--series`, `--turbulence kOmegaSST --turb-length`, `--moving-wall --empty --closed-cavity`) |
| `fluent` | mesh → FLUENT .msh | `meshforge fluent tube.vtp --thickness 1` |
| `run` | YAML recipe with sweeps | `meshforge run recipe.yaml --set wall.thickness=0.4` |
| `replay` | re-run a work file | `meshforge replay s.mfwork --set volmesh.n_theta=48` |
| `web` | serve the Workbench (browser) | `meshforge web` (add `--port N`) — prints a 127.0.0.1 `/app/` URL |
| `desktop` | the Workbench in a native window | `meshforge desktop` (needs the `web`+`viewport`+`desktop` extras) |

Every command exits nonzero on failure and prints WHY (fail-loud rule).

---

## 7. Recipes (batch/parametric)

```yaml
steps:
  - step: generate_synth
    shape: straight_tube
  - step: compute_centerline
  - step: compute_volmesh
    thickness: {sweep: [0.3, 0.5]}
    n_theta:   {sweep: [16, 32]}
  - step: save
    format: vtu
```
`meshforge run recipe.yaml` → cartesian product (4 variants) under
`outputs/<recipe>/<slug>/` + `manifest.json` (params, status, quality;
one variant failing never kills the batch).

---

## 8. Where things land

- `outputs/` — every artifact (git-ignored). Solve: `outputs/solve/
  <deck>/…_results.vtu` + `…_solve.json`. CFD: `outputs/cfd/<case>/…`
  (+ per-frame `…_t0000.vtu` with `--series`).
- Work files: wherever you save them (`.mfwork`, JSON).
- Nothing is ever written next to or over an input (guarded).

## 9. Troubleshooting (the exact messages)

| Symptom | Meaning / fix |
|---|---|
| `no centerline available — run compute_centerline first` | Step panels ran out of order — run the missing step; nothing is broken. |
| Viewport says "3-D viewport unavailable" / a mesh won't render | The optional `viewport` extra is missing — install `trame-vtk` (conda-forge). The pipeline still runs; only the in-browser render needs it. |
| `meshforge web` won't start / port already in use | Pass an explicit free port: `meshforge web --port 8791`. It binds `127.0.0.1` only. |
| `refusing to solve a mesh with N inverted cell(s)` | Mesh quality gate — coarsen/adjust knobs until `inverted=0`. |
| `ccx/OpenFOAM/MYSTRAN not found` | Check the conda envs / brew launchers or set the `MESHFORGE_*` env var. |
| OpenFOAM dies about paths | It cannot run under paths with spaces — meshforge stages to a temp dir automatically; if you drive OpenFOAM by hand, do the same. |
| `--thickness and --outer-surface are mutually exclusive` | Give exactly one thickness source. |
| GUI froze during a solve | Long solves run on a worker thread; check the log — `timeout` knob caps runaway solves. |

## 10. Honest limits (as of V44)

Format-level only: LS-DYNA .k and ANSYS MAPDL .cdb (no solver here —
golden cards + exact internal re-parse are the evidence, and the deck
headers say so), NASTRAN PLOAD4 under MYSTRAN (use nodal forces instead —
automatic in the session).
Not wired: temperature-dependent film h (ccx needs the `film.f` user
subroutine — use the k(T) conductivity table instead), thermo-mechanical
coupling, MARC decks. Radiation (`*RADIATE` + physical constants,
V41) and Riks post-buckling (`*STATIC, RIKS`, V42 — read the true load
from the reaction force, not the frd "time") ARE wired and
solve-validated. DICOM ingest is validated on real GE axial cardiac CT
(multi-frame single-file, V39); oblique/enhanced/non-GE variants are
refused loudly. Transient heat accuracy is first-order in dt (backward
Euler) — dt is the accuracy dial.

# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Demo freeze (M6) for the copilot's twin card and report.
  - **Workbench twin panel:** the report's "your twin" card, live in the right
    pane (`GET /twin`). Every slot waits ("not run yet") until its step lands,
    then fills from the session's own journal, replayed through the copilot's
    tool code, so it quotes the copilot's exact numbers. A failed wall gate
    shows mid-run, a new source empties the card, and a re-run crop clears the
    stress it made stale.
  - **`report.html` is a real page:** header, twin card (with each slot's
    numbers), headline tiles, results and pipeline tables, the numbers-guard
    note, the investigational-index caveat and the data credit. It has no
    script and no web fetch, prints cleanly and fits a phone. Every string
    from a tool, Claude or the command line is HTML-escaped. `report.md` is
    unchanged.
  - **No local folder on screen:** the Workbench history, summary and log
    show a path by its file name and `/Users/<name>` as `~`. The copilot's
    `transcript.json` names the CT by its file name, its printed paths are
    relative to the working folder, and the report's Reproduce line names the
    work file only. Replay still reads the work file's real paths.
  - **Video-figure scripts** (`scripts/m6_*.py`): wall shear stress from an
    OpenFOAM case, the Raghavan–Vorp law vs the stress step's stiff-linear
    wall, the s1364 stress stills, the normal-aorta strength bands (USP
    dataset, doi 10.17632/yfj4wfszbw.4, CC BY 4.0) beside the AAA band, and
    one command that rebuilds every figure and its index from outputs.
- `meshforge copilot "<request>" --ct <scan>` (optional extra `copilot`) is
  an investigational Claude copilot.
  - **What it runs:** segment → sac → crop → wall mesh with its quality gate →
    wall stress with thrombus → strength index.
  - **Recovery:** when a gate fails it reads the error and recovers, for
    example re-cropping with parallel caps.
  - **Report:** built only from tool outputs and opening with a "your twin"
    card: the aorta as module 1 and the data layers as slots. Claude's
    summary is included only if every number in it matches a tool output
    (one correction is allowed, otherwise it is withheld).
  - **Live view:** `--server <url>` runs the steps on a live Workbench so its
    viewport follows along.
  - **Model and record:** Claude Opus 5.5 with adaptive thinking and a
    server-side refusal fallback. Every action lands in the transcript and in
    a replayable work file.
- `segment --mask_out` (step knob), `crop --parallel_caps`, and `aaa_stress`
  using the session's cropped surface as the sac when only `lumen` is given.

- `aaa-stress --lumen <cropped lumen> --sac <the cropped sac the wall was built from>` (with `--crop-report`): models
  the intraluminal thrombus as a solid.
  - **Geometry:** the region between the blood lumen and the wall is filled
    with quadratic tets (C3D10) and tied to the wall's inner faces.
  - **Loads and material:** the blood pressure acts on the lumen. The thrombus
    ends are free. The default thrombus modulus is 5 MPa against the wall's
    100 MPa, as in the Zenodo AAA dataset's FE model (`--E-ilt`).
  - **Two-layer cylinder check:** the wall hoop stress matches plane-strain
    Lamé within 0.7%.
  - **On a native AAA**, peak wall stress at 140 mmHg drops from 674 kPa
    (wall only) to 287 kPa, inside the published 220–330 kPa range.
  - **Against the Zenodo AAA dataset's published FE result** (12 cases, which
    model thrombus too), the median tension rank correlation is 0.95 and the
    median p99 ratio 1.14 (wall only: 0.70 and 1.42).
  - **The assumptions it prints** follow the model ("thrombus modelled" vs
    "wall stress is overstated").

- `lumen --method surface-tet`, and as the last `auto` fallback: tet-fills
  the cropped surface itself, after a gmsh reparametrize-and-remesh, so it
  needs no centreline rings.
  - On a real AAA lumen whose centreline bends tighter than the vessel,
    both the O-grid and the station-ring tet fill failed; this one gives
    0 inverted cells.
  - The remesh removes the needle triangles of a marching-cubes surface.
    Filled as-is they became sliver tets (scaled Jacobian about 1e-4), and
    simpleFoam diverged on them.
  - Inlet and outlet faces are tagged by lying on their cap plane.

- `aaa_stress` step and `meshforge aaa-stress` — an investigational wall
  stress for an abdominal aortic aneurysm under blood pressure. Stiff-linear
  CalculiX solves on the loaded (imaged) geometry of a swept hex wall: uniform
  pressure on the lumen side, both ends clamped, E 100 MPa, ν 0.49, the hexes
  solved as C3D20R. Two real solves (systolic, default 140 mmHg, and a
  what-if, default 120 mmHg). Reports the peak wall stress (99th percentile of
  the integration-point von Mises outside the clamped end zones), the max
  principal p99, the max, the peak location (height above the distal cap) and
  the wall tension (N/mm, ~independent of the assumed thickness). Writes a
  results `.vtu` (point and cell fields, end zones flagged) and a summary
  JSON, and always prints its assumptions (thickness assumed 1.5 mm, no
  thrombus modelled, stiff-linear, investigational). Refuses — no number, the
  reason printed, exit 1 — a wall with inverted cells, capped or floored wall
  rays, a missing mesh summary, or end zones that leave under 10% of the wall.
  Input: a wall `.vtu` with its `.summary.json` (boundary groups are recovered
  from the swept lattice), or a `.mfwork` session.
  The summary JSON holds the peak wall stress once, at the top level:
  `pws_mpa` (systolic von Mises 99th percentile, Cauchy, MPa; `null` when
  refused) with `pws_measure` and `pws_pressure_mmhg`; each pressure's block
  has `von_mises_p99_mpa`.
- `read_frd(..., elements=False)` / `solve_deck(..., read_elements=False)`:
  read only the nodal fields of a result whose element type has no viewer
  mapping (the quadratic AAA wall).
- `scripts/m3_stress_maps.py` (off-screen stress-map stills) and
  `scripts/m3_tension_validation.py` (our tension vs the Zenodo AAA dataset's
  published FE result, doi 10.5281/zenodo.19182977, CC BY 4.0).

### Changed

- Public tree slimmed to what someone *using* meshforge needs. The agent and
  session machinery (`CLAUDE.md`, `AGENT_GROUNDUP_PROMPT.md`, `DECISIONS.md`,
  `BACKLOG.md`, `INPUTS.md`) and the 72 per-milestone design documents under
  `docs/specs/` and `docs/decisions/` are no longer published; they remain the
  project's local working record. 246 tracked files -> 174.
- Repository root reorganised: `CONTRIBUTING.md` and `SECURITY.md` moved to
  `.github/` (GitHub still surfaces them there) and `TEST_PLAN.md` to `docs/`.
  Root is now README, LICENSE, NOTICE, CHANGELOG, CITATION, the three
  dependency manifests, and the source/test/docs/packaging directories.
- Workbench UI reworked into desktop-tool chrome: a single dark theme owned by
  the app, hairline-separated docked panels with small-caps title bars, 22px
  controls with inset fields, flat raised buttons, and a status bar carrying the
  session state.
- `TEST_PLAN.md` brought up to date — it described milestone M1 while the
  project was at V47. Now a summary-level map of the 60 test modules, the
  ground-truth strategy, and exactly what is solver-gated versus ungated.
- Security reports moved to [`docs/security/`](docs/security/) and renamed
  without spaces; every finding now carries a re-verified **FIXED/OPEN** status
  and the `file:line` that closes it.
- README no longer implies a downloadable installer. There are no published
  releases yet, so the packaging section describes building one.
- Both Workbench screenshots in the tutorial regenerated on the new chrome,
  against a live session driven through the tutorial's own happy path.

### Fixed

- Where the thrombus was thin, the model put the blood pressure on the WALL
  side of the thrombus and left the wall there unloaded.
  - **Cause:** each thrombus boundary face was labelled lumen (pressure) or
    sac (tied to the wall) by the nearest surface VERTEX to its centre. At
    the 3 mm mesh that point is about 1.7 mm from its own vertices, so under
    about 1.7 mm of thrombus the labels swapped.
  - **Effect:** 41% and 45% of the wall area on two Zenodo AAA cases with thin
    thrombus, 7% on a third; 1–2% on the s1364 demo case.
  - **The fix:** a face is labelled by the surface it lies ON (the
    point-to-surface distance of its corners and centre). The thrombus ends
    on the cap planes and the lumen's re-cut caps are now explicitly free.
    Before, 48–84% of the cap-plane end faces carried pressure, against the
    model's own "ends stay free". A face on neither surface or on both is
    refused. The notes give the pressure / tied / free face counts.
  - **Results:** against the Zenodo dataset's published FE result, the thin
    cases now agree (rank correlation 0.13 → 0.91 on one, peak-tension ratio
    0.23 → 1.13 on the other). The 12-case medians are rho 0.95 and p99
    ratio 1.14. On s1364 the copilot's peak wall stress reads 282.5 kPa at
    140 mmHg (was 278.9).
- The thrombus mesh (`aaa-stress --lumen`) could hang forever with no
  error. gmsh spun inside its remesh of a cropped sac carrying near-zero
  edges (one exactly 0 mm); this stopped the s1364 copilot run at the stress
  step.
  - **Guard:** every gmsh call now runs in a child process that is stopped
    after 60 s with a readable error.
  - **Retries:** the mesh is tried with tiny edges collapsed (0.009 mm), then
    as given, then two other tolerances. An attempt is kept only if its
    volume and scaled-Jacobian checks pass, and the notes name the one used.
  - **Unchanged results:** the hand-built s1364 thrombus mesh is unchanged
    (19,777 tets).
- The Workbench could crash (SIGABRT/SIGSEGV in VTK's Python wrapping) when
  a step and the viewport's geometry request ran at the same time.
  - **Cause:** FastAPI runs the handlers in a thread pool, and VTK 9.6.2's
    wrapper map has an upstream bug (VTK commit 0acc7f73, not yet in a
    release) that concurrent VTK work makes far likelier.
  - **The fix:** compute and geometry routes now run one at a time under one
    lock. The `/state` and `/journal` polls stay unlocked.
  - **Verified:** the copilot's s1364 path crashed a fresh Workbench on its
    first run; it now ran four times in a row in one Workbench process.
- The copilot's wall gate passed a wall thinner than it claimed.
  - **What happened:** on s1364 the centreline sweep built a wall with no bad
    cells, but 1.30 mm thick along the surface normal, not 1.5 mm (its rays
    meet the sac's shoulders at an angle). Its peak stress read 382 kPa
    instead of 279.
  - **The fix:** `volmesh` now records the thickness measured along the
    normal (`lattice.thickness_normal_median`; also printed by the CLI). The
    `mesh_wall` gate fails a wall more than 5% off and tells the copilot to
    re-crop with parallel caps and use the plane sweep.
  - **Report:** it shows the measured thickness and a failure's whole first
    sentence, and shows scan paths by file name only.
- `crop` on a surface that is the previous crop's unchanged output now
  re-crops the surface that crop cut. It used to cut the crop again, so the
  copilot's "re-crop with parallel caps" trimmed the s1364 sac from 134.8
  to 93.7 mL.
- Zenodo AAA reference loading refused two of the 20 cases ("end annuli lie
  on 3 z levels"). In each, a single end-ring vertex sits 0.009 or 0.002 mm off
  its plane (float32 rounding). End-plane levels closer than 0.01 mm are now
  one plane. A genuine third plane is still refused.
- Lumen tet fallback: a gmsh failure (overlapping ring-shell facets on a
  folding sweep) now raises a readable error instead of a bare gmsh
  exception the CLI did not catch.
- `import_image` / `import-image`: a NIfTI with a rotated affine or negative
  axis directions (any LPS-derived volume) was refused. Marching cubes now runs
  in voxel-index space and the vertices go through the full affine; a
  reflecting affine flips the triangles back so normals stay outward.
  Singular affines are still refused.
- `import_image` on a NIfTI now sets the length unit (mm; metres and microns
  are converted; an undeclared unit is assumed mm with a note), so the cardiac
  CFD presets work on imaging sources.
- `segment` on a full chest-to-pelvis CTA: the CT size limit (512 x 512 x
  1024) refused real 1100-slice scans and is now 512 x 512 x 2048. An aorta
  mask whose bounding box is over the 40 M-voxel surface limit is now
  block-averaged by the smallest integer factors that fit, with a note,
  instead of refused. The smoothing keeps its physical size.
- Surfaces extracted from images no longer contain line or vertex cells.
  Merged sliver triangles are now deleted rather than demoted. Before this, a
  watertight aorta surface could fail the all-triangles check and not be
  mesh_ready.
- `lumen`: the `inlet`/`outlet` patches were much smaller than the vessel
  section whenever the surface ended in a tilted flat cap — every real arch
  tested (one inlet patch covered 38% of its cap, another 5%). The end
  station sat a fraction of a millimetre inside the cap with its plane square
  to the centreline, 3–26° off the cap's plane, so the
  rays on one side left through the cap almost at once; where the centreline
  stopped short of a cap (up to 25.8 mm) the lumen stopped short with it. Each
  end with a flat cap is now anchored on it: a short transition from the first
  interior station that clears the cap ends in a station that IS the cap, its
  spokes taken from the cap's rim polygon. All 20 patches now lie on their caps
  at 98.3–99.3% of the cap area (a 32-spoke ring inscribed in the rim); interior stations are untouched; inverted 0
  and structured hex throughout; sj_min rises in 8 of 10 cases. An end with no
  flat cap keeps its old section and the log says so.
- A fresh clone that ran `meshforge-web` or `meshforge-desktop` got a bare
  `ModuleNotFoundError`. `environment.yml` installs the base dependencies only
  and `-e .` pulls no extras, so those entry points are the first thing a new
  user hits with nothing installed. Both now raise with the exact install
  command, name conda-forge for the Qt and vtk.js packages, and offer
  `meshforge-web` as the no-Qt fallback. The README says plainly that the base
  environment is CLI-only.
- Workbench history panel: the `step` column collapsed to one character per line
  ("ce nt er li ne"). The JSON columns' `overflow-wrap` made every column's
  min-content one character, and auto table layout then squeezed the narrow
  column; the step cell is now pinned to its natural width.
- `conda-recipe/meta.yaml` declared the license as MIT and pointed `home` at a
  former repository name. Both now match the Apache-2.0 `LICENSE` and this repo.
- The web app's OpenAPI version was hard-coded and had drifted from the package
  version; it now reads `meshforge.__version__`.
- Workbench assets are now served with `Cache-Control: no-cache`. Without it a
  browser kept running a stale `app.js` against an upgraded server's routes.

- **A first run of the Workbench no longer dead-ends.** Eight front-door
  defects found in a hands-on walkthrough with a real clinical `.cso`; the
  meshing engine was not involved and is unchanged.
  - "Load surface" handed a `.cso` (or DXF/STEP/NIfTI/DICOM) straight to
    pyvista and leaked its reader error. Twelve suffixes are now routed by
    name, before the file is opened, to the panel and CLI subcommand that owns
    them — `.cso` says to use `import_cso` / `meshforge import-cso`.
  - Path fields rejected `~`, quoted paths, drag-into-terminal `\ ` escapes
    and trailing spaces. A new normaliser runs at every path entry point in
    the CLI, the service layer and the web write guards.
  - A mistyped path reported a bare `[Errno 2] No such file or directory`:
    the friendly `surface file not found` guard was unreachable because the
    checksum ran before the load. Both orderings fixed, and `file_sha256()`
    now checks existence itself.
  - The SDF voxel-budget refusal named `spacing` — a knob no panel has — and
    leaked `np.int64(...)`. It now names **thickness**, prints plain integers,
    and quotes a minimum computed for the actual bounding box and verified to
    fit.
  - The four `import_*` panels sat 7th-10th; they now sit directly under
    `source`, from an explicit step order the server publishes.
  - Run buttons had no busy state, so a second click fired a second concurrent
    request. Every request button now disables and relabels for its call.
  - `Browse…` reached only 5 of 12 file fields, and vanished without
    explanation in a plain browser tab. All 12 have a picker in the desktop
    app, and a browser tab gets one note saying why it cannot.
  - The step pane was 7,924px tall in a 668px viewport and errors printed
    ~1,000px from the button that caused them. Panels are now collapsible and
    remember their state; errors appear inline in the panel that produced them.
  - 29 internal `(Vnn)` milestone tags removed from user-facing help text.

### Added

- **Tissue strength library v0 and the rupture-potential index** (M4).
  `meshforge/data/strength_v0.csv` holds one row per tensile strip of the open
  USP AAA dataset (Gomes et al., Mendeley Data, doi 10.17632/x64srrc39p.3,
  CC BY 4.0; attribution in `meshforge/data/STRENGTH_V0_SOURCES.md` and
  `NOTICE`). There are 24 strips from 7 autopsy aneurysms: 13 from the
  rupture border and 11 non-dilated controls. Each row gives the failure
  stress as Cauchy and as engineering stress (MPa), with failure strain,
  tension and thickness, a stress-measure and unit flag, a late-peak flag,
  the source file, sheet and cells, and the source sha256.
  `scripts/build_strength_v0.py` builds the table. It checks every dataset
  file against its sha256, recomputes Cauchy stress from the raw curves (each
  value rounds to the reported one, largest difference 0.049 N/cm²), and stops
  on any unexpected file, unit or column. `--check` proves the shipped files
  match a fresh build. `meshforge.tissue.strength` loads and validates the
  library: it refuses an empty file, and any row with a missing or unknown
  unit or stress measure.
  - `rupture_index_band` returns the index PWS ÷ strength at P10/P50/P90,
    with n_strips and n_aneurysms. Wall stress from the solve is Cauchy
    stress, so it is compared with Cauchy strength, and index P90 = PWS ÷
    strength P10.
  - The default is empirical quantiles over the rupture-border strips. The
    output adds a lognormal fit, engineering strength, the late peaks at
    their reported maximum and raw-curve strips only as sensitivities, plus
    the controls as a comparison.
  - New `tissue_index` step (knobs `pws_mpa` / `from_summary`, `selection`,
    `method`; journaled and replayed; Browse… on `from_summary`).
  - New `meshforge tissue-index --pws <MPa> | --from-summary <json>` command,
    with `--selection`, `--method` and `--out`.
  - Every result is labelled an investigational index, not a diagnosis.
- `volmesh --sweep planes|auto` / knob `sweep` (swept backend; default
  `centreline`, unchanged): a **parallel-plane sweep** for a wall between two
  flat parallel end caps — a cropped aneurysm sac, where the centreline's
  rays cross and the wall folds (Anev_02's recovered sac: 1917 of 7552 hex
  inverted, 486 rays capped, 469 floored). Stations are planes square to the
  common cap normal, evenly spaced from cap to cap, so they never cross; each
  station is the plane's section of the surface (the cap's rim at the ends),
  sampled evenly in arc length and carried station to station along the
  surface; the wall layers are offset in the plane by t/cos(tilt) so the
  thickness along the surface normal stays t and the inlet/outlet faces lie
  on the caps. Counted, never silent: `capped` = surface steeper than
  `--max-tilt` (default 80°) plus sections that turn tighter than the offset;
  `floored`; small extra section loops are dropped and counted, a large one
  (a plane meeting the segment twice) refuses, as do caps more than 2° from
  parallel. `--side inward` grows the wall into the surface (the surface is
  the outer wall, e.g. a recovered sac). `--section-smoothing <mm>` (default
  0) Gaussian-smooths each section before sampling, for a surface rougher than
  the wall, and reports how far the nodes then sit from the input surface.
  `auto` keeps the centreline sweep when its wall is clean (0 inverted,
  capped, floored) and otherwise takes the planes when the caps are parallel.
  Results at 1.5 mm, cap 0.9: Zenodo P05 470 → 0 inverted, P16 347 → 0, both
  0 capped / 0 floored with no smoothing; Anev_02 sac 1917 → 0 inverted,
  0 capped / 0 floored with `--section-smoothing 4` (nodes ≤ 1.49 mm off the
  sac; unsmoothed: 0 inverted but 10 nodes capped at mm-scale notches of the
  recovered surface). The `eccentric_sac` synthetic (a 60 mm segment between
  parallel caps, exact circular z-sections) is the test shape.
- `crop` step and `meshforge crop` CLI (M2): crops an aorta surface (mm, +z
  superior) to its infrarenal aneurysm. Landmarks come from the centreline's
  equivalent-diameter profile — bifurcation (caudal end), flare top, sac
  (widest section), neck (first stretch above the sac with d <= max(1.2 x
  d_ref, d_ref + 2 mm) for >= 10 mm). Two cut planes square to the local
  vessel axis: flare top + `--distal-margin` (10 mm) and neck start +
  `--neck-extension` (25 mm). Each opening gets an exactly planar cap; a cut
  that opens two loops, grazes the wall or crosses the input's own end cap
  moves toward the sac. Refuses loudly, with the measured profile, when there
  is no sac (< 1.5 x its neck) or no neck. `--lumen` cuts a second surface with
  the same planes; `--planes-from` reuses an earlier crop's planes. The
  cropped surface carries a segment centreline whose ends leave the caps
  along their normals. Writes `<out>_crop.json` (landmarks, planes, caps,
  profile).
- `segment --lumen-out` / step knob `lumen_out` (+ `lumen_hu`, default 150):
  the contrast blood lumen = CT >= 150 HU inside the AI aorta mask, largest
  piece, through the same surface path; labelled as a CT threshold, not AI;
  detached bright pieces (calcification) are dropped and counted; reports the
  Dice against the AI mask.
- `volmesh --curvature-cap` / knob `curvature_cap` (swept, default 0.5
  unchanged) and a `wall rays: capped / floored` line; the swept wall's
  summary now carries `lattice` counts (`n_capped`, `n_floored`,
  thickness range) so a stress step can refuse a capped or floored wall.
- `aneurysm_tube` synthetic shape: a fusiform sac, a neck and a flared caudal
  end with an exact radius profile (straight or gently curved).
- `scripts/m2_gate_table.py`: crops and meshes every public AAA case (Zenodo
  P01-P20, AortaSeg-60 Anev_02 / Anev_04) into `outputs/m2/gate_table.*`.

- `sac` step and `meshforge sac <ct> --lumen-mask <mask>` CLI: recovers the
  OUTER aneurysm sac (lumen + intraluminal thrombus + wall) from a contrast CT
  around a lumen-only mask, such as TotalSegmentator's `aorta` label on a native
  (non-stented) aneurysm. Rays from the lumen centroid on every axial slice stop
  at the first sustained fat or air (< -30 HU over 1.5 mm). A sustained run
  >= 100 HU (another vessel, bone, or an enhancing organ such as the duodenum)
  stops a ray as unresolved. So do the field-of-view edge and 40 mm without fat.
  Unresolved and outlier rays are filled from their resolved neighbours, then
  smoothed around the circumference and along z, never past their upper bounds.
  The surface carries point fields `ilt_thickness_mm` (thrombus + wall along the
  ray) and `sac_resolved`. The summary reports the resolved fraction (per slice
  and overall), every ray's stop reason, maximum diameter (caliper, short axis,
  area-equivalent) and thickness statistics. Loud refusals: no lumen, a lumen
  touching the in-plane field-of-view edge, a non-axial grid, fewer than half of
  the rays finding fat (`min_resolved`). A NIfTI CT is read only inside the
  region of interest. The CLI writes the surface, the sac mask and a JSON
  summary, and `--reference-zenodo` scores the sac against the Zenodo AAA
  reference segmentation (DOI 10.5281/zenodo.19182977, CC BY 4.0).
  `scripts/validate_sac_zenodo.py` runs that comparison on all 20 cases, and
  `scripts/sac_overlay.py` draws axial lumen and sac overlays. The demo case is
  from AortaSeg-60 (DOI 10.5281/zenodo.18147026, CC BY 4.0).
- `segment` step and `meshforge segment` CLI: a CT (`.nrrd`, `.nii`,
  `.nii.gz`) goes to TotalSegmentator (`total` task, ROI `aorta`, full or
  `--fast`, `mps` or `cpu`), which runs as a separate program from its own
  conda env (`MESHFORGE_TOTALSEG`, then `PATH`, then
  `~/miniconda3/envs/totalseg`). The input is staged in a temp folder without
  spaces and checksummed before and after. The mask keeps its largest
  connected piece, is cropped to its bounding box, blurred (σ = 1 voxel by
  default) and becomes a closed surface in RAS millimetres; the session length
  unit is mm. Loud errors for a missing or failing executable (with the tail of
  its log), a timeout, an empty mask, and a mask too small to be an aorta
  (< 1 mL). The CLI writes the surface plus its mask, log and summary JSON.
- NRRD reader (`io/nrrd.py`, no new dependency): attached header, raw or gzip,
  LPS/RAS with negative or rotated directions, converted to the RAS frame. The
  voxel count is checked from the header before anything is decompressed;
  detached data, other encodings, non-3-D data and missing space information
  are refused.
- Validation against the public Zenodo AAA dataset (DOI 10.5281/zenodo.19182977,
  CC BY 4.0): `meshforge segment … --reference-zenodo <case>` reports voxel Dice,
  volumes and symmetric surface distance against three closed regions built from
  the authors' reference segmentation (lumen; lumen + thrombus; lumen + thrombus
  + wall) and names the best match (`io/zenodo_aaa.py`, `quality/overlap.py`).
- `scripts/prep_aortaseg60.py`: AortaSeg-60 (DOI 10.5281/zenodo.18147026,
  CC BY 4.0) ships its CTs with the head-foot axis of the header reversed.
  The script checks this against the dataset's own mask before changing
  anything, then writes a copy with the corrected header. Voxel data are
  untouched.
- `SECURITY.md` — threat model, private reporting route, audit index.
- `CONTRIBUTING.md`, `CHANGELOG.md`, `CITATION.cff`.
- Group-name validator tests covering OpenFOAM dictionary metacharacters
  (`;`, `{`, `}`, `#`, `$`, `//`) and an exact length-ceiling check, plus a
  writer-level test for the one patch name that is not a `BoundaryGroup`.
- `cfd`: the `inlet` knob takes a scalar speed, `inlet=150`, as well as a
  vector. The velocity is that speed along the patch's inward normal, measured
  on the mesh, so a sign slip can no longer blow fluid out of the inlet. A patch
  that is not a flat cap, or a speed <= 0, is refused with the reason. Every
  session-built run now reports its inflow: area, equivalent diameter, flow rate
  (also in L/min when the source's length unit is known) and Reynolds number.
  It warns when a vector points out of the domain, and when Re > 2300 under
  laminar flow. The CLI `cfd` command has only a case directory, so it refuses
  the speed form and says why.
- Workbench **flow ends** picker in the cfd panel: click the vessel near its
  inflow end, **Swap**, a speed field, and cardiac presets (blood viscosity,
  aortic mean and peak speed) converted to the source's length unit. Presets
  are refused when the source declares no unit. A live flow-rate/Reynolds
  preview shows before the run. The picker only writes the panel's own knobs,
  so a work file replays the click exactly.
- Workbench viewport: a CFD result is shown on a curved **flow section** along
  the vessel centreline, face-on, instead of the uniform no-slip wall. It has
  toggles for the section, a translucent wall and a robust colour range.

## [1.0.0] — 2026-07-24

Initial public release — commit `25a23f2`, "meshforge v1.0 — initial public
release". Version numbering starts here; development before this point was
tracked internally as milestones V1–V47.

The published package metadata lagged this release at `0.1.0` until the
2026-08-25 cleanup brought every source of the version into agreement; the
release itself is this commit.

### Added

- **Pipeline** — CAD/surface/image/CSO ingest → centreline → sections → wall →
  volume mesh, with a session journal, `.mfwork` work files and selective
  replay.
- **Meshing** — multi-block decomposition, projection onto curved profiles,
  O-grid (butterfly) disk layout, elliptic (Winslow) smoothing with a
  do-no-harm quality guard, transition grading, boundary-layer prisms, and a
  swept-hex lumen filler with an automatic tet fallback.
- **Solver decks** — Abaqus/CalculiX `.inp` (statics, modal, buckling, heat,
  transient heat, film convection, thermal stress), NASTRAN SOL 101, OpenFOAM
  `constant/polyMesh`, FLUENT `.msh`. LS-DYNA `.k` and ANSYS `.cdb` are
  **format-level only** — no solver has validated them, and the emitted deck
  headers say so.
- **In-app solve** — CalculiX and OpenFOAM runners with results read back onto
  the session mesh; MYSTRAN as an independent cross-check.
- **Interfaces** — a scriptable CLI, YAML recipes with parameter sweeps, a local
  127.0.0.1 web Workbench with an interactive vtk.js viewport, and a native
  desktop shell over it.
- **Validation** — analytic and published-benchmark ground truth throughout:
  Lamé thick cylinder, Euler buckling, rod spectrum, log T(r), Hagen–Poiseuille,
  Blasius friction, and Ghia et al. (1982) lid-driven cavity.

[Unreleased]: https://github.com/LeventS2005/meshforge/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/LeventS2005/meshforge/releases/tag/v1.0.0

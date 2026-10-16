# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

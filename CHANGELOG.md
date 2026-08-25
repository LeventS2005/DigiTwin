# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed

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

### Fixed

- Workbench history panel: the `step` column collapsed to one character per line
  ("ce nt er li ne"). The JSON columns' `overflow-wrap` made every column's
  min-content one character, and auto table layout then squeezed the narrow
  column; the step cell is now pinned to its natural width.
- `conda-recipe/meta.yaml` declared the license as MIT and pointed `home` at a
  former repository name. Both now match the Apache-2.0 `LICENSE` and this repo.
- The web app's OpenAPI version was hard-coded and had drifted from the package
  version; it now reads `meshforge.__version__`.

### Added

- `SECURITY.md` — threat model, private reporting route, audit index.
- `CONTRIBUTING.md`, `CHANGELOG.md`, `CITATION.cff`.
- Group-name validator tests covering OpenFOAM dictionary metacharacters
  (`;`, `{`, `}`, `#`, `$`, `//`) and an exact length-ceiling check, plus a
  writer-level test for the one patch name that is not a `BoundaryGroup`.

## [1.0.0] — 2026-08-25

Initial public release. Version numbering starts here; development before this
point was tracked internally as milestones V1–V47 and is visible in the git
history.

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

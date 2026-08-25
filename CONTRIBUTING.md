# Contributing

Thanks for looking. meshforge is a research project maintained by one person;
issues and small, focused pull requests are welcome.

## Environment

conda is the primary, reproducible path — several dependencies (VTK, Qt,
trame's vtk.js assets) are compiled binaries that must come from conda-forge:

```bash
conda env create -f environment.yml --solver=libmamba
conda activate meshforge
```

pip works as a fallback for the pure-Python parts:

```bash
pip install -r requirements.txt -e .
```

Optional extras, installed only when you need them: `[meshing]` (gmsh),
`[cad]` (ezdxf), `[recipes]` (pyyaml), `[image]` (pydicom/nibabel), `[web]`
(FastAPI/uvicorn). On **macOS**, `[viewport]` (trame-vtk) and `[desktop]`
(PySide6 + qt6-webengine) must come from **conda-forge, never pip** — a pip
PySide6 bundles a second Qt beside conda's `qt6-main` and hard-crashes with
exit 134.

## Tests

```bash
python -m pytest -q                      # everything available on this machine
python -m pytest tests/test_solve.py -v  # one area
```

Tests that need an external solver (CalculiX, OpenFOAM, MYSTRAN), an optional
dependency, or a local clinical data directory **skip cleanly** when it is
absent — that is deliberate, and CI runs the ungated set. See
[`TEST_PLAN.md`](TEST_PLAN.md) for what is gated on what.

What a change needs before it lands:

- Tests for the new behaviour **and** its failure branches — not just the happy
  path. Ground truth is analytic or a published benchmark wherever one exists;
  tolerances reflect discretization, never hand-tuning to make a test pass.
- Degenerate cases constructed on purpose (open surfaces, non-manifold edges,
  empty input, wrong dtype).
- `python -m pytest -q` green, and for anything touching the pipeline, a real
  CLI or Workbench run end-to-end with **0 inverted cells** on clean geometry.
- Adversarial fixtures stay **tiny and synthetic**. Never commit clinical data,
  a real filename, or anything patient-derived.

## Architecture rules

These are load-bearing, not style preferences:

1. **User-supplied input files are read-only.** Checksum before and after; a
   writer that could overwrite an input must refuse loudly.
2. **Generated files go only into `outputs/`** (git-ignored). Temp files go in a
   temp dir, never the repo.
3. **The core stays GUI-agnostic.** All logic lives in `src/meshforge/`; the web
   and desktop layers may only call the service layer (`MeshingSession`). This
   is enforced by an AST scan — see `tests/rule4_scan.py`.
4. **Heavy and optional dependencies are lazy-imported**, so the CLI never pays
   for gmsh, pydicom, or FastAPI it does not use.
5. **Fail loudly.** A bad input raises with a message that says what was wrong
   and what to do; it never silently produces a plausible-looking mesh.

## Branches and commits

- Work on a feature branch off `main`; never commit directly to `main`.
- One logical change per commit, with a real message: a short imperative
  subject (`fix(web): …`, `docs: …`, `test(security): …`), then a body
  explaining *why* when it is not obvious.
- Documentation ships in the **same commit** as the code it describes.
- Open a PR against `main`; CI must be green.

## Security

Please report vulnerabilities privately — see [`SECURITY.md`](SECURITY.md).
Do not open a public issue, and never attach clinical data.

# Test plan

## Strategy
- **Ground truth is analytic.** Every geometric quantity is asserted against a
  closed-form value on synthetic geometry (sphere/cylinder now; the M5
  generators with exact centerlines later). Tolerances reflect discretization,
  not hand-tuned slack.
- **Hard cases are explicit.** Open surfaces, non-manifold edges, empty files,
  wrong data types — the degenerate case is constructed on purpose, never
  softened to make a test pass (trap #2).
- **Every failure branch is exercised**, not just the happy path (trap #3).
- **Read-only inputs are proven**, not assumed: byte-for-byte checksum before
  and after every load/CLI run.
- **Backend-gated tests skip cleanly** when an optional dep (gmsh, Qt, solver)
  is missing (trap #16) — applies from M4 on.
- **GUI tests (M6)** inject a stub viewport and run steps synchronously; no real
  QtInteractor offscreen (trap #15 — segfaults can't be caught in-process).

## Matrix (current: M1)
| Area | Case | Kind |
|---|---|---|
| io.load | sphere STL loads as PolyData | happy |
| io.load | missing file → FileNotFoundError | error |
| io.load | UnstructuredGrid .vtu → TypeError | error |
| io.load | empty PolyData → ValueError | error |
| io.sha256 | matches hashlib reference; input unchanged after load | invariant |
| quality.inspect | sphere: watertight, area/volume within 1% of analytic | ground truth |
| quality.inspect | open plane: volume None + "open edges" note | hard case |
| quality.inspect | 3-triangle fan: non-manifold → volume None + note | hard case |
| quality.inspect | JSON/text serialization round-trip | contract |
| cli.inspect | end-to-end: exit 0, reports in outputs/, input unchanged | e2e |
| cli.inspect | missing file: exit 1, stderr error, no report written | error |
| cli | unknown command: argparse exit 2 | error |

## How to run
```
conda activate meshforge
python -m pytest -v
```
Plus the per-milestone end-to-end CLI run on a generated surface (pasted into
CHECKPOINT.md each session).

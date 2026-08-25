# Security review — CSO / biomedical ingest path (untrusted binary input)

**Date:** 2026-07-09 (session 24) · **Reviewer model:** Opus 4.8 · **Branch:** `sec-cso`
· **Baseline:** `python -m pytest -q` → 544 passed, 3 skipped.

Scope: every untrusted-binary parser added *after* the last security review
(session 11, D49 — `security-analysis-findings.md`), which explicitly deferred
finding **F5** to "when a 2nd input type or a web upload endpoint lands." Four
have since landed:

* `io/cso.py` — hand-written decoder for MeVisLab Binary CSO (1,6,0) (V38–V40).
* `io/image.py` — single-file multi-frame DICOM CT loader + grid math (V39).
* `geometry/centerline.py` — centreline embedded in / read back from a surface's
  field data — a **crafted `.vtp` a user opens is untrusted input** (V40).
* `pipeline.py` — `compute_import_cso` / `auto_cso_surface` + `.mfwork` replay of
  attacker-controlled param VALUES (cf. D49 F1).

**Threat model:** a malicious `.cso`, `.dcm`, or `.vtp` file a user opens (single-
user local tool; no network/priv boundary). Consistent with D49's overall
verdict, **no RCE / privilege-escalation was found.** The findings are
**integrity (silent geometry corruption — rule-6 fail-open)** and **DoS
(unbounded allocation / quadratic loops)** — which in a meshing tool that emits
"analysis-ready, watertight" surfaces for surgical/engineering use is exactly the
class rule 6 exists to stop.

Every finding below was reproduced with a **synthetic, tiny** crafted input (the
existing `tests/test_io_cso.py` / `tests/test_image.py` writers) — the real UCSF
family is never used for adversarial fixtures (rule 1).

> **Status column re-verified 2026-08-25 (session 39).** The `FIX` entries below
> were the *proposed* action when this review was written; every one of them
> landed in session 24 (D61) — see **Resolution** further down — and the column
> now records that outcome with the `file:line` that closes it in the current
> tree. Coverage: `tests/test_cso_security.py`, `tests/test_image.py`.

| # | Sev | File | Class | Status | Closed by |
|---|-----|------|-------|--------|-----------|
| F-CSO-1 | HIGH | `io/cso.py` `_read_ring` | fail-open (NaN/inf coords) | **FIXED** | `src/meshforge/io/cso.py:440` |
| F-CSO-2 | HIGH | `geometry/centerline.py` `centerline_from_surface` | fail-open (`.vtp` deserialization) | **FIXED** | `src/meshforge/geometry/centerline.py:81,84` (read-back) + `:57` (`embed_centerline`) |
| F-CSO-3 | MEDIUM | `io/cso.py` `_order_contours` | DoS (unbounded count → O(n²)) | **FIXED** | `src/meshforge/io/cso.py:62` (`MAX_RECORDS`), enforced at `:276` |
| F-CSO-4 | MEDIUM | `io/cso.py` `_loft_surface` (+ replay/CLI) | DoS (unbounded `n_ring` alloc) | **FIXED** | `src/meshforge/io/cso.py:65` (`MAX_N_RING`), enforced at `:747` |
| F-IMG-5 | LOW | `io/image.py` `_read_dicom_multiframe` | fail-open (non-finite/≤0 spacing) | **FIXED** | `src/meshforge/io/image.py:220-226` |
| F-IMG-6 | INFO→LOW | `io/image.py` `_load_dicom_series` | broad `except`, no series ceiling | **FIXED** (D62) | `src/meshforge/io/image.py:307` (narrowed to `InvalidDicomError`), `:322-329` (`MAX_VOXELS`) |
| F-IMG-7 | INFO | `io/image.py` `_load_nifti` | `get_fdata()` before MAX_VOXELS | **FIXED** (D62) | `src/meshforge/io/image.py:265-270` (shape checked before `get_fdata()`) |
| F-INFO-8 | INFO | `pipeline.py` `compute_import_cso` | double-read (TOCTOU) | **FIXED** (D62) | `src/meshforge/pipeline.py:1089` |

---

## F-CSO-1 — HIGH — non-finite (NaN/inf) contour coordinates → fail-open garbage mesh

**Where:** `io/cso.py` `_read_ring` (line ~373: `pts[k] = xyz`; line ~396–399 the
`normal` normalisation). No `np.isfinite` check on any decoded `VEC3`.

**Crafted input:** a well-formed Binary CSO whose control-point `VEC3` payload
carries a `NaN` (or `±inf`) in one coordinate (8 bytes = `struct.pack('<d', nan)`),
which is byte-legal in the token stream.

**Impact (fail-open, rule-6 violation):** the NaN flows straight into
`cso_to_surface`. Every downstream guard that should catch a degenerate ring is a
`> 0` / `< 1e-9` / `<= 0` comparison, and **NaN is False in all of them**, so they
are silently bypassed:
* `_loft_surface` tangent check `np.any(tn < 1e-9)` → `nan < 1e-9` is False → passes;
* `_resample_closed` `if total <= 0` (perimeter) → `nan <= 0` is False → passes;
* `contour_centerline` `np.any(r_mean <= 0)` / `r_ins <= 0` → NaN passes.

Confirmed: a 2-contour and an 8-contour-with-guide crafted CSO each produced a
**loft surface with non-finite points reported `watertight=True`** — a "successful"
but meaningless mesh handed to the wall/volmesh/solve pipeline. (When there are
>2 contours *and no guide*, `_order_contours`'s SVD happens to raise
`LinAlgError` — an opaque, path-dependent crash, not a clean rule-6 error, and it
does **not** fire on the with-guide or ≤2-contour paths.)

**Fix:** reject non-finite decoded geometry at the source in `_read_ring` (points
and plane normal) with a loud, human-readable `ValueError` naming the record.
Fixing at ingest closes every downstream path uniformly (loft, voxel, both
centrelines) and makes surface bounds finite by construction.

**D49 cross-ref:** new class; D49 predates the CSO parser. Same spirit as trap #13
(NaN scaled-Jacobian poisoning stats) — non-finite must be treated as invalid, not
silently propagated.

---

## F-CSO-2 — HIGH — non-finite embedded centreline in a crafted `.vtp` → fail-open

**Where:** `geometry/centerline.py` `centerline_from_surface` (lines 61–91). The
field-data arrays `meshforge_centerline_points` / `_radii` are shape/length-checked
but **never finiteness-checked**; the tangent guard `if np.any(norms <= 0): return
None` (line 79) is NaN-evading (`nan <= 0` is False).

**Crafted input:** a `.vtp` a user opens whose `field_data["meshforge_centerline_
points"]` contains a `NaN`/`inf` (an attacker-authored surface, or a tampered
round-trip of a V40 auto-loft export). `build_wall_lattice` reads this embedded
centreline *in preference to* recomputing a skeleton.

**Impact (fail-open):** confirmed — `centerline_from_surface` returned a
`Centerline` whose `points` are non-finite, which then seeds the wall lattice →
NaN wall → a degenerate-but-"successful" volume mesh. A new deserialization
surface introduced by V40, never audited.

**Fix:** `np.isfinite(pts).all()` (and radii) gate → `return None` (the documented
"no usable embedded centreline" sentinel; the caller then recomputes a real
skeleton centreline — fail-safe, not fail-open). Belt-and-braces: reject a
non-finite `Centerline` at `embed_centerline` time too.

**D49 cross-ref:** directly answers D49 **F5** ("revisit when a 2nd input type …
lands") — the `.vtp` field-data reader is a genuine new untrusted-input surface.

---

## F-CSO-3 — MEDIUM — unbounded record/contour count → O(n²) ordering DoS

**Where:** `io/cso.py` — the token walk appends to `records` with **no ceiling**;
`_order_contours` → `_nearest_neighbour_chain` (lines 482–497) is O(n²) with a
per-element Python `min(..., key=lambda: np.linalg.norm(...))`, and the
guide-projection path `_project_param` is O(n_contours · guide_len).

**Crafted input:** a Binary CSO packed with many tiny (3-point) closed contours.
Minimum record ≈ 260 bytes, so a ~5 MB file yields ~20k contours → ~4·10⁸ slow
Python/NumPy lambda evaluations (minutes); a ~50 MB file → ~10¹¹ (hours). No large
allocation is needed — the cost is pure quadratic CPU.

**Impact:** wall-clock DoS / unkillable hang on a modestly-sized crafted file.

**Fix:** a `MAX_CONTOURS` ceiling (and a matching `MAX_RECORDS` on the decode
loop) checked *before* ordering, failing loud. Cap chosen far above any real
segmentation (the real family maxes ~20 rings) yet below the quadratic-pain
threshold.

**D49 cross-ref:** same discipline as the existing `MAX_VOXELS` (centerline.py:13)
and the `npts ≤ 1e6` cap already in `_read_ring` — this extends the "bound every
attacker-sized count" rule to the record dimension.

---

## F-CSO-4 — MEDIUM — unbounded `n_ring` → loft allocation DoS

**Where:** `io/cso.py` `_loft_surface` (line ~645) checks only `if n_ring < 3`; no
upper bound. Reachable with an attacker-controlled value via a crafted `.mfwork`
`import_cso` step (`replay` passes journal `params` straight into
`compute_import_cso(**params)` → `cso_to_surface(n_ring=…)`), and via the CLI.

**Crafted input:** a `.mfwork` whose `import_cso` params set `n_ring = 100_000_000`.

**Impact:** `_resample_closed` builds `np.arange(n_ring)` per contour and the face
list is `2·n_ring·(R−1)` ints → tens of GB → OOM DoS, before any geometry is
produced.

**Fix:** cap `n_ring` at a sane maximum (e.g. 100 000 — ~2000× the default 48,
far beyond any real need) with a loud `ValueError`.

**D49 cross-ref:** analogous to F3/F4 (bound the untrusted knob before it reaches
an allocator); complements the `.mfwork` step-name allowlist (D49 F1) by bounding
attacker-controlled param VALUES, not just names.

---

## F-IMG-5 — LOW — DICOM PixelSpacing / origin not finiteness/positivity-checked

**Where:** `io/image.py` `_read_dicom_multiframe` (lines 183–194): only the z-step
is guarded (`z_sp <= 0`). `PixelSpacing` (px_row/px_col) and `ImagePositionPatient`
(origin) are taken as-is. `dicom_grid` returns them unvalidated, and the consumer
`cso_to_surface` grid path checks `np.any(spacing <= 0)` — **NaN-evading**.

**Crafted input:** a multi-frame `.dcm` with `PixelSpacing = [nan, 0.75]` (or `0`).
Confirmed: `dicom_grid` returned `spacing=(nan, 0.75, 0.9)` — a non-finite grid
that anchors a segmentation onto a meaningless lattice.

**Impact:** image-anchored voxelisation onto a NaN/zero-pitch grid → NaN index math
that slips the `< -0.5 / > dims-0.5` ROI bounds checks (NaN-evading) → garbage or
crash. Low because it requires a malformed medical image and the pixel-path
consumer (`volume_to_surface`) already rejects it via `all(s > 0 …)`.

**Fix:** validate `PixelSpacing` finite & > 0 and `ImagePositionPatient` finite at
read, loudly — closing both the `dicom_grid` geometry path and the NaN-evasion in
the `_voxel_surface` grid check at the source.

**D49 cross-ref:** extends D49 **F4** (MAX_VOXELS in image ingest) — same "validate
the image header before trusting it" posture, applied to spacing/origin.

---

## Resolved (D62, session 25) — the three former INFO items closed out

> **Update 2026-07-09 (D62):** the three INFO items below were initially
> risk-accepted (outside the CSO/DICOM primary path). The user asked to fix all
> F's, so they were **all fixed in session 25 (D62)** — each in the core (rule 4),
> failing LOUD (rule 6), test-first in `tests/test_cso_security.py`. Original
> rationale kept for context.

| # | Fix | Location |
|---|-----|----------|
| F-IMG-6 | narrow `except Exception` → `except InvalidDicomError` (a straggler still skips; a *corrupt* DICOM's other error now surfaces) **and** add a `MAX_VOXELS` ceiling on the declared `rows·cols·n_slices` *before* the pixel-read loop | `io/image.py` `_load_dicom_series` |
| F-IMG-7 | check `img.header.get_data_shape()` against `MAX_VOXELS` and refuse loud **before** `get_fdata()` allocates the full (float64-upcast) array | `io/image.py` `_load_nifti` |
| F-INFO-8 | new `load_cso_checked(path) -> (sha256, CsoData)` reads the bytes ONCE and derives both the checksum and the parse from the same buffer; `compute_import_cso` uses it (no TOCTOU window; `load_cso(buf=…)` added, public `load_cso`/`cso_input_sha256` unchanged) | `io/cso.py` + `pipeline.py` |

**Original deferral rationale (kept):**

* **F-IMG-6 (INFO)** — `_load_dicom_series` wrapped per-file `dcmread` in
  `except Exception: continue` (intended straggler-skip) and stacked slices with no
  `MAX_VOXELS` pre-check. This is the **directory-of-slices** loader; the real
  biomedical data (and the CSO-anchoring path) is single multi-frame files handled
  by `_read_dicom_multiframe`, which *was* already ceiling-checked. The broad
  `except` only *skips* a file (fail-safe toward "no slices → loud ValueError"), it
  does not fabricate geometry — hence INFO, raised to LOW on fix.

* **F-IMG-7 (INFO)** — `_load_nifti` called `img.get_fdata()` (full allocation)
  before `volume_to_surface`'s MAX_VOXELS ceiling. NIfTI was outside the CSO/DICOM
  focus and un-testable at audit time (nibabel not installed — the one skipped image
  test). **nibabel is now installed (pip)** so the ceiling and the NIfTI round-trip
  are both exercised.

* **F-INFO-8 (INFO)** — `compute_import_cso` read the file twice (checksum then
  parse); a concurrent modification between them is a TOCTOU. The threat model is a
  static malicious file a *single* user opens (not a racing attacker) and the
  checksum is provenance bookkeeping (rule 1), not a security gate — hence INFO. The
  single-read refactor closes the window cleanly and is marginally faster.

### D62 verification (done)

* **Adversarial fixtures** — 9 new tests in `tests/test_cso_security.py` (4 F-IMG-6,
  2 F-IMG-7, 3 F-INFO-8). On the **pre-fix** code the 5 security-relevant ones
  FAILED (series ceiling absent, broad `except` swallowed a `RuntimeError`, NIfTI
  `get_fdata` reached, `load_cso_checked` missing); after the fixes all 9 pass; the
  4 regression guards passed throughout.
* **CLI end-to-end (pasted, real exit codes):**
  ```
  $ meshforge import-cso  clean_arch.cso --method loft   → watertight surface,
        input_sha256 == sha256(file bytes) (single read)   exit 0
  $ meshforge import-image oversized.nii  --level 0
        error: oversized.nii volume (350, 350, 350) exceeds 40000000 voxels …  exit 1
  $ meshforge import-image oversized_series/ --level 0
        error: DICOM series … declares 4000x4000x3 = 48000000 voxels, exceeding
        40000000 …                                                            exit 1
  ```
* **nibabel installed** (pip) so the NIfTI round-trip test now runs (it was the
  historical skip); the `_load_nifti` install-hint branch test now skips instead
  (net skip count unchanged at 3: NIfTI install-hint, opt-in live shell, pydicom
  install-hint).
* **Rule 1** — all 13 input checksums byte-for-byte unchanged at session start AND
  end.

**Verdict (D61+D62):** the CSO/DICOM/NIfTI/`.vtp` ingest path is now hardened
end-to-end — two fail-open (rule-6) holes and five DoS/allocation vectors closed,
plus the TOCTOU double-read removed; no RCE/priv-esc found (consistent with D49).

---

## Resolution (all applied 2026-07-09, session 24, D61)

Each fix lands in the core (`src/meshforge/`, rule 4) and fails LOUD (rule 6):

| # | Fix | Location |
|---|-----|----------|
| F-CSO-1 | reject non-finite control points + plane normal at decode | `io/cso.py` `_read_ring` |
| F-CSO-2 | `centerline_from_surface`: non-finite → `return None` (recompute); `embed_centerline`: reject non-finite | `geometry/centerline.py` |
| F-CSO-3 | `MAX_RECORDS = 100_000`, checked inside the decode loop | `io/cso.py` `load_cso` |
| F-CSO-4 | `MAX_N_RING = 100_000`, checked before the loft allocation | `io/cso.py` `_loft_surface` |
| F-IMG-5 | validate PixelSpacing finite & > 0 and origin finite at read | `io/image.py` `_read_dicom_multiframe` |

## Verification (done)

* **Adversarial fixtures** — `tests/test_cso_security.py` (19 tests). On the
  **unpatched** code they failed as expected (`FFFFF.FFF.FF…`) and
  `test_huge_n_ring_refused` **hung on a real 100M-point loft allocation** (F-CSO-4
  live). After the fixes: **19 passed in 0.91 s** (the n_ring test now fails fast).
* **Full suite** — `python -m pytest -q` → **563 passed, 3 skipped** (544 baseline
  + 19), no regression.
* **CLI end-to-end on crafted-malicious input** (all exit 1, loud):
  ```
  $ meshforge import-cso outputs/malicious_nan.cso --out …
  error: CSO record 3: non-finite control-point coordinate (NaN/inf) — refusing to build geometry from corrupt data
  $ meshforge import-cso outputs/malicious_truncated.cso --out …
  error: unknown CSO tag b'' at offset 1037 — refusing to skip it …
  $ meshforge import-cso outputs/clean.cso --method loft --n-ring 100000000 --out …
  error: n_ring 100000000 exceeds the maximum 100000 — refusing to allocate a loft of that width …
  ```
* **Real UCSF family** — all 10 `.cso` still mesh `inverted == 0` via `auto`
  (`test_real_family_end_to_end` + read-only sample + DICOM-anchor: 12 passed),
  no regression.
* **Rule 1** — all 13 input checksums (DWG + 10 CSO + 2 DICOM) byte-for-byte
  unchanged at session start AND end.

**Verdict:** the CSO/DICOM/`.vtp` ingest path is hardened against the audited
malicious inputs; two fail-open (rule-6) holes and three DoS vectors closed; no
RCE/priv-esc found (consistent with D49). Residual INFO items risk-accepted above
and logged to BACKLOG.md.

---

## Round 2 (session 34, 2026-07-11, D73) — fuzz campaign + the last two-read

Scope per the session-33 queue: fuzz the binary token walk + the spline-skip,
add VEC2/VEC4/MAT decoding ONLY on evidence, close the `--dicom` two-read.

### Fuzz campaign (scratchpad harness, structure-aware + random)

36k+ mutations per pass over a synthetic tube AND an in-memory copy of a real
clinical export (byte flips, truncations, slice duplication, u32/u64/f64 field
corruption incl. adversarial lengths, tag swaps incl. VEC2/VEC4/MAT/garbage),
each parse alarm-guarded. Contract: `ValueError` or a valid parse — and, in a
second pass, `ValueError` or a FINITE loft surface.

| Finding | Severity | Where | Class | Status |
|---|---|---|---|---|
| F-CSO-9 | HIGH | `io/cso.py` `_read_ring` | fail-open (finite overflow) | FIXED |
| F-CSO-10 | LOW | `io/cso.py` `_read_ring` (normal) | silent corruption | FIXED |
| F-INFO-8b | INFO | `pipeline.py` `--dicom` anchor | double-read (TOCTOU) | FIXED |

* **F-CSO-9 — HIGH — huge-but-FINITE coordinates → fail-open NaN mesh.**
  Coordinates ≥ ~1e154 pass every NaN/inf guard (they are finite) but the loft
  arithmetic (squared distances, cross products) overflows into NaN → a NaN
  surface **reported `watertight=True`** — F-CSO-1's fail-open through the
  overflow door. Probe: scales 1e154/1e200/1e300 all failed open; 16/3000
  random f64-field mutants hit the same root cause. **Fix:** `MAX_COORD = 1e9`
  (mm — 1000 km; real scanner coords are <1e4, overflow needs ~1e154) checked
  in `_read_ring` right after the finiteness gate.
* **F-CSO-10 — LOW — ~1e300 plane normal silently zeroed.** Its norm
  overflows to inf and `n / inf → [0,0,0]` — silent corruption instead of a
  refusal. Same `MAX_COORD` gate on the raw normal.
* **F-INFO-8b — INFO — the `--dicom` anchor read the file twice** (checksum
  via `volume_input_sha256`, then `dicom_grid` re-opened the path in pydicom)
  — the metadata-only sibling of F-INFO-8, deferred at D62. **Fix:** new
  `io/image.py dicom_grid_checked(path) -> (sha256, (dims, spacing, origin))`
  reads the bytes once (`_read_dicom_multiframe(buf=…)` parses a `BytesIO`);
  `compute_import_cso` uses it.
* **VEC2/VEC4/MAT tags:** scanned ALL 20 real files (both families) — none
  carries them (`FLT_`/`U64_` do appear and were already decoded). Per the
  evidence rule, no speculative tag support was added; unknown tags keep
  refusing loudly.

### Verification (round 2)

* 5 new adversarial tests — **all 5 FAILED on the pre-fix code** (huge coords
  parsed fine; huge normal silently zeroed; a seeded fuzz mutant produced a
  non-finite "watertight" loft; `dicom_grid_checked` absent; e2e `dcmread`
  received the raw path) — all pass after; `tests/test_cso_security.py`
  33 passed. A 300-seed mini-fuzz (`test_fuzz_seeded_parse_loft_contract`)
  now locks the parse+loft contract in-suite.
* Post-fix fuzz campaigns: 33k parse mutations + 3k surface-path mutants +
  every overflow scale → **zero findings**.
* CLI end-to-end (real exit codes):
  ```
  $ meshforge import-cso outputs/malicious_overflow.cso --out …
  error: CSO record 0: control-point coordinate magnitude 3e+200 exceeds 1e+09
  (coordinates are millimetres; values this large overflow geometry arithmetic
  into NaN) — refusing to build geometry from corrupt data          exit 1
  $ meshforge import-cso "Example CSO Files/<VAM1 export>" --method loft
  contours 24, loft 1154 pts / 2304 tris, watertight=True           exit 0
  ```
* Real families unaffected: `tests/test_io_cso.py` 43 passed (incl. all 10
  arches end-to-end `inverted == 0`).

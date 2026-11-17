# DigiTwin — CSO user guide (MeVisLab contours → analysis-ready mesh)

This is the step-by-step guide for loading **MeVisLab Binary CSO** contour
files into DigiTwin and taking them all the way to a solver-ready volume
mesh — in the app (Workbench) or on the command line. It assumes nothing;
for the rest of the app see **[APP_GUIDE.md](APP_GUIDE.md)**, and for a
gentle first run **[TUTORIAL.md](TUTORIAL.md)**.

> **Clinical data rule (read this first).** CSO files exported from patient
> scans are treated as READ-ONLY inputs: DigiTwin never modifies them, and
> records each file's sha256 in the import provenance. Keep them out of
> version control — filenames alone can carry initials and scan dates. The
> `Example CSO Files/` folder under the project root is git-ignored for
> exactly this reason; if you add a new folder of scans, git-ignore it too.

---

## 1. What a CSO file is, and what DigiTwin does with it

A `.cso` file is MeVisLab's contour format: a stack of closed planar
contours (rings) traced on CT slices — typically a segmented vessel — plus,
optionally, an open marker chain used as a centreline guide. DigiTwin
reads the **Binary CSO (1,6,0)** encoding directly; no MeVisLab needed.

The pipeline (`import_cso` step):

1. **Parse** the binary token stream. Every record is bounds-checked;
   corruption, truncation, and unknown tags are refused loudly — a damaged
   clinical file can never silently half-load.
2. **Classify** each contour: closed rings become the vessel sections; an
   open marker chain (if present) becomes the centreline guide that orders
   the rings along the vessel.
3. **Surface** the rings by one of three methods (see §3 `method`), giving
   a closed, watertight triangulated surface.
4. The surface becomes the session's active geometry — everything else in
   DigiTwin (volmesh, solve, CFD, decks, export) works from there
   unchanged.

---

## 2. The 60-second happy path (Workbench)

```bash
cd /path/to/DigiTwin
conda activate digitwin
digitwin desktop        # or: digitwin web  (same UI in the browser)
```

1. In the left column, scroll to the **import_cso** panel.
2. Click **Browse…** next to `path` and pick your `.cso` file.
   (Browse… buttons exist only in the desktop window — in a plain browser
   tab, paste the file's **absolute path** instead. There is no drag-drop
   or HTTP upload here: the server runs on *your* machine and reads the file
   directly from disk, so a path is all it needs. The one upload is the Lab
   mode's **Upload scan**, for a CT only — see `APP_GUIDE.md` §3.14.)
3. Leave every other knob at its default and click **Run import_cso**.
4. Read the log: you want `watertight=True` and `mesh_ready=True`.
   The viewport now shows the lofted vessel surface, with the **raw
   source contours** drawn over it in amber — eyeball that the surface
   hugs the rings (the viewport-toolbar **contours** checkbox toggles
   them; the count in its label should match your segmentation).
5. Scroll to **volmesh**, set `thickness` to your wall thickness
   (e.g. `1.5` for an aortic wall in mm — the knob is in the file's own
   units), click **Run volmesh**.
6. Read the log: **`inverted` MUST be 0**; `sj_min` is your quality floor
   (≥ 0.1 is workable for a real tortuous arch; the mean matters more).
7. Done — the mesh has named boundary groups (`inner`, `outer`, `inlet`,
   `outlet`) ready for the **solve** panel or a CLI solver deck.

Everything you did is journaled (right pane). **Save work file…** captures
the whole session as a `.mfwork` you can replay bit-for-bit later.

---

## 3. The import_cso panel, knob by knob

| Knob | Default | What it does |
|---|---|---|
| `path` | — | Absolute path to the `.cso` file. Desktop: use **Browse…**. |
| `method` | `auto` | How rings become a surface. **`auto` (recommended):** tries lofting with progressively more robust centrelines and falls back to voxelisation, keeping the FIRST variant whose trial volume mesh has zero inverted cells — you get the smooth loft when the anatomy allows it and a guaranteed-sound mesh when it doesn't. `loft`: force ring-skinning (smooth tube; can fail on very tortuous vessels). `voxel`: force rasterise-then-marching-cubes (always works; denser, slightly stepped surface). |
| `n_ring` | 48 | Loft resampling: points per ring. More = rounder sections, more triangles. |
| `voxel_size` | auto | Voxel pitch for the `voxel` method. Smaller = finer + slower. |
| `cap_ends` | 1 | Cap the two end rings so the surface is watertight. Set 0 only if you deliberately want an open tube (it will NOT be mesh-ready). |
| `on_open` | `skip` | Policy for an **open editor contour** (a ring someone left unclosed in MeVisLab). `skip`: drop it with a loud note in the log — the right default; a dangling arc is usually an editing artifact. `close`: bridge the gap, but only if the ring is nearly closed — a wide-open arc still refuses. `error`: strict — any open contour stops the import (use when you want to be told your segmentation needs fixing). |
| `dicom` | — | Optional: path to the matching CT (§5). Forces `voxel`. |

**Reading the import log.** The summary reports the contour/guide counts,
the method actually used (with `auto`, the log's `strategy` line tells you
which cascade stage won — e.g. `loft + skeleton centreline`), point and
triangle counts, bounds/area/volume in file units, and
`watertight` / `mesh_ready`. It also records the file's `sha256` — that
plus the knob values in the journal is a complete provenance trail.

---

## 4. From surface to volume mesh

Two volume meshes are available from the same CSO surface:

- **`volmesh`** — meshes the vessel **WALL** (a thin shell of thickness
  around the lumen), for structural FEA of the wall.
- **`lumen`** — fills the **blood volume** (the inside), for cardiac
  CFD/FEA of the flow domain (V47). Swept O-grid hexahedra where the
  geometry permits, an automatic gmsh tet fallback with a loud
  fold-number note where a section is too eccentric for the structured
  fill. Its result takes the session volume-mesh slot with
  `wall`/`inlet`/`outlet` groups, so `foam`/`cfd`/`deck`/`solve` run on
  it unchanged. On both real UCSF families all 10 arches reach
  `inverted 0` (9 structured hex + 1 honest tet on the Test-Scans set;
  10/10 structured hex on the Example set). CLI: `digitwin lumen
  SURFACE.vtp` — the loft's embedded centreline is reused automatically.
  Each end is anchored on the loft's flat end cap, so the `inlet`/`outlet`
  patches ARE the caps (the log gives each patch's area against its cap's).
  Note that contour #0 of these exports is a small seed contour at the
  aortic root: the surface pinches down to it, and the log says so when an
  end narrows sharply — a CFD inflow there enters through that pinch.

In the **volmesh** panel:

| Knob | Default | Notes |
|---|---|---|
| `backend` | `swept` | `swept` = structured hex (θ × axial × radial); `tet` = gmsh tetrahedra. Use swept for tubes. |
| `thickness` | 0.5 | Nominal wall thickness in file units. On tight concave bends the applied thickness is automatically capped to avoid self-intersection — the log tells you how many rays were capped and the minimum applied thickness. |
| `n_theta` / `n_axial` / `n_layers` | 32 / 60 / 4 | Lattice resolution: azimuthal divisions, axial stations, radial layers. |
| `layer_ratio` | 1.0 | Radial grading (outer/inner layer thickness). |

The loft's chosen centreline is embedded in the surface itself, so volmesh
reuses it — this works even across a save/load of the surface `.vtp`
(the CLI two-step in §6 relies on this).

**Quality gate:** `inverted=0` is non-negotiable — DigiTwin refuses to
solve on a mesh with inverted cells. If you ever see inverted > 0 on a CSO
import, re-run import_cso with `method=voxel` (or just `auto`, which does
this dance for you), or coarsen `n_axial`/`n_theta`.

---

## 5. DICOM anchoring (optional, for image-aligned masks)

If you have the CT series the contours were traced on, set the `dicom`
knob (or CLI `--dicom`) to the `.dcm` file. DigiTwin then:

- verifies it is the axial, axis-aligned multi-frame series DigiTwin
  supports (anything else — oblique, enhanced SOP — is refused loudly);
- voxelises the contours **onto the scan's own image lattice** (correct
  slice spacing from the header), ROI-cropped, so the resulting mask/
  surface is in exact register with the image;
- records the DICOM's sha256 in provenance alongside the CSO's.

This forces `method=voxel`. Use it when downstream work must line up with
the image (e.g. comparing against voxel measurements); skip it when you
just want the smoothest possible surface (loft).

---

## 6. Command line (single files and batches)

Same engine, same knobs:

```bash
# contours → watertight surface (+ provenance JSON)
digitwin import-cso scan.cso --outputs outputs/mycase
#   options: --method auto|loft|voxel   --n-ring N   --voxel-size S
#            --no-cap   --on-open skip|close|error   --dicom series.dcm

# surface → wall volume mesh (reuses the embedded centreline)
digitwin volmesh outputs/mycase/scan_surface.vtp --thickness 1.5 \
    --outputs outputs/mycase

# volume mesh → complete solver deck (ccx/abaqus/nastran/lsdyna/ansys)
digitwin deck outputs/mycase/scan_surface.vtp --thickness 1.5 \
    --material elastic --E 1000 --nu 0.3 \
    --fix inlet,3 --pressure inner,0.016 --target ccx
```

Batch a folder:

```bash
for f in "Example CSO Files"/*.cso; do
  digitwin import-cso "$f" --outputs "outputs/batch/$(basename "$f" .cso)"
done
```

Every command exits nonzero on failure and prints why (fail-loud rule).

---

## 7. Troubleshooting (what the messages mean)

| Message / symptom | Meaning / fix |
|---|---|
| `truncated CSO: needed N more byte(s) for … at offset …` | The file is cut short (bad copy/transfer). Re-export or re-copy it; DigiTwin refuses partial parses of clinical data by design. |
| `CSO record N: implausible point count …` | Corrupt or non-CSO bytes where a contour was expected — the file is damaged or not Binary CSO (1,6,0). |
| `coordinate magnitude … exceeds 1e+09` | A control point (or plane normal) is astronomically large — the file is corrupt (real coordinates are millimetres). Refused because such values silently overflow the meshing math into `NaN`. Re-export the segmentation. |
| An `open editor contour` refusal / a "dropped open contour" note | A ring was left unclosed in MeVisLab. Default (`skip`) drops it and says so; use `on_open=close` for near-closed rings, or fix the segmentation and re-export. |
| `unknown tag` errors | The file uses CSO features DigiTwin doesn't decode yet (e.g. VEC2/VEC4/MAT payloads). Send the file to the developer — support is added on evidence. |
| Import succeeded but the desktop window still shows the old geometry | The Workbench polls the session revision and re-syncs itself within a couple of seconds (session 34). If it genuinely doesn't, ⌘R still works — and say so, that's a bug. |
| `inverted=N` (N > 0) after volmesh | Use `method=auto` on import (its trial-sweep exists precisely to prevent this), or fall back to `method=voxel`; coarsening `n_axial` can also help on extreme bends. |
| `give either --thickness or --outer-surface` | volmesh needs exactly one thickness source. |
| `SDF grid … voxels exceeds the … budget … raise thickness: T or more fits` | The **wall** step sizes its signed-distance grid off `thickness` (grid spacing = `thickness/3`), so a thin wall on a scan-sized bounding box asks for an impossible grid. There is no `spacing` knob — **raise `thickness`**. The message quotes the smallest value that fits this surface; `1.5` is the recommended figure for a mm-scale scan (§5). The stock default `0.5` is sized for the synthetic tube, not for clinical geometry. The refusal is immediate — nothing expensive has run yet. |

---

## 8. Honest limits (what CSO import can NOT do yet)

- **Structured hexes need a moderate fold number:** the `lumen` fill
  (§4) sweeps structured O-grid hexahedra where the fold number
  F = r·κ stays below ~1; a section too eccentric for a planar O-grid
  falls back to gmsh **tetrahedra** (loud note) rather than folding —
  1 of 10 real arches on one family. All-hex on the most tortuous
  arches is a separate research track (non-planar sections).
- **Uniform nominal thickness:** one thickness value per mesh (plus the
  automatic curvature cap) — no per-station or patient-specific
  thickness maps.
- **Tag coverage:** only the tags observed in real exports are decoded;
  exotic payloads (VEC2/VEC4/MAT) refuse loudly rather than guess.
- **DICOM support** is scoped to axial axis-aligned multi-frame CT
  (the format of the validated scanner exports); anything else refuses
  with an explicit reason.

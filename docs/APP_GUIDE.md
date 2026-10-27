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
first` you see next to the Run button *and* in the log pane — run the step
panels top-to-bottom). Starting from a file means starting at the matching
**import_\*** panel, which is why those sit directly under **source**.

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
  step (built live from `/schema`, ordered by the server's `step_order`:
  the four **import_\*** panels first, then centerline -> wall -> volmesh
  and the rest), each with its knobs and a **Run …** button. Panels are
  collapsible and start collapsed except **source** and the imports; the
  open/closed state is remembered. A step's error appears inline under its
  own Run button as well as in the log. While a step runs its button reads
  **Running…** and is disabled, so a second click cannot start a duplicate.
  The column scrolls; run it top-to-bottom.
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
- **Right — twin, log & history:** on top, the **your twin · v0** panel
  (the copilot report's twin card, live — see §3.9c), then the **log**, the
  **history** table (the journal — it mirrors exactly what a `.mfwork` file
  replays; failed steps never enter it), and the **Save / Load work file** +
  **Reset session** actions.
- **No local folder on screen:** the history, the summary line and the log
  show an absolute path by its file name and a `/Users/<name>` prefix as
  `~` (display only — the journal and work files keep the real paths), so a
  recording never shows a user name.

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
- `curvature_cap` — swept: how far toward the fold radius a wall ray may
  reach on a concave bend (0.5 default; 0.9 for an aneurysm sac).
- `sweep` — swept: where the stations come from.
  - `centreline` (default): rays square to the centreline. On a short,
    wide, eccentric segment (a cropped sac) the centreline bends tighter
    than the section radius, neighbouring ray fans cross, and the wall
    inverts or gets capped/floored whatever the cap.
  - `planes`: needs exactly two flat end caps, parallel within 2° (a
    `crop` whose planes both took the chord axis, or any segment cut by two
    parallel planes). Stations are planes square to the cap normal, evenly
    spaced from cap to cap — planes never cross, so the stations cannot fold.
    Each station is the plane's section of the surface (the cap's rim at the
    two ends), sampled evenly along its length; the nodes then follow the
    surface from station to station. The wall is offset **in the plane** by
    t/cos(tilt), so the thickness along the surface normal is still t and the
    `inlet`/`outlet` faces lie exactly on the caps. Sections need not be
    star-shaped.
  - `auto`: the centreline sweep when its wall is clean (0 inverted, 0
    capped, 0 floored), else the planes when the caps are parallel; the
    first log note says which and why.
- `side` — planes: `outward` (default; the surface is the lumen side) or
  `inward` (the surface is the OUTER wall — a recovered sac's edge is the fat
  plane — and the wall grows into it). `inner`/`outer` keep their meaning:
  `inner` is always the lumen side.
- `max_tilt` — planes: the steepest surface the in-plane offset follows,
  degrees between the surface normal and the station planes (default 80;
  at 80° the offset is 5.8 t and the cells' scaled Jacobian is about 0.17).
  Steeper nodes keep a 5.8 t offset and count as **capped**.
- `section_smoothing` — planes: Gaussian smoothing of each section along
  its length before sampling, in length units (default 0 = exact). Use it
  only for a surface rougher than the wall thickness: notches narrower than
  2t make the offset cross itself (counted as capped). The log and summary
  report how far the wall's surface-side nodes then sit from the input
  surface (`nodes_off_surface max/p95`) and the largest section-area change.
- **Run volmesh** → log reports cells, `inverted` (MUST be 0),
  `sj_min` (scaled Jacobian, quality floor), volume, and the named
  boundary groups (`inner/outer/inlet/outlet` for tubes) — these
  group names are what BCs and loads reference later.

### 3.5b lumen  (needs: a tubular surface; fills the INSIDE, V47)
Where `volmesh` meshes the vessel WALL, `lumen` fills the **blood
volume** — the domain cardiac CFD actually solves on.
- `method` — `auto` (default: swept O-grid hexahedra, automatic gmsh
  tet fallback with a loud note when the structured fill fails),
  `ogrid` (force structured, loud failure), `tet` (force tetrahedra),
  `surface-tet` (tetrahedra filling the input surface itself).
  - Where the centreline bends tighter than the vessel radius, the
    O-grid stations and the tet fallback's station rings overlap, so
    both fail.
  - In that case `auto` falls through to `surface-tet`. It needs no
    centreline: gmsh reparametrizes the surface (splitting at 40°
    creases), remeshes it at an even size and fills it.
  - It needs exactly two flat end caps, so crop first. The cap nearest
    the centreline's start is `inlet`.
  - Cap faces are tagged by lying on their cap's plane, so each patch
    is the whole cap.
- `n_theta` / `n_axial` — rays per station / stations along the
  centreline (the validated centreline embedded by `import_cso` is
  reused automatically).
- `smooth` — guarded smoothing iterations; the do-no-harm guard means
  `sj_min` can never regress (0 = off).
- `core_fraction`, `mesh_size` — O-grid core size; tet target size.
- End caps need no knob: each end the surface closes with a flat cap is
  anchored on it, so the `inlet`/`outlet` patch IS the cap. The log gives
  each patch's area against its cap's, flags an end where the surface
  narrows sharply, and says loudly when an end has no flat cap (that end
  keeps the section square to the centreline).
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

`import_image` maps the volume's full voxel-to-world affine: a NIfTI with
rotated or negative (LPS-style) axes is placed exactly, no resampling. A
NIfTI sets the length unit to mm (declared mm; metres/microns converted;
an undeclared unit is *assumed* mm and the log says so).

### 3.6a segment  (AI segmentation: CT → aorta surface)
Outlines the aorta on a CT with **TotalSegmentator** (`total` task, ROI
`aorta`) and makes it the session SOURCE, like an import step.
- `path` — the CT: `.nrrd` (attached header, raw or gzip, LPS/RAS space)
  or `.nii` / `.nii.gz`. **Browse…** in the desktop window.
- `device` — `mps` (Apple GPU, default) or `cpu`.
- `fast` — `1` = TotalSegmentator's faster 3 mm model.
- `smoothing` — Gaussian blur of the mask in voxels before the surface
  (default 1, `0` = off).
- `timeout` — wall-clock limit for TotalSegmentator, seconds (default 1800).
- `lumen_out` — optional `.vtp` path: also write the **contrast blood
  lumen**, the CT voxels ≥ `lumen_hu` (default **150 HU**) inside the AI
  outline, largest connected piece, through the same surface path. This is
  a **CT threshold inside the AI label, not an AI segmentation**, and the
  output says so. Calcified plaque is bright too: plaque detached from the
  lumen is dropped and counted ("dropped N piece(s) / M voxels"); plaque
  touching the lumen stays in it. The summary also gives the lumen's share
  of the AI mask and their Dice. Use it when the AI label includes the
  thrombus (seen on stent-grafted sacs); on a native sac TotalSegmentator
  v2.18's `aorta` can already be the lumen alone, and the two nearly
  coincide.

What happens: the CT is copied (NIfTI) or converted (NRRD → RAS NIfTI)
into a temp folder without spaces, TotalSegmentator runs there as a
separate program, and the folder is deleted. Your CT is only read, and it
is checksummed before and after. The mask keeps its largest connected
piece (the log reports what was dropped), is cropped to its bounding box,
blurred, and turned into a closed surface at level 0.5. Where the aorta
leaves the scan, the surface is closed by a flat cap at the edge of the
field of view.

**Frame and units:** the surface is in **RAS world coordinates, millimetres**
(the NIfTI convention). The session length unit is mm, so the cardiac CFD
presets work downstream.

**Install TotalSegmentator once, in its own environment** (it brings
PyTorch, which meshforge itself never imports):
```bash
conda create -n totalseg --solver=libmamba python=3.11
conda run -n totalseg pip install TotalSegmentator
```
meshforge finds it through `MESHFORGE_TOTALSEG` (the executable's path),
then `TotalSegmentator` on `PATH`, then
`~/miniconda3/envs/totalseg/bin/TotalSegmentator`. The first run downloads
the model weights. meshforge never changes TotalSegmentator's own settings;
if its usage statistics are on (`~/.totalsegmentator/config.json`), the log
says so.

### 3.6b sac  (outer aneurysm sac from a CT and a lumen mask)
On a native (non-stented) abdominal aortic aneurysm, an AI `aorta` label
often traces only the contrast-filled lumen. Wall-stress work needs the
outer sac: lumen + intraluminal thrombus (ILT) + wall. `sac` recovers it
from the CT around the lumen and makes it the session SOURCE.
- `path` — the CT in HU (`.nrrd`, `.nii`, `.nii.gz`). **Browse…** in the desktop window.
- `lumen_mask` — the lumen mask on the **same grid** (for example the mask
  `meshforge segment` writes). **Browse…**
- `lumen_hu` (150) — mask voxels at or above it are contrast lumen. The rest
  of the mask stays inside the sac but does not count as lumen.
- `fat_hu` (-30) — fat or air below this marks the outer edge.
- `max_thickness_mm` (40) — the longest thrombus + wall searched beyond the lumen.
- `n_rays` (72) — rays per axial slice.
- `roi_mm` (150) — height above the lumen's lower end to process. The
  infrarenal aorta from the bifurcation up is the usual target. `0` = the
  whole mask.
- `min_resolved` (0.5) — refuse when fewer rays than this fraction find a fat plane.
- `smoothing` (1) — Gaussian blur of the sac mask in voxels before the surface.

**How it works.** On every axial slice, rays leave the lumen centroid. Each
ray walks outward from the lumen edge and stops at the first of four things:
- fat or air sustained over 1.5 mm. The sac edge is found (*resolved*).
- a bright run ≥ 100 HU over 3 mm (*bright*). This is another vessel, bone,
  or an enhancing organ, such as the duodenum crossing in front of the sac.
  Thrombus is about 20–60 HU. A shorter bright run is wall calcification and
  is walked through.
- the scan edge (*fov*).
- 40 mm without fat (*no_fat*). Unenhanced tissue (IVC, psoas, bowel) touches
  the sac with no fat plane.

Errors are one-sided: a ray that crosses an abutting organ finds the fat
behind it. So two kinds of resolved ray are demoted to *outlier*:
- one far outside a robust fit of thickness around the circle;
- one in a short run far behind a neighbouring bright stop.

Unresolved rays are filled from their resolved neighbours, by thickness. The
radius is then smoothed around the circle and along z. It is never let past a
ray's upper bound: the sac holds no fat, bone or organ. The sac always
contains the lumen.

**What you get.** A closed surface in RAS millimetres with two point fields:
- `ilt_thickness_mm` — the lumen-to-sac distance along the axial ray
  (thrombus + wall), for a thickness map;
- `sac_resolved` — 1 where the nearest ray actually saw fat, 0 where it was
  filled in.

The step text and the CLI report the following:
- the resolved fraction, per slice and overall;
- the count of every stop reason;
- the maximum diameter: largest caliper, short axis and area-equivalent;
- thrombus + wall thickness statistics.

Read the resolved fraction as the method's own uncertainty. A sac edge that
touches an organ with no fat plane is interpolated, not measured. The
thickness is an in-plane (axial) distance, so on a tilted segment it reads
long by 1/cos(tilt).

**Refusals (loud):** an empty lumen, a lumen touching the in-plane edge of the
scan, slices more than 20° off axial, or too few resolved rays.

**CLI:**
```bash
meshforge sac CT.nii.gz --lumen-mask lumen.nii.gz --out outputs/sac/case_sac.vtp \
    [--reference-zenodo .../Dataset_root/<case>]
```
This writes `case_sac.vtp`, `case_sac_mask.nii.gz` (the sac mask on the
region-of-interest box of the CT grid) and `case_sac.json`. With
`--reference-zenodo`, the sac is scored against the Zenodo AAA reference
segmentation (Dice, ASSD, HD95). That is agreement with a reference, not
accuracy.

Validation on the 20 Zenodo cases (doi 10.5281/zenodo.19182977, CC BY 4.0):
- 16 accepted, 4 refused (tight ~80 mm crops);
- median Dice 0.82 vs lumen + thrombus and 0.89 vs lumen + thrombus + wall;
- median ASSD 3.1 / 2.0 mm.

On clean fat edges, the -30 HU edge sits about 1 mm outside the reference
wall. To reproduce, run `scripts/validate_sac_zenodo.py`. Overlays come from
`scripts/sac_overlay.py`.

Demo data: a native AAA from AortaSeg-60 (doi 10.5281/zenodo.18147026, CC BY
4.0). Run `scripts/prep_aortaseg60.py` on it first.

### 3.6c crop  (aorta → infrarenal aneurysm segment, flat caps)
Crops the session surface — a closed aorta in **millimetres, +z superior**
(what `segment` makes; RAS or LPS) — to its infrarenal aneurysm, and makes
the cropped piece the session surface. Everything is automatic:

1. **Profile.** A smoothed skeleton centreline (run out to the surface at
   both ends; caudal end = lower z) and, every 1 mm, the equivalent
   diameter `d = 2·sqrt(area/π)` of the section square to it, median over
   5 mm. `h` = mm above the caudal end.
2. **Landmarks.** *Bifurcation*: h = 0, where the aorta label ends or
   splits. *Flare top*: past the end's own rise, the first local minimum of
   `d` (0 when `d` climbs straight into the sac). *Sac*: the widest `d` up
   to 200 mm. *Neck*: the first stretch above the sac that stays
   non-dilated for ≥ 10 mm, non-dilated meaning `d ≤ max(1.2 × d_ref,
   d_ref + 2 mm)` with `d_ref` the narrowest `d` within 150 mm above the
   sac. Renal arteries are not in the aorta label, so the reported neck
   length is the non-dilated length above the sac.
3. **Cuts.** Distal: `distal_margin_mm` (default 10) above the flare top.
   Proximal: `neck_extension_mm` (default 25) above the neck start — the
   flow/clamp extension — held inside the neck. Each plane is square to the
   local vessel axis (the line through nearby section centres), or to the
   segment axis where there is no straight local axis (a dome, an eccentric
   sac). A cut that opens two loops, grazes the wall, or crosses an end cap
   the input already had moves 2 mm toward the sac and tries again.
4. **Caps.** Each opening is closed by an **exactly planar** cap (rim
   vertices snapped onto the plane, ~1e-13 mm), so the lumen's end-cap
   anchoring and the wall lattice see truly flat, square ends.

Knobs: `neck_extension_mm`, `distal_margin_mm`; `distal_height_mm` +
`proximal_height_mm` to place both cuts by hand (mm above the caudal end;
landmarks are then reported but not enforced); `lumen` + `lumen_out` to cut
a second surface — the blood lumen from `segment`'s `lumen_out` — with the
**same two planes**, so outer and lumen share their caps. **Browse…** on
`lumen` (open) and `lumen_out` (save).

It **refuses loudly**, printing the measured profile (`h mm:d mm` every
10 mm), when there is **no sac** (the widest section is less than 1.5× its
neck) or **no neck** (the surface ends before 10 mm of non-dilated aorta
above the sac). The report lists sac height, sac equivalent and maximum
diameter, neck start/length/diameter, sac/neck ratio, both cut planes
(origin, outward normal, cap diameter, planarity, rim crease) and notes.

The cropped surface carries its own **segment centreline**, leaving each cap
along its normal; `volmesh` and `lumen` use it automatically. On a wide sac
mesh the wall with `--curvature-cap 0.9` (volmesh knob `curvature_cap`): the
default 0.5 caps and floors wall rays in the sac (0.3 mm "walls" that read as
a fake stress hotspot), and the `wall rays` line reports capped/floored
counts — a stress result on a capped or floored wall is not reportable.

When the crop's segment centreline still folds (a short, eccentric sac that
kinks into the neck: the crop note gives a fold number above 1), add
`--sweep auto` (or `planes`): when both planes took the chord axis the caps
are parallel and the wall is swept on planes parallel to them instead. On a
recovered sac (`sac`) the surface carries millimetre-scale axial terraces;
if the plane sweep then reports a few capped nodes, `--section-smoothing 4`
smooths them out and says how far the nodes moved (Anev_02: ≤ 1.5 mm):

    meshforge volmesh sac_crop.vtp --thickness 1.5 --curvature-cap 0.9 \
        --sweep auto --section-smoothing 4

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

### 3.9a aaa_stress  (AAA wall stress under blood pressure — needs: a swept hex wall)
An **investigational** wall-stress metric for an abdominal aortic aneurysm —
not a diagnosis and not a rupture prediction. It runs on the session's swept
hex wall (`volmesh`, e.g. the 1.5 mm wall on a cropped sac, §3.6c), or from
the CLI on a saved wall `.vtu` with its `<stem>.summary.json`.

**Method (stiff-linear on the loaded geometry).** The CT shows the aorta
already pressurised, so a linear elastic, small-strain solve on that geometry
gives a stress set by the geometry and the pressure, not by the modulus
(changing E ten-fold moves it by 0). Units are mm, N and MPa (1 mmHg =
133.322 Pa). CalculiX, two real solves: the `systolic_mmhg` (default 140) and
the `whatif_mmhg` (default 120) pressure, each uniform on the wall's lumen
side (`inner`), both ends (`inlet`, `outlet`) clamped (encastre), as in the
Zenodo AAA dataset's own model. Wall E 100 MPa, ν 0.49. The hexes are solved
as 20-node quadratic hexes (C3D20R: mid-edge nodes added on straight edges,
so the geometry is identical) — 8-node C3D8 locks at ν 0.49 and C3D8I reads
10–21% low on the tilted cells of a real sac wall.

**What it reports** (outside the end zones; the zone next to each clamp is
dominated by the clamp):
- **PWS** — the 99th percentile of the von Mises stress, volume-weighted over
  the integration points (2×2×2 per hex), in kPa;
- the 99th percentile of the max principal stress, and the von Mises max;
- the **peak location** (coordinates, height above the distal cap) and the
  centroid of the top-1% region;
- **wall tension** (N/mm) p50 / p99 / max: the through-thickness resultant
  of the in-plane stress, its larger principal value. It is the wall
  thickness × the layer-mean in-plane stress only when every radial column of
  hexes is that thick along the surface normal (within 2%). Otherwise the
  stress is integrated along each column's measured normal thickness: a
  centreline ray that crosses a sac's shoulder at an angle builds a column
  thinner than the ray, and thickness × stress would over-read there (the
  summary's `tension_definition` says which rule applied). On the loaded
  geometry the tension does not depend on the assumed thickness (1.2 / 1.5 /
  2.0 mm agree within the test's 2%; D88 measured 0.3%), while the PWS
  scales as 1/t;
- the what-if: PWS at 120 / 140 mmHg (= 0.857 — the solve is linear).

**End zones** (`end_zone`): `neck` (default) — one neck diameter from each
cap plane, from the crop report (`crop_report`, or this session's `crop`
step), else each cap's own equivalent diameter; `bending` — 3 shell bending
lengths `sqrt(R t)/(3(1−ν²))^¼` per cap (where a clamp's local disturbance
has decayed); or a length in mm. If the zones leave less than 10% of the
wall, the PWS is **refused** — the crop is too short for that rule. The
AortaSeg-60 Anev_02 crop is: its caps are 50.7 mm apart and its neck is
24.7 mm, so the default leaves 1.9%; run it with `--end-zone bending`.

**Refusals** (no number, the reason printed, exit 1): any inverted cell; any
capped or floored wall ray (both sweep kinds — planes: tilt + concavity); a
missing or mismatched mesh summary; too little wall outside the end zones.

**Thrombus** (`--lumen`, step knob `lumen`).
- **What to give it:** the blood-lumen surface cut by the SAME two planes as
  the wall's surface (`crop --lumen`), `--sac`, the surface the wall was built from, and `--crop-report`, which carries
  those planes.
- **The thrombus solid:** the region between the lumen and the sac (the
  wall's inner surface), filled with quadratic tets (C3D10, `--ilt-size`
  3 mm). The lumen is re-cut 0.5 mm inside the caps so the two surfaces
  never touch.
- **The mesher cannot hang:** gmsh can spin forever on a cut surface with
  near-zero edges. Each gmsh call runs in its own process and is stopped
  after 60 s with a readable error. The mesh is tried up to four ways (edges
  under 0.009 mm collapsed first, then the surfaces as given, then two other
  tolerances), and an attempt counts only if it fills sac − lumen within 2%
  with a scaled-Jacobian minimum of at least 0.02. The notes say which
  attempt was used.
- **Coupling and loads:** the thrombus is tied to the wall without moving
  any node. The blood pressure then acts on the **lumen**, as it does in the
  body. The thrombus ends are free; the wall carries the clamp.
  - **How each face is labelled:** each face of the thrombus is labelled by
    the surface it lies on (lumen: pressure; sac: tied). This holds however
    thin the thrombus is.
  - **The re-cut caps:** the lumen's caps, 0.5 mm inside the cut planes,
    are free too. Pressure there would add an end load that the blood,
    which continues past the cut, does not apply.
  - **Refusals and notes:** a face that lies on neither surface, or on both,
    stops the run with a reason. The notes give the pressure / tied / free
    face counts.
- **Material:** `--E-ilt` defaults to 5 MPa (wall 100 MPa, ν 0.49 both). These
  are the Zenodo AAA dataset's own FE values, so our numbers compare with
  theirs. Physiological thrombus is softer; the ratio is what matters on
  loaded geometry.
- **Checks:** the wall hoop stress matches the two-layer Lamé solution within
  0.7%, and a near-zero-stiffness thrombus passes the pressure through as
  if it were absent. On an off-centre lumen with a 0.8 mm thin side, every
  lumen face carries pressure and every sac face is tied.
- **Why it matters:** without it the stress is overstated. On a native AAA,
  674 vs 287 kPa at 140 mmHg.

**Assumptions it always prints:** investigational; wall thickness assumed
1.5 mm, uniform; the thrombus line (either "modelled: ..." or "**no
intraluminal thrombus modelled, so wall stress is overstated where thrombus
is present**"); stiff-linear on the loaded geometry; uniform static
pressure, both ends clamped.

Output (`outputs/aaa_stress/session/` from the Workbench; the CLI's
`--out-dir`): `<stem>_aaa_stress.vtu` — point and cell fields `von_mises`,
`max_principal`, `tension` (MPa, MPa, N/mm; `_whatif` for the second
pressure), `excluded_end_zone`, `DISP`; `<stem>_aaa_stress.json`; the two
`.inp` decks with their `.dat`/`.frd`.

```bash
meshforge aaa-stress outputs/m2/e2e/Anev_02_sac_wall_auto_s4.vtu \
    --crop-report outputs/m2/e2e/Anev_02_crop_crop.json \
    --systolic 140 --whatif 120 --end-zone bending --out-dir outputs/m3/anev02
```
(AortaSeg-60 Anev_02, a native AAA: PWS 889 kPa at 140 mmHg, 762 kPa at
120 mmHg, ratio 0.857; 30 s.) `python -I scripts/m3_stress_maps.py
<stem>_aaa_stress.vtu outputs/m3/<name>` renders the stress map and the
140-vs-120 pair off-screen.

**Validation.** Against closed forms: a clamped cylinder (hoop tension = P·a
to 0.6%, PWS = the Lamé von Mises to 0.2%), a sphere-like bulge (Young–
Laplace at the apex, +2.9%), a fusiform sac (general Laplace −0.5%; PWS 0.83
of P·R/t, inside the membrane bounds 0.75–1.05). Against the Zenodo AAA
dataset's published FE result (`scripts/m3_tension_validation.py`,
`outputs/m3/tension_validation.md`), on all 12 cases whose crop used the
automatic landmarks and whose wall passed the gate, at their 16 kPa: median
Spearman ρ 0.70 (0.42–0.89), median offset between their and our top-1%
tension regions 9.8 mm, our p99 / their p99 1.42 (p50 1.56). Ours reads
higher because our wall carries no thrombus: their pressure acts on the
lumen, inside the thrombus. Across the cases our/their p50 follows the
thrombus thickness (Spearman 0.90); on the thin-thrombus case P12 it is 1.12
with ρ 0.89. (These wall-only numbers used the earlier nominal-thickness
tension; recomputed from the same solves with the current rule they read ρ
0.72, offset 4.0 mm, p99 1.26, p50 1.39.)

**With the thrombus modelled** (`MESHFORGE_TENSION_ILT=1`,
`outputs/m3/tension_validation_ilt.md`), on the same 12 cases:
- median ρ 0.98 (0.83–0.99); the lowest is P07, a 46.6 mm crop that leaves a
  6 mm comparison band;
- top-1% offset 0.95 mm; p99 ratio 0.98 (0.93–1.00 in every case); p50
  ratio 0.96.
- The p99 hotspots that P04 (1.62) and P17 (1.91) showed before 20 Oct were
  ours: the validation's gate walls use the centreline sweep, whose 1.5 mm
  rays cross the sac's shoulders at an angle and build columns only 0.80 /
  0.57 mm thick along the normal there, and the tension multiplied their
  stress by the nominal 1.5 mm. Integrated along the measured normal
  thickness (the rule above) they read 0.98 / 0.95.

Data credits: Zenodo AAA 4D-CTA dataset, doi 10.5281/zenodo.19182977,
CC BY 4.0 (its surfaces are a *reference segmentation* and its tension maps
are *the dataset's published FE result* — neither is ground truth);
AortaSeg-60, doi 10.5281/zenodo.18147026, CC BY 4.0.

### 3.9b tissue_index  (wall stress ÷ lab-measured strength — needs: a peak wall stress)
An aneurysm bursts when wall stress exceeds wall strength. The scan and the
solve give the stress. Strength can only be measured by testing tissue in a
lab. `tissue_index` divides the peak wall stress (PWS) by the measured
strength of real aneurysm wall and returns a band. **It is an investigational
index, not a diagnosis.**
- `pws_mpa`: the peak wall stress in MPa, as Cauchy stress (what the
  stiff-linear solve on the loaded geometry gives).
- `from_summary`: instead of `pws_mpa`, a wall-stress summary `.json` that
  contains a `pws_mpa` value anywhere in it. **Browse…** The file is only
  read, and its checksum goes into the journal. Several different `pws_mpa`
  values, or none, are refused.
- `selection`: which strips supply the strength.
  - `aaa` (default): the 13 rupture-border strips, i.e. aneurysm wall.
  - `control`: the 11 non-dilated infrarenal strips from the same aortas.
  - `all`: both.
- `method`: `empirical` (default) for quantiles over the strips, or
  `lognormal` for a lognormal fit.

**The strength library (v0).** It is open data: uniaxial tests of 24 wall
strips from 7 aneurysms removed at autopsy (USP AAA dataset, see credits
below). Strength is the Cauchy failure stress, F·λ/A₀ with the tissue taken as
incompressible, so it is compared like with like against the Cauchy wall
stress. Two strips reached their highest stress only after the load had
already peaked and dropped. They enter at that first failure. The band is
the strength P10/P50/P90, and **index P90 = PWS ÷ strength P10**, the
weak-wall end.

The output always states n ("open strength data: 7 aneurysms, 13 strips —
our lab narrows this"). It also gives sensitivities: the other method,
engineering strength (to show why the measures must match), late peaks at
their reported maximum, and raw-curve strips only. The controls appear as a
comparison. Example:
```bash
meshforge tissue-index --pws 0.30          # or --from-summary outputs/m3/aaa_stress.json
# rupture index : P10 / P50 / P90 = 0.231 / 0.461 / 0.974
```

**Credits.** The strength data come from Gomes VC, da Silva ES, da Silva LF,
Gomes J, Raghavan ML, Silvestre G, Queiroz A, Marques M, Zyngier SP, Chung
TK-J, *Dataset on cadaveric human abdominal aorta aneurysm biomechanics
(uniaxial) and histology*, Mendeley Data v3,
doi [10.17632/x64srrc39p.3](https://doi.org/10.17632/x64srrc39p.3), CC BY 4.0.
The packaged table is derived from it: units converted, Cauchy stress
recomputed, flags added. Credit the authors when you use it. How it was built,
and its limits, are in `src/meshforge/data/STRENGTH_V0_SOURCES.md`. The main
limits: autopsy tissue, aortas inflated to rupture before testing, test
direction not recorded, and a small n.

### 3.10 cfd  (OpenFOAM — needs: volmesh saved as a foam case, or `case`)
Knobs: `case`, `solver` (`simpleFoam` steady / `icoFoam` transient),
`nu`, `inlet` ("front=0,0,1", or a speed "front=150" — §3.10a),
`outlet` ("back=0"), `end_time`,
`delta_t`, `write_interval`, `timeout`, `turbulence`
(`laminar`/`kOmegaSST`), `intensity`, `turb_length` (REQUIRED for
kOmegaSST, hint 0.07·D), `series` (1 = read every written time step
back as animation frames), `moving_wall` ("lid=1,0,0"), `empty`
("front,back"), `closed_cavity` + `p_ref_cell` + `p_ref_value`
(sealed domains). With `series=1` the viewport's **play/slider** arms —
step the U/p fields through time.

### 3.10a cfd: pick the flow ends, then see the flow
The **cfd** panel opens with a **flow ends** box. It only fills the
panel's own `inlet` / `outlet` / `nu` knobs, as plain text, so the
history, a work file and its replay record exactly what you could have
typed. It needs a lumen (or volmesh) with exactly two flat end caps;
anything else is refused with the reason — set the knobs by hand.
- **Pick inflow end**, then click the vessel near the end the flow comes
  IN. The nearest cap becomes the inflow, the other the outflow (p = 0).
  A drag still rotates the view, a click on empty background asks again,
  and pressing the button again cancels.
- **Swap** exchanges inflow and outflow.
- **speed** — the inflow speed in model units per second (the unit is
  shown beside the field). It is written as `inlet=<inflow>=<speed>`:
  that speed along the patch's inward normal, measured on the mesh and
  uniform over the patch (a plug profile, the same as the vector form).
  You never work out a direction or its sign.
- **speed preset** / **fluid** — cardiac values converted to the
  source's length unit. CSO and DICOM sources are millimetres: blood
  ν = 3.3 mm²/s (Newtonian; μ 3.5 mPa·s, ρ 1060 kg/m³), aorta cycle-mean
  at rest 150 mm/s, ascending-aorta peak systole 1000 mm/s. STL/VTK,
  synthetic shapes and gmsh/CAD imports declare no unit, so the presets
  stay empty and say why — type ν and the speed in your model's units.
- **Live preview**, under the fields, before anything runs: U·n, the
  inward normal, the inflow area and equivalent diameter D_eq, the flow
  rate Q (also in L/min when the unit is known) and Re = U·D_eq/ν. Above
  Re 2300 it warns that a steady laminar solution may not exist (expect
  stalled residuals). After a run the same line is in the log. Example,
  a synthetic 6 mm tube at 150 mm/s with blood: `Q 4168 (0.25 L/min);
  Re 270`, simpleFoam converged in 88 iterations.

Typed by hand, `inlet` takes either form. `inlet=150` is refused, with
the reason, on a patch that is not a flat cap (it has no single inflow
direction) and for a speed ≤ 0 (to reverse the flow, swap the patches).
`inlet=0,0,150` works as before; the log warns if the vector points out
of the domain. The CLI `meshforge cfd CASE` has only a case directory,
not the mesh, so it refuses `--inlet NAME=SPEED` and asks for the
vector; to run the speed form outside the Workbench, replay a work file
with `meshforge replay`.

**Flow section.** A CFD result drawn as the vessel's boundary is the
no-slip wall — U = 0, one flat colour. After a cfd run on a session
with a centreline, the viewport instead shows the flow on a curved
section that follows the centreline, turned face-on to the camera.
Toolbar toggles: **flow section** (on; off returns to the boundary),
**wall** (off; draws the wall as a faint translucent shell around the
section), **fit colours** (on: the colour range is the 2nd–98th
percentile of the flow on the section, so a small fast inflow does not
pin the scale; off: the full range). Without a session centreline the
log says so and the viewport shows the boundary — run **centerline**
first. The section is a view only; nothing is saved or exported.

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

### 3.13 Demo mode  (presentation layout; the Demo switch in the top bar)

For a demo or a recording. Flip **Demo** in the top bar, or open the page
with `?demo=1` (it is also remembered in the browser). On:

- the left pane shows the AAA path's six steps as numbered tabs, in the
  order the copilot runs them, and nothing else — **1 Load scan**
  (`segment`: the CT goes in, the AI outlines the aorta; the `sac` recovery
  runs in its turn), **2 Crop aneurysm** (`crop`), **3 Mesh wall**
  (`volmesh`), **4 Wall stress · FEA** (`aaa_stress`), **5 Strength index**
  (`tissue_index`), **6 Blood flow · CFD** (`cfd`; the copilot's pre-computed
  flow metrics fill it). Each tab is the step's own panel, closed: a click
  opens its knobs and Run button. Its mark shows waiting, running (the step
  holding the session, red, pulsing) or done (green ✓: a record for it is in
  the journal, so a re-run upstream puts the tabs after it back to waiting);
  the Mesh wall tab shows failed (red ✕) while the copilot's last `mesh_wall`
  call failed its quality gate — the twin's wall slot says the same, in the
  same words — until a re-mesh lands.
  Quick start, the synth source box and the history table are hidden (the
  session panel stays: Save work file);
- a **new result wipes in** over about two seconds instead of appearing at
  once (a cover over the viewport; the mesh underneath is the final one);
- the **step running on the server** shows bottom-left of the viewport with
  its elapsed seconds ("contouring the aorta (AI outline)… 12 s"), also when
  the copilot drives the server over `--server` (from `GET /state`
  `running` / `running_for`);
- a **planned coverage** panel above the twin: the body outline with the
  circulatory system (CT, MRI, PET), the skeletal system (X-ray) and brain
  tissue (MRI, PET) as layers you toggle; the aorta stays lit as the live
  module. It is an illustration of what the twin is meant to cover, not
  data, and the panel says so.

Off, the Workbench is exactly the one described above. Demo mode changes
nothing about what any step computes.

### 3.9c copilot  (Claude runs the AAA pipeline from a plain request — investigational)
`meshforge copilot "Assess this aneurysm at 140 and 120 mmHg" --ct scan.nii.gz` hands
the pipeline to Claude (Opus 5.5). It has five tools, each one or two of the steps above:

| tool | steps | returns |
|---|---|---|
| `segment_aorta` | `segment` (`lumen_out`, `mask_out`) + `sac` | AI time, sac diameters, thrombus + wall thickness, how much of the sac edge was measured |
| `crop_aneurysm` | `crop` (`lumen`, `parallel_caps`) | sac and neck diameters |
| `mesh_wall` | `volmesh` 1.5 mm, `curvature_cap` 0.9, `sweep` | the quality gate: inverted / capped / floored, and the thickness measured along the surface normal (within 5% of 1.5 mm) |
| `wall_stress` | `aaa_stress` with the thrombus (`lumen`) | PWS at both pressures, % change; the gate again under the peak: the wall under the PWS's top 1% must also be within 5% of 1.5 mm, or the call fails with the same hint |
| `strength_index` | `tissue_index` at both pressures | index P10/P50/P90, strength band, n aneurysms / strips |

How it behaves:
- **Settings:** it starts with the standard ones and changes one only when a
  step says so. A wall that fails the gate gives no stress. The error says
  why, and the copilot fixes the cause and meshes again. The failure stays
  in the record.
  - **On the s1364 demo scan:** the default wall has no bad cells but is
    1.30 mm thick along the surface normal, 13% thin, because the centreline
    sweep's rays meet the sac's shoulders at an angle. Its stress would read
    about 37% high (382 vs 279 kPa when measured, D93). The gate fails it,
    and the copilot re-crops with parallel caps and meshes with the plane
    sweep (1.50 mm, PASS).
  - **Another failure:** "caps 35° from parallel" when the plane sweep is
    asked for on the default crop.
- **The report** (`report.html`/`.md`) is built from the tool outputs alone.
  `report.html` is a real page (no script, no web fetch, prints cleanly,
  one column on a phone): header, twin card, headline tiles (PWS, what-if,
  investigational index), results and pipeline tables (a failed gate stays
  in red), the summary with its numbers-guard note, the caveats and the data
  credit. Every string from a tool, Claude or the command line is
  HTML-escaped. The Reproduce line names the work file only (no folder).
  - **Twin card:** a body outline with the aorta lit as module 1 and heart and
    kidneys "next". Its data-layer slots are shape, wall + thrombus, stress,
    strength (with n), flow (from `--flow-metrics`), BP what-if, and growth,
    which waits for a second scan. Each filled slot shows its tool numbers
    (e.g. "282.5 kPa peak at 140 mmHg"). The `--flow-metrics` file (numbers
    only, checked before the run starts) is journaled in the session as a
    `flow_metrics` record before the report is written, in-process or over
    `--server`, so `session.mfwork` keeps it.
  - **The same card in the Workbench** (right pane, `GET /twin`): every slot
    reads "not run yet" until its step lands, then fills from the session's
    own journal, replayed through the copilot's tool code, so it quotes the
    copilot's exact numbers. Mid-run it shows a failed gate ("last wall gate
    FAIL: 1.3 mm along the normal"). A new source empties it; a re-run crop
    clears the stress and index it made stale. Flow fills from the session's
    `flow_metrics` record (the copilot posts its `--flow-metrics` to
    `POST /twin/flow`), so the panel prints the report's exact flow lines; the
    record belongs to the scan, so a re-crop keeps it and a new source or
    segment empties it.
  - **Claude's prose:** included only if every number in it matches a tool
    output; one correction is allowed, otherwise it is withheld and the
    report says so.
- **The trail:** `transcript.json` (every request, tool call and result) and,
  in-process, `session.mfwork`. `meshforge replay` reproduces every number.
- **Live view:** `--server http://127.0.0.1:8765` runs the steps on a running
  Workbench (`meshforge web`), so its viewport shows each artifact as it
  lands.

Needs the Anthropic SDK (`pip install 'meshforge[copilot]'`, or conda-forge
`anthropic`) and credentials in the environment (`ANTHROPIC_API_KEY`). Every
run calls the API, so it costs money. Model `--model`, effort `--effort` (default
`high`); the server-side refusal fallback is on.

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
| `segment` | CT → TotalSegmentator aorta mask → surface (§3.6a) | `meshforge segment ct.nrrd --out outputs/m1/case_aorta.vtp` (+ `--lumen-out lumen.vtp [--lumen-hu 150]`, `--fast`, `--device cpu`, `--smoothing 0`, `--reference-zenodo <case dir>`; see below) |
| `crop` | aorta → infrarenal AAA segment, flat caps (§3.6c) | `meshforge crop aorta.vtp --out outputs/m2/aaa_crop.vtp --lumen lumen.vtp` (+ `--neck-extension`, `--distal-margin`, `--distal-height`/`--proximal-height`, `--planes-from <earlier _crop.json>`) |
| `tissue-index` | PWS ÷ lab-measured wall strength, P10/P50/P90 with n (§3.9b) | `meshforge tissue-index --pws 0.30` (or `--from-summary <wall-stress .json with pws_mpa>`; `--selection aaa\|control\|all`, `--method empirical\|lognormal`, `--out band.json`) |
| `blocks` | profile → multi-block hexes | §5; add `--project`, `--ogrid`, `--smooth`, `--grade-ratio` |
| `volmesh` | surface → wall volume mesh | `meshforge volmesh tube.vtp --thickness 0.5` (aneurysm sac: `--thickness 1.5 --curvature-cap 0.9`; a cropped sac between parallel caps: add `--sweep auto`) |
| `lumen` | surface → blood-volume mesh (V47) | `meshforge lumen surface.vtp` (O-grid hexes, then tet, then surface-tet fallback; add `--method`, `--n-theta`, `--n-axial`, `--smooth 0`) |
| `blayers` | external boundary layers | `meshforge blayers body.vtp --first 0.1 --n-layers 5 --growth 1.3` |
| `deck` | full solver input deck | `meshforge deck tube.vtp --thickness 1 --material elastic --E 1000 --nu 0.3 --fix inlet,3 --pressure inner,1 --target ccx` (targets: abaqus, ccx, nastran, lsdyna, ansys; add `--analysis frequency/heat/buckle`, `--transient --dt --period --t0`, `--film outer,20,0.5`, `--preloaded`, `--formulation reduced`; thermal stress V45: `--expansion 1e-5 --t-ref 20` + `--temperature 120` or `--temperature-from heat.frd` chaining a solved heat run; V46: `--temperature-time 5.0` picks one snapshot of a TRANSIENT heat history — must match one of the frd's own recorded times, no interpolation) |
| `aaa-stress` | AAA wall stress, investigational (§3.9a) | `meshforge aaa-stress wall.vtu --crop-report crop.json` (+ `--systolic 140 --whatif 120`, `--end-zone neck/bending/<mm>`, `--E`, `--nu`, `--out-dir`; a `.mfwork` session works too; exit 1 = refused) |
| `solve` | run ccx (.inp) or MYSTRAN (.bdf) | `meshforge solve outputs/tube_ccx.inp` (`.k`/`.cdb` refuse loudly — no LS-DYNA/ANSYS solver) |
| `foam` | mesh → OpenFOAM polyMesh case | `meshforge foam tube.vtp --thickness 1 --case outputs/tube_case` |
| `cfd` | run OpenFOAM on a case | `meshforge cfd outputs/tube_case --nu 0.1 --inlet inlet=0,0,1 --outlet outlet=0` (+ `--series`, `--turbulence kOmegaSST --turb-length`, `--moving-wall --empty --closed-cavity`) |
| `fluent` | mesh → FLUENT .msh | `meshforge fluent tube.vtp --thickness 1` |
| `run` | YAML recipe with sweeps | `meshforge run recipe.yaml --set wall.thickness=0.4` |
| `replay` | re-run a work file | `meshforge replay s.mfwork --set volmesh.n_theta=48` |
| `web` | serve the Workbench (browser) | `meshforge web` (add `--port N`) — prints a 127.0.0.1 `/app/` URL |
| `desktop` | the Workbench in a native window | `meshforge desktop` (needs the `web`+`viewport`+`desktop` extras) |

Every command exits nonzero on failure and prints WHY (fail-loud rule).

**`segment` against a reference segmentation.** `--reference-zenodo` takes
one case folder of the public Zenodo AAA dataset (4D-CTA of 20 abdominal
aortic aneurysms, DOI [10.5281/zenodo.19182977](https://doi.org/10.5281/zenodo.19182977),
CC BY 4.0 — credit the authors when you use it), e.g.
```bash
meshforge segment ".../Dataset_root/P01/4D_CTA/70%.nrrd" \
    --out outputs/m1/P01_aorta.vtp --reference-zenodo .../Dataset_root/P01
```
The case's surfaces are the authors' **reference segmentation**, not ground
truth. They are turned into three closed regions by capping each tube on the
dataset's end planes: `lumen` (blood only), `lumen_ilt` (blood + thrombus)
and `full` (blood + thrombus + the dataset's constructed 1.5 mm wall). On the
CT grid, inside the slab between the reference end planes, the command prints
the voxel Dice against each region, the volumes, and the symmetric surface
distance (ASSD = mean of the two directed means, HD95 = the larger directed
95th percentile; measured 2 mm away from each end plane), and names the best
match. The best match tells you whether the segmentation includes the
thrombus. The dataset's image and surfaces agree to roughly 1–2 mm, so
distances of that size are the floor.

**TotalSegmentator needs body context.** The Zenodo images are tight crops of
about 80 mm around the aneurysm. On them TotalSegmentator finds no aorta, and
`segment` refuses the empty mask. So the reference comparison above is a check
of the mask-to-surface pipeline and the metrics, not of the AI. For a real AI
segmentation, use a CT with the full field of view (chest to pelvis or the
whole abdomen). One example is the aneurysm group of AortaSeg-60
(DOI [10.5281/zenodo.18147026](https://doi.org/10.5281/zenodo.18147026),
CC BY 4.0). Its CT headers have the head-foot axis reversed. Correct a copy
first:
```bash
python -I scripts/prep_aortaseg60.py ct/Anev_06_image.nii.gz \
    masks/Anev_06_segmentation.nii.gz ct_fixed/Anev_06_image.nii.gz
```
The script uses the dataset's own mask as a witness. It lays the mask on the
CT both as it is and slice-reversed, and only rewrites the header when the
reversed overlay lands on contrast-filled aorta. Otherwise it refuses. The
voxel data are copied unchanged. On such a scan the TotalSegmentator `aorta`
label covers the whole aneurysm sac, intraluminal thrombus included, not just
the contrast-filled lumen.

Next to `--out` it writes `<name>_mask.nii.gz`, `<name>_totalseg.log` and
`<name>_segment.json` (configuration, timings, mask and surface statistics,
check report, validation).

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
  (+ per-frame `…_t0000.vtu` with `--series`). Segment: the surface at
  `--out` (default `outputs/segment/<ct name>_aorta.vtp`) with its mask,
  TotalSegmentator log and summary JSON beside it (and the threshold lumen at
  `--lumen-out`). The `segment` *step* in the Workbench writes only its
  `lumen_out`; TotalSegmentator's scratch files live in a temp folder that is
  deleted. Crop: the surface at `--out` (default `outputs/crop/<name>_crop.vtp`),
  `<out>_crop.json` (landmarks, planes, caps, full diameter profile) and the
  cut lumen (`--lumen-out`, default `<out>_lumen.vtp`). AAA wall stress:
  `outputs/aaa_stress/<wall stem>/` (or `--out-dir`) — `<stem>_aaa_stress.vtu`
  + `.json` and the two decks with their `.dat`/`.frd`; the Workbench step
  writes to `outputs/aaa_stress/session/`.
- **Public AAA data used in the M2 gate table** (`outputs/m2/`): Zenodo AAA
  4D-CTA dataset, doi 10.5281/zenodo.19182977, CC BY 4.0 (the authors'
  surfaces are a *reference segmentation*, not ground truth); AortaSeg-60,
  doi 10.5281/zenodo.18147026, CC BY 4.0 (Anev_02 native AAA; Anev_04 is
  post-EVAR and never a demo case).
- **Tissue strength data** (packaged, `src/meshforge/data/`): USP AAA dataset,
  Mendeley Data doi 10.17632/x64srrc39p.3, CC BY 4.0 (derived table; credits
  in §3.9b). `tissue-index --out` writes the band, sensitivities and
  provenance as JSON wherever you point it.
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
| `patch 'X' is not a flat end cap (planarity …)` | A speed inlet needs a flat cap to define "inward"; give that patch a vector `X=vx,vy,vz` instead (§3.10a). |
| `the inflow picker needs exactly two flat end caps` | The picker only handles a tube with two ends; type `inlet` / `outlet` in the cfd panel. |
| `cardiac presets need the model's length unit` | The source declares no unit (STL/VTK/synthetic) — type ν and the speed in your model's own units. |
| `--inlet NAME=SPEED needs the mesh in memory` | CLI `cfd` only has a case directory: use `NAME=vx,vy,vz`, or the Workbench / `meshforge replay`. |
| `TotalSegmentator not found: set MESHFORGE_TOTALSEG …` | Install it in its own env (§3.6a) or point `MESHFORGE_TOTALSEG` at the executable. |
| `TotalSegmentator failed (exit code N). Last log lines: …` | The tail is TotalSegmentator's own output. On a Mac without a working GPU backend, try `device` = `cpu`. |
| `TotalSegmentator returned an empty aorta mask — is the aorta in the field of view? …` | The scan does not show the aorta (wrong series, cropped away), or it is a tight crop with no body context — TotalSegmentator needs the full-field-of-view CT. Seen on the Zenodo AAA crops (~80 mm boxes around the aneurysm). |
| `TotalSegmentator's aorta mask holds only … mL … implausibly small for an aorta` | Same cause: a few stray voxels, not an aorta. The step refuses to pass them on. |
| `REFUSED : … wall rays capped …` / `… inverted cell(s)` / `no mesh summary …` | `aaa-stress` will not score a wall the gate did not clear — re-mesh (`--curvature-cap 0.9`, `--sweep auto`, `--section-smoothing`) until the wall is 0 inverted / 0 capped / 0 floored, and keep the `.summary.json` next to the `.vtu`. |
| `REFUSED : the end zones (…) leave N% of the wall volume (< 10%)` | The crop is too short for one neck diameter at each clamped end. Re-crop with longer ends, or pass `--end-zone bending` (or a length in mm); the zones used are printed. |
| `… over the 268435456 voxel CT ceiling` | The CT header declares more than 512×512×1024 voxels; crop or downsample it first. |
| `… the anatomical frame (LPS/RAS) is unknown — refusing to guess` | The NRRD has no `space` field. Re-save it from a viewer that writes `space: left-posterior-superior`. |

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

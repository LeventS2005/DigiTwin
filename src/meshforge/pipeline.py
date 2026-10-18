"""GUI-agnostic service layer: MeshingSession.

Every GUI/CLI front-end drives ONLY this class. Each step returns a
StepResult carrying a machine summary, human-readable text, and neutral
render hints — fully testable without Qt. Paths are accepted as parameters
but files are read only inside the step that consumes them (trap #7).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Declarative knob specs: (type, default, help). The GUI generates its
# parameter widgets from these, so new knobs appear with no GUI changes.
#
# The ``help`` strings are USER-FACING (Workbench knob captions and CLI
# --help). Keep internal spec/milestone ids ("V38", "V47", D-numbers) OUT of
# them — they mean nothing to a user. Provenance belongs in the section
# comments below, in docstrings, or in docs/decisions/.
STEP_KNOBS: dict[str, dict[str, tuple[type, Any, str]]] = {
    "centerline": {
        "voxel_size": (float, None, "voxel size (default: bbox diagonal / 150)"),
        "smoothing": (float, None, "RMS smoothing in physical units (default: 1 voxel)"),
        "n_samples": (int, 200, "uniform arc-length samples"),
    },
    "sections": {
        "every": (int, 5, "take every Nth centerline station"),
        "rel_drop": (float, 0.2, "constriction detection threshold"),
    },
    "wall": {
        "thickness": (float, 0.5, "wall thickness (clear this to use outer_surface); 0.5 suits the synthetic tube — a mm-scale scan wants ~1.5"),
        "outer_surface": (str, "", "path to a measured outer surface (thickness must be cleared)"),
        "mode": (str, "sdf", "sdf (default, robust) | uniform | radius_scaled"),
        "curvature_cap": (float, 0.5, "fraction of 1/|κ| allowed on concave bends"),
        "floor_frac": (float, 0.2, "minimum thickness as a fraction of nominal"),
    },
    "volmesh": {
        "backend": (str, "swept", "swept | tet"),
        "thickness": (float, 0.5, "wall thickness; 0.5 suits the synthetic tube — a mm-scale scan wants ~1.5"),
        "n_theta": (int, 32, "azimuthal divisions"),
        "n_axial": (int, 60, "axial stations"),
        "n_layers": (int, 4, "radial layers (swept)"),
        "layer_ratio": (float, 1.0, "radial grading: outer/inner layer thickness"),
        "curvature_cap": (float, 0.5, "swept: fraction of the fold radius 1/|κ| a wall ray may reach on a concave bend (0.9 for an aneurysm sac)"),
        # D87: station layout of the swept wall (default unchanged).
        "sweep": (str, "centreline", "swept stations: centreline (rays square to the centreline) | planes (sections parallel to two flat parallel end caps — for a short, wide, eccentric segment such as a cropped sac) | auto (centreline unless its wall inverts or is capped/floored, then planes)"),
        "side": (str, "outward", "planes: grow the wall outward of the surface, or inward (the surface is the outer wall, e.g. a recovered sac)"),
        "max_tilt": (float, None, "planes: largest surface tilt to the station planes, degrees, before the wall is thinned (default 80)"),
        "section_smoothing": (float, 0.0, "planes: Gaussian smoothing of each section along its length (length units, 0 = exact) for a surface rougher than the wall thickness; how far the nodes move is reported"),
    },
    # V47: fill the blood volume (lumen) instead of the wall; the result
    # takes the session volmesh slot so deck/solve/cfd/save work unchanged.
    "lumen": {
        "method": (str, "auto", "auto (O-grid hexes, tet fallback on failure) | ogrid | tet"),
        "n_theta": (int, 32, "rays per station (ring resolution)"),
        "n_axial": (int, 40, "stations along the centerline"),
        "core_fraction": (float, 0.5, "O-grid core size fraction of centroid-to-rim"),
        "smooth": (int, 4, "guarded smoothing iterations, sj_min can never regress (0 = off)"),
        "mesh_size": (float, None, "tet-fallback target size (default: 1.5x median ring edge)"),
    },
    # V12 integration: the roadmap-v2 features, same declarative pattern.
    "import_cad": {
        "path": (str, "", "DXF/DWG drawing file"),
        "block": (str, "", "block name (empty: modelspace/auto)"),
        "mode": (str, "auto", "auto | chain | faces"),
    },
    "import_cad3d": {
        "path": (str, "", "STEP/IGES/BREP solid file"),
        "mesh_size": (float, None, "absolute surface mesh size"),
        "mesh_size_factor": (float, None, "scales the default (bbox diag / 50)"),
    },
    "import_image": {
        "path": (str, "", ".npy / .nii(.gz) volume or DICOM directory"),
        "level": (float, None, "iso level (REQUIRED — e.g. HU threshold)"),
        "spacing": (str, "", "X,Y,Z voxel spacing override (.npy inputs)"),
    },
    "import_cso": {
        "path": (str, "", "MeVisLab Binary .cso contour file"),
        "method": (str, "auto", "auto (default: loft, else voxel — whichever sweeps clean) | loft | voxel"),
        "n_ring": (int, 48, "resample count per ring (loft)"),
        "voxel_size": (float, None, "voxel size for the label volume (voxel)"),
        "cap_ends": (int, 1, "1 = cap the two end rings (watertight, loft)"),
        "on_open": (str, "skip", "open editor contour policy: skip | close | error"),
        "dicom": (str, "", "anchor to a DICOM series' grid: voxelise onto its lattice"),
    },
    # D83 (M1): AI segmentation — TotalSegmentator (own conda env, run as a
    # subprocess) outlines the aorta on a CT; the mask becomes the source surface.
    "segment": {
        "path": (str, "", "CT volume .nrrd / .nii(.gz) — the aorta is outlined by TotalSegmentator"),
        "device": (str, "mps", "mps (Apple GPU) | cpu"),
        "fast": (int, 0, "1 = TotalSegmentator's faster 3 mm model"),
        "smoothing": (float, 1.0, "Gaussian blur of the mask in voxels before the surface (0 = off)"),
        "timeout": (float, 1800.0, "TotalSegmentator wall-clock limit in seconds"),
        "lumen_out": (str, "", "also write the contrast blood lumen to this .vtp file (CT >= lumen_hu inside the AI outline — a threshold, not AI)"),
        "lumen_hu": (float, 150.0, "HU threshold for the blood lumen (calcification is bright too: detached pieces are dropped)"),
    },
    # D85 (M2): crop the aorta to its infrarenal aneurysm segment — two cut
    # planes square to the centreline, flat caps, landmark report.
    "crop": {
        "neck_extension_mm": (float, 25.0, "proximal cut this far above the neck start (the flow/clamp extension), held inside the neck"),
        "distal_margin_mm": (float, 10.0, "distal cut this far above the top of the bifurcation flare"),
        "distal_height_mm": (float, None, "manual distal cut, mm above the caudal end (set both heights or neither)"),
        "proximal_height_mm": (float, None, "manual proximal cut, mm above the caudal end"),
        "lumen": (str, "", "blood-lumen surface file to cut with the same two planes"),
        "lumen_out": (str, "", "write the cut lumen surface to this .vtp file"),
    },
    # D86 (M2): outer aneurysm sac (lumen + thrombus + wall) recovered from the
    # CT around a lumen-only mask, by axial rays to the first fat plane.
    "sac": {
        "path": (str, "", "CT volume .nrrd / .nii(.gz) in HU — the scan the lumen mask was drawn on"),
        "lumen_mask": (str, "", "lumen mask file on the same grid (.nii/.nii.gz/.nrrd), e.g. an AI aorta mask"),
        "lumen_hu": (float, 150.0, "contrast threshold (HU): mask voxels at or above it are the lumen"),
        "fat_hu": (float, -30.0, "fat / air below this HU marks the outer sac edge"),
        "max_thickness_mm": (float, 40.0, "longest thrombus + wall searched beyond the lumen (mm)"),
        "n_rays": (int, 72, "rays per axial slice"),
        "roi_mm": (float, 150.0, "height above the lumen's lower end to process (mm; 0 = whole mask)"),
        "min_resolved": (float, 0.5, "refuse when fewer rays than this fraction find a fat plane"),
        "smoothing": (float, 1.0, "Gaussian blur of the sac mask in voxels before the surface (0 = off)"),
    },
    # V5 structured blocks; V6 projection, V17 Winslow smoothing,
    # V20 grading, V26 O-grid.
    "blocks": {
        "target_size": (float, None, "max element edge (default: bbox diag / 60)"),
        "thickness": (float, 1.0, "extrusion thickness"),
        "n_layers": (int, 1, "extrusion layers"),
        "holes_like": (str, "", "punch holes at components with outer bbox WxH"),
        "project": (int, 0, "1 = project curved loops"),
        "ogrid": (int, 0, "1 = O-grid (butterfly) disk layout — one loop, no holes"),
        "core_fraction": (float, 0.5, "O-grid core size fraction of centroid-to-rim"),
        "smooth": (int, 0, "1 = elliptic (Winslow) smoothing pass"),
        "grade_ratio": (float, 1.0, "transition grading: max/min spacing per interval"),
    },
    "repair": {
        "merge_tol": (float, None, "vertex merge tolerance (default 1e-9 x diag)"),
        "max_hole_edges": (int, 16, "fill holes up to this many edges"),
    },
    "blayers": {
        "first": (float, None, "first layer thickness (REQUIRED, V22)"),
        "n_layers": (int, 5, "number of wedge layers"),
        "growth": (float, 1.2, "geometric growth ratio (>= 1)"),
        "normal_smoothing": (int, 10, "Laplacian passes on the growth normals"),
    },
    # V7 recipe sweeps.
    "recipe": {
        "path": (str, "", "recipe .yaml to run (parameter sweeps)"),
    },
    "replay": {
        "workfile": (str, "", "optional .mfwork (empty: this session's journal)"),
        "override": (str, "", "optional STEP.KNOB=VALUE override"),
    },
    "screenshot": {
        "path": (str, "", "PNG path (default: outputs/session.png)"),
    },
    # V13: in-app solve — CalculiX on a session-built (or external) deck,
    # displacements/stresses read back as point data.
    "solve": {
        "deck": (str, "", "existing .inp to solve (empty: build one from the session volmesh)"),
        "analysis": (str, "static", "static | frequency (modal) | heat (steady/transient) | buckle"),
        "preloaded": (int, 0, "1 = *STATIC preload then *FREQUENCY, PERTURBATION (frequency, V32)"),
        "E": (float, 1000.0, "Young's modulus (static/frequency decks)"),
        "nu": (float, 0.3, "Poisson's ratio"),
        "density": (float, 0.0, "mass density (REQUIRED for frequency)"),
        "n_modes": (int, 10, "eigenmodes to extract (frequency)"),
        "conductivity": (float, 0.0, "thermal conductivity k (REQUIRED for heat)"),
        "conductivity_table": (str, "", "temperature-dependent k as k@T,k@T,... e.g. 2@0,12@1000 (heat, V41; overrides conductivity)"),
        "temp": (str, "", "fixed temperatures NAME=VALUE,... e.g. inner=100,outer=20 (heat)"),
        "flux": (str, "", "surface heat flux NAME=VALUE, positive into the body (heat)"),
        "film": (str, "", "film convection NAME=SINK,H e.g. outer=20,0.5 (heat, V31)"),
        "radiate": (str, "", "environmental radiation NAME=SINK,EMISS e.g. outer=25,0.9 (heat, V41; needs physical constants)"),
        "stefan_boltzmann": (float, None, "Stefan-Boltzmann constant (REQUIRED with radiate)"),
        "absolute_zero": (float, None, "absolute-zero offset in the model temperature unit: 0 (Kelvin) / -273.15 (Celsius) (REQUIRED with radiate)"),
        "specific_heat": (float, 0.0, "specific heat c_p (REQUIRED for transient heat)"),
        "transient": (int, 0, "1 = transient *HEAT TRANSFER, DIRECT (needs dt/period/t0)"),
        "dt": (float, 0.0, "transient time increment (accuracy dial: backward Euler is first-order)"),
        "period": (float, 0.0, "transient total time"),
        "t0": (float, None, "initial temperature (REQUIRED for transient — never guessed)"),
        "pressure": (str, "", "follower pressure load NAME=VALUE, e.g. inner=1.0 (static)"),
        "cload": (str, "", "dead concentrated load NAME,dof,value on a boundary group, e.g. back,3,-0.04 (static; dead loads suit riks buckling)"),
        "expansion": (float, 0.0, "thermal-expansion coefficient alpha -> *EXPANSION (static thermal stress; needs temperature or temperature_from + t_ref, V45)"),
        "temperature": (float, None, "uniform *TEMPERATURE soak on all nodes (static; needs expansion + t_ref)"),
        "temperature_from": (str, "", "solved heat .frd whose NDTEMP field becomes the *TEMPERATURE load — chain analysis='heat' into thermal stress (static; needs expansion + t_ref)"),
        "temperature_time": (float, None, "snapshot time to take from the temperature_from frd's transient history (default = last increment; must match one of the frd's own recorded times exactly — no interpolation)"),
        "t_ref": (float, None, "stress-free reference temperature -> *INITIAL CONDITIONS (REQUIRED with a thermal load — never guessed: a wrong datum is silently wrong stress)"),
        "fix": (str, "", "DOF fixes NAME:first[-last],... e.g. inlet:3,outlet:3 (bare NAME clamps 1-3; NALL valid for frequency)"),
        "pin_lateral": (int, 0, "1 = tangential single-node pins on the inlet (removes rigid modes)"),
        "nlgeom": (int, 0, "1 = large-deformation static step"),
        "formulation": (str, "", "element formulation reduced|full|hybrid (empty = ccx default full; reduced C3D8R avoids shear-locking in bending/riks)"),
        "riks": (int, 0, "1 = *STATIC, RIKS arc-length continuation for post-buckling (static, needs nlgeom=1 + reduced formulation; read load from RF, V42)"),
        "riks_max_lpf": (float, 0.0, "RIKS load-proportionality-factor cap = data-line field 2 (0 = default 1.0)"),
        "riks_dl_init": (float, 0.0, "RIKS initial arc-length increment (0 = default 0.05)"),
        "riks_dl_min": (float, 0.0, "RIKS minimum arc-length increment (0 = default 1e-5)"),
        "riks_dl_max": (float, 0.0, "RIKS maximum arc-length increment (0 = default 0.5)"),
        "ccx": (str, "", "CalculiX executable (empty: MESHFORGE_CCX / PATH / conda env)"),
        "timeout": (float, 600.0, "solver wall-clock limit in seconds"),
    },
    # D88 (M3): AAA wall stress under blood pressure — stiff-linear solve on
    # the loaded geometry, peak wall stress, wall tension, a pressure what-if.
    "aaa_stress": {
        "systolic_mmhg": (float, 140.0, "systolic blood pressure (mmHg) on the lumen side of the wall"),
        "whatif_mmhg": (float, 120.0, "what-if blood pressure (mmHg), solved separately"),
        "E": (float, 100.0, "wall Young's modulus in MPa (stiff-linear on the loaded geometry: the stress barely depends on it)"),
        "nu": (float, 0.49, "wall Poisson's ratio (near-incompressible tissue)"),
        "end_zone": (str, "neck", "left out of the metrics next to each clamped end: neck (one neck diameter from the crop report, else each end's own diameter) | bending (3 shell bending lengths) | a length in mm"),
        "crop_report": (str, "", "crop report JSON file with the neck diameter (empty: this session's crop step, if any)"),
        "ccx": (str, "", "CalculiX executable (empty: MESHFORGE_CCX / PATH / conda env)"),
        "timeout": (float, 600.0, "solver wall-clock limit per solve in seconds"),
    },
    # V30: carve a new boundary patch out of an existing group by a
    # face-centroid coordinate (e.g. the moving lid of a cavity).
    "split_patch": {
        "group": (str, "outer_rim", "boundary group to split"),
        "axis": (str, "y", "centroid coordinate to test: x | y | z"),
        "threshold": (float, None, "split threshold (REQUIRED)"),
        "name": (str, "", "new group name (REQUIRED)"),
        "side": (str, "above", "which faces move: above (>= threshold) | below"),
    },
    # V23: in-app CFD solve — OpenFOAM on a session-built (or external)
    # case, U/p read back via pyvista.
    "cfd": {
        "case": (str, "", "existing complete case dir to run (empty: build one from the session volmesh)"),
        "solver": (str, "simpleFoam", "simpleFoam (steady) | icoFoam (transient)"),
        "nu": (float, 1e-3, "kinematic viscosity"),
        "inlet": (str, "", "velocity inlet: NAME=SPEED flows in along the patch's inward normal (e.g. inlet=150), or NAME=vx,vy,vz"),
        "outlet": (str, "", "pressure outlet NAME=value, e.g. back=0"),
        "end_time": (float, 2000.0, "endTime (steady: max iterations)"),
        "delta_t": (float, 0.0, "time step (icoFoam only; 0 invalid there)"),
        "write_interval": (float, 0.0, "write interval (0: final result only)"),
        "timeout": (float, 600.0, "solver wall-clock limit in seconds"),
        "turbulence": (str, "laminar", "laminar | kOmegaSST (steady RANS, V27)"),
        "intensity": (float, 0.05, "inlet turbulence intensity I (kOmegaSST)"),
        "turb_length": (float, 0.0, "inlet mixing length, e.g. 0.07*D (REQUIRED for kOmegaSST)"),
        "series": (int, 0, "1 = read ALL written time steps back as frames (needs write_interval < end_time)"),
        "moving_wall": (str, "", "tangentially moving wall NAME=vx,vy,vz, e.g. lid=1,0,0"),
        "empty": (str, "", "comma-separated 2-D 'empty' patches, e.g. front,back"),
        "closed_cavity": (int, 0, "1 = sealed domain: pressure level pinned by pRefCell/pRefValue"),
        "p_ref_cell": (int, 0, "reference cell index (closed_cavity)"),
        "p_ref_value": (float, 0.0, "reference kinematic pressure (closed_cavity)"),
    },
}

# Panel/display order for front-ends (Workbench builds its left pane from this).
# STEP_KNOBS is keyed by name everywhere else (recipes.py, web/app.py), so the
# order a user sees is stated ONCE, here, instead of riding on dict insertion
# order. The import_* steps come first because a real run starts from a file:
# they produce the surface every processing step below them consumes.
STEP_ORDER: tuple[str, ...] = (
    "import_cad",
    "import_cad3d",
    "import_image",
    "import_cso",
    "segment",
    "sac",
    "crop",
    "centerline",
    "sections",
    "wall",
    "volmesh",
    "lumen",
    "blocks",
    "repair",
    "blayers",
    "recipe",
    "replay",
    "screenshot",
    "solve",
    "aaa_stress",
    "split_patch",
    "cfd",
)

# Fail loudly if a step is ever added to STEP_KNOBS without a display slot,
# rather than letting it silently vanish from (or duplicate in) the GUI.
_missing = set(STEP_KNOBS) - set(STEP_ORDER)
_extra = set(STEP_ORDER) - set(STEP_KNOBS)
if _missing or _extra:  # pragma: no cover - import-time contract check
    raise RuntimeError(
        f"STEP_ORDER is out of sync with STEP_KNOBS: missing {sorted(_missing)}, "
        f"unknown {sorted(_extra)}"
    )
if len(STEP_ORDER) != len(set(STEP_ORDER)):  # pragma: no cover
    raise RuntimeError("STEP_ORDER contains a duplicate step")


def sac_text(summ: dict[str, Any]) -> str:
    """Human-readable sac summary (step text and CLI share it)."""
    md, th = summ["max_diameter"], summ["thickness_mm"]
    counts = ", ".join(f"{k} {v}" for k, v in summ["ray_status_counts"].items())
    rps = summ["resolved_fraction_per_slice"]
    text = (
        f"sac: {summ['n_slices']} axial slices x {summ['n_rays']} rays, h "
        f"{summ['h_range_mm'][0]:g}..{summ['h_range_mm'][1]:g} mm above the lumen's "
        f"lower end\n"
        f"  resolved (fat plane found): {summ['resolved_fraction']:.1%} of rays; per "
        f"slice min {rps['min']:.0%} / median {rps['median']:.0%}; {counts}\n"
        f"  sac {summ['sac_volume_ml']:.1f} mL, lumen {summ['lumen_volume_ml']:.1f} mL; "
        f"max diameter {md['max_caliper']['value_mm']:.1f} mm (caliper, h "
        f"{md['max_caliper']['h_mm']:g}), short axis {md['min_caliper']['value_mm']:.1f} "
        f"mm, area-equivalent {md['area_equivalent']['value_mm']:.1f} mm\n"
        f"  thrombus + wall thickness: median {th['all_rays']['median']:.1f}, p90 "
        f"{th['all_rays']['p90']:.1f}, max {th['all_rays']['max']:.1f} mm\n"
    )
    for note in summ["notes"]:
        text += f"  note: {note}\n"
    return text


def _clean_path(raw: str | Path) -> str:
    r"""Normalise one user-entered path (``~``, surrounding quotes, ``\ ``
    drag-into-terminal escapes, stray whitespace) — see ``io/paths.py``.

    Applied at EVERY path entry point in this service layer, so the CLI, the
    Workbench and the desktop shell all accept the same path habits and a
    missing file reports the loader's own friendly message instead of a raw
    ``[Errno 2]``. Lazy import: ``meshforge.io`` pulls pyvista, and this
    module must stay cheap to import (rule 5).
    """
    from meshforge.io.paths import normalize_path_str

    return normalize_path_str(raw)


def _parse_inlet_spec(inlet: str):
    """``NAME=vx,vy,vz`` -> (name, (vx, vy, vz))."""
    from meshforge.meshing import valid_group_name

    name, sep, raw = inlet.partition("=")
    parts = raw.split(",")
    if not sep or not name.strip() or len(parts) != 3:
        raise ValueError(f"inlet expects NAME=vx,vy,vz, got {inlet!r}")
    try:
        return valid_group_name(name.strip()), tuple(float(v) for v in parts)
    except ValueError as exc:
        if "invalid boundary-group name" in str(exc):
            raise
        raise ValueError(f"inlet velocity {raw!r} is not three numbers") from exc


def _parse_inlet_knob(inlet: str):
    """The cfd ``inlet`` knob: ``NAME=SPEED`` -> (name, speed) or
    ``NAME=vx,vy,vz`` -> (name, (vx, vy, vz)). The scalar is resolved
    against the mesh later (``solve/inflow.py``); only its syntax and sign
    are checked here, before any mesh is required."""
    from meshforge.meshing import valid_group_name
    from meshforge.solve.inflow import check_inlet_speed

    name, sep, raw = inlet.partition("=")
    parts = raw.split(",")
    if not sep or not name.strip() or len(parts) not in (1, 3):
        raise ValueError(
            f"inlet expects NAME=SPEED or NAME=vx,vy,vz, got {inlet!r}"
        )
    if len(parts) == 3:
        return _parse_inlet_spec(inlet)
    name = valid_group_name(name.strip())
    try:
        speed = float(parts[0])
    except ValueError as exc:
        raise ValueError(f"inlet speed {raw!r} is not a number") from exc
    return name, check_inlet_speed(speed, name)


def _parse_outlet_spec(outlet: str):
    """``NAME=value`` -> (name, p)."""
    from meshforge.meshing import valid_group_name

    name, sep, raw = outlet.partition("=")
    if not sep or not name.strip():
        raise ValueError(f"outlet expects NAME=VALUE, got {outlet!r}")
    valid_group_name(name.strip())
    try:
        return name.strip(), float(raw)
    except ValueError as exc:
        raise ValueError(f"outlet pressure {raw!r} is not a number") from exc


def _parse_fix_spec(fix: str) -> list:
    """``NAME:first[-last],...`` (bare ``NAME`` clamps DOFs 1-3) -> BCs."""
    from meshforge.export.deck import BoundaryCondition

    bcs = []
    for token in filter(None, (t.strip() for t in fix.split(","))):
        name, sep, dofs = token.partition(":")
        if not name:
            raise ValueError(f"fix entry {token!r} is missing a group name")
        if not sep:
            bcs.append(BoundaryCondition(name, 1, 3))
            continue
        first, dash, last = dofs.partition("-")
        try:
            bcs.append(
                BoundaryCondition(
                    name, int(first), int(last) if dash else None
                )
            )
        except ValueError as exc:
            raise ValueError(
                f"fix entry {token!r}: expected NAME:first[-last] with integer "
                f"DOFs 1..3"
            ) from exc
    return bcs


def _parse_temp_spec(temp: str) -> list:
    """``NAME=VALUE,NAME=VALUE`` -> [(name, value), ...] (heat solves)."""
    pairs = []
    for token in filter(None, (t.strip() for t in temp.split(","))):
        name, sep, raw = token.partition("=")
        if not sep or not name.strip():
            raise ValueError(f"temp entry {token!r}: expected NAME=VALUE")
        try:
            pairs.append((name.strip(), float(raw)))
        except ValueError as exc:
            raise ValueError(f"temp value {raw!r} is not a number") from exc
    return pairs


def _parse_conductivity_table(spec: str) -> list:
    """``k@T,k@T,...`` -> [(k, T), ...] for a temperature-dependent
    *CONDUCTIVITY (V41). Row validation (k>0, ascending T, >=2 rows) lives in
    Material.thermal — this only parses the numbers."""
    rows = []
    for token in filter(None, (t.strip() for t in spec.split(","))):
        k_str, sep, t_str = token.partition("@")
        if not sep:
            raise ValueError(
                f"conductivity_table entry {token!r}: expected k@T"
            )
        try:
            rows.append((float(k_str), float(t_str)))
        except ValueError as exc:
            raise ValueError(
                f"conductivity_table entry {token!r}: k@T are not two numbers"
            ) from exc
    if not rows:
        raise ValueError("conductivity_table is empty")
    return rows


def _parse_pressure_spec(pressure: str):
    """``NAME=VALUE`` -> Load.pressure (single load; use an external deck
    for anything richer)."""
    from meshforge.export.deck import Load

    name, sep, raw = pressure.partition("=")
    if not sep or not name.strip():
        raise ValueError(f"pressure expects NAME=VALUE, got {pressure!r}")
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"pressure value {raw!r} is not a number") from exc
    return Load.pressure(name.strip(), value)


def _parse_cload_spec(cload: str):
    """``NAME,dof,value`` -> Load.concentrated on a boundary NSET (a dead,
    fixed-direction force, unlike the follower *DSLOAD ``pressure`` — dead
    loads are what a RIKS buckling column needs). ccx applies the magnitude
    to each node in the set; on a RIKS step the true load is read from the
    reaction, so the reference magnitude only sets the scale."""
    from meshforge.export.deck import Load

    parts = cload.split(",")
    if len(parts) != 3 or not parts[0].strip():
        raise ValueError(f"cload expects NAME,dof,value, got {cload!r}")
    try:
        return Load.concentrated(parts[0].strip(), int(parts[1]), float(parts[2]))
    except ValueError as exc:
        raise ValueError(f"cload {cload!r} is invalid: {exc}") from exc


def _parse_flux_spec(flux: str):
    """``NAME=VALUE`` -> Load.surface_flux (heat solves)."""
    from meshforge.export.deck import Load

    name, sep, raw = flux.partition("=")
    if not sep or not name.strip():
        raise ValueError(f"flux expects NAME=VALUE, got {flux!r}")
    return Load.surface_flux(name.strip(), float(raw))


def _parse_film_spec(film: str):
    """``NAME=SINK,H`` -> Load.film (V31 heat solves)."""
    from meshforge.export.deck import Load

    name, sep, raw = film.partition("=")
    parts = raw.split(",")
    if not sep or not name.strip() or len(parts) != 2:
        raise ValueError(
            f"film expects NAME=SINK,H, got {film!r}"
        )
    try:
        sink, h = float(parts[0]), float(parts[1])
    except ValueError as exc:
        raise ValueError(
            f"film sink/H {raw!r} are not two numbers"
        ) from exc
    return Load.film(name.strip(), sink, h)


def _parse_radiate_spec(radiate: str):
    """``NAME=SINK,EMISS`` -> (name, sink, emissivity) (V41 heat solves;
    pairing the load with *PHYSICAL CONSTANTS lives in _heat_solve_config)."""
    name, sep, raw = radiate.partition("=")
    parts = raw.split(",")
    if not sep or not name.strip() or len(parts) != 2:
        raise ValueError(
            f"radiate expects NAME=SINK,EMISS, got {radiate!r}"
        )
    try:
        sink, emiss = float(parts[0]), float(parts[1])
    except ValueError as exc:
        raise ValueError(
            f"radiate sink/emissivity {raw!r} are not two numbers"
        ) from exc
    return name.strip(), sink, emiss


# ---- compute_solve sub-configs (V43, D65) ----------------------------------
# compute_solve's knob surface spans five analysis kinds; each helper below
# owns one concern and takes compute_solve's recorded ``params`` dict, so the
# knob contract (STEP_KNOBS/CLI/web/replay) stays in exactly one place.


def _validate_solve_knobs(params: dict, riks_knobs_set: bool) -> None:
    """Cross-knob consistency, before any deck work (fail loud, rule 6)."""
    analysis = params["analysis"]
    if analysis not in ("static", "frequency", "heat", "buckle"):
        raise ValueError(
            f"analysis must be static|frequency|heat|buckle, got {analysis!r}"
        )
    if params["preloaded"] and analysis != "frequency":
        raise ValueError(
            "preloaded describes frequency solves — set "
            "analysis='frequency'"
        )
    if (params["riks"] or riks_knobs_set) and analysis != "static":
        raise ValueError(
            "riks (arc-length continuation) is a static-analysis knob — "
            "set analysis='static'"
        )
    if riks_knobs_set and not params["riks"]:
        raise ValueError(
            "riks_max_lpf/riks_dl_init/riks_dl_min/riks_dl_max apply only "
            "to a riks solve — set riks=1 or drop them"
        )
    if params["riks"] and not params["nlgeom"]:
        raise ValueError(
            "riks needs nlgeom=1 — post-buckling continuation is "
            "geometrically nonlinear"
        )
    if analysis != "static" and (
        params["expansion"] or params["temperature"] is not None
        or params["temperature_from"] or params["t_ref"] is not None
        or params["temperature_time"] is not None
    ):
        raise ValueError(
            "expansion/temperature/temperature_from/temperature_time/t_ref "
            "describe a static "
            "solve CONSUMING a temperature field (thermal stress, V45) — a "
            "heat solve produces one; set analysis='static'"
        )


def _refuse_external_deck_build_knobs(params: dict, riks_knobs_set: bool) -> None:
    """An external .inp is solved as-is — every session build knob must stay
    at its default (nlgeom included, V43: it used to pass silently)."""
    if (
        params["pressure"] or params["cload"] or params["fix"]
        or params["pin_lateral"] or params["temp"] or params["flux"]
        or params["film"] or params["radiate"] or params["conductivity_table"]
        or params["stefan_boltzmann"] is not None
        or params["absolute_zero"] is not None
        or params["specific_heat"] or params["transient"] or params["dt"]
        or params["period"] or params["t0"] is not None or params["preloaded"]
        or params["analysis"] != "static"
        or params["nlgeom"] or params["riks"] or riks_knobs_set
        or params["formulation"]
        or params["expansion"] or params["temperature"] is not None
        or params["temperature_from"] or params["t_ref"] is not None
        or params["temperature_time"] is not None
    ):
        raise ValueError(
            "an external deck already carries its step/BCs/loads and "
            "element formulation — the analysis/preloaded/pressure/fix/"
            "pin_lateral/temp/flux/film/radiate/conductivity_table/"
            "stefan_boltzmann/absolute_zero/specific_heat/transient/dt/"
            "period/t0/nlgeom/formulation/riks/expansion/temperature/"
            "temperature_from/temperature_time/t_ref knobs apply only to "
            "session-built decks"
        )


def _heat_solve_config(params: dict):
    """Material/Step/BCs/loads (+ optional *PHYSICAL CONSTANTS) for a
    session-built heat deck (V24/V31/V41)."""
    from meshforge.export.deck import (
        BoundaryCondition,
        Load,
        Material,
        PhysicalConstants,
        Step,
    )

    density = params["density"]
    specific_heat = params["specific_heat"]
    if params["conductivity_table"]:
        if params["conductivity"]:
            raise ValueError(
                "give either conductivity or conductivity_table, "
                "not both"
            )
        material = Material.thermal(
            _parse_conductivity_table(params["conductivity_table"]),
            density=density or None,
            specific_heat=specific_heat or None,
        )
    else:
        if not params["conductivity"] > 0.0:
            raise ValueError(
                "a heat solve needs a positive conductivity or "
                "conductivity_table knob"
            )
        material = Material.thermal(
            params["conductivity"],
            density=density or None,
            specific_heat=specific_heat or None,
        )
    # 0-defaults pass through as None so Step/DeckSpec raise their own loud
    # refusals (dt without transient etc.).
    step = Step(
        kind="heat",
        transient=bool(params["transient"]),
        dt=params["dt"] or None,
        period=params["period"] or None,
        initial_temperature=params["t0"],
    )
    bcs = [
        BoundaryCondition(name, 11, 11, value)
        for name, value in _parse_temp_spec(params["temp"])
    ]
    loads = []
    if params["flux"]:
        loads.append(_parse_flux_spec(params["flux"]))
    if params["film"]:
        loads.append(_parse_film_spec(params["film"]))
    physical_constants = None
    if params["radiate"]:
        name, sink, emiss = _parse_radiate_spec(params["radiate"])
        if params["stefan_boltzmann"] is None or params["absolute_zero"] is None:
            raise ValueError(
                "radiate needs stefan_boltzmann and absolute_zero "
                "(the *PHYSICAL CONSTANTS for the σT⁴ law)"
            )
        loads.append(Load.radiate(name, sink, emiss))
        physical_constants = PhysicalConstants(
            stefan_boltzmann=params["stefan_boltzmann"],
            absolute_zero=params["absolute_zero"],
        )
    elif params["stefan_boltzmann"] is not None or params["absolute_zero"] is not None:
        raise ValueError(
            "stefan_boltzmann/absolute_zero only feed radiate — "
            "add a radiate knob or drop them"
        )
    return material, step, bcs, loads, physical_constants


def _mech_solve_config(vol, params: dict):
    """Material/Step/BCs/loads (+ V45 thermal-load DeckSpec fields) for a
    session-built static/frequency/buckle deck (V13/V32/V42/V45); refuses
    heat knobs loudly."""
    from meshforge.export.deck import Material, Step, lateral_pins

    if (
        params["temp"] or params["flux"] or params["film"] or params["radiate"]
        or params["conductivity_table"]
        or params["stefan_boltzmann"] is not None
        or params["absolute_zero"] is not None
        or params["specific_heat"] or params["transient"] or params["dt"]
        or params["period"] or params["t0"] is not None
    ):
        raise ValueError(
            "temp/flux/film/radiate/conductivity_table/"
            "stefan_boltzmann/absolute_zero/specific_heat/transient/"
            "dt/period/t0 knobs describe heat solves — set "
            "analysis='heat'"
        )
    material = Material.elastic(
        params["E"], params["nu"], density=params["density"] or None,
        expansion=params["expansion"] or None,
    )
    thermal = _thermal_load_config(vol, params)
    if params["analysis"] == "frequency":
        step = Step(
            kind="frequency", n_modes=params["n_modes"],
            preloaded=bool(params["preloaded"]),
        )
    elif params["analysis"] == "buckle":
        step = Step(kind="buckle", n_modes=params["n_modes"])
    else:
        step = Step(
            nlgeom=bool(params["nlgeom"]),
            riks=bool(params["riks"]),
            riks_max_lpf=params["riks_max_lpf"] or None,
            riks_dl_init=params["riks_dl_init"] or None,
            riks_dl_min=params["riks_dl_min"] or None,
            riks_dl_max=params["riks_dl_max"] or None,
        )
    bcs = _parse_fix_spec(params["fix"])
    if params["pin_lateral"]:
        bcs.extend(lateral_pins(vol))
    loads = [_parse_pressure_spec(params["pressure"])] if params["pressure"] else []
    if params["cload"]:
        loads.append(_parse_cload_spec(params["cload"]))
    return material, step, bcs, loads, thermal


def _thermal_load_config(vol, params: dict) -> dict:
    """V45 thermal-stress knobs -> the DeckSpec *TEMPERATURE fields.

    Cross-knob refusals live here (the radiate/stefan_boltzmann idiom); the
    trap-level refusals (missing *EXPANSION => silently zero stress, missing
    reference => silently wrong stress) are DeckSpec.validate()'s.
    """
    uniform = params["temperature"]
    frd_path = params["temperature_from"]
    if uniform is not None and frd_path:
        raise ValueError(
            "give either temperature (uniform) or temperature_from "
            "(a heat solve's .frd), not both"
        )
    if params["temperature_time"] is not None and not frd_path:
        raise ValueError(
            "temperature_time selects a snapshot from the temperature_from "
            "frd's transient history — add temperature_from, or drop it"
        )
    has_source = uniform is not None or bool(frd_path)
    if not has_source:
        if params["expansion"]:
            raise ValueError(
                "expansion is inert without a temperature source — add a "
                "temperature or temperature_from knob, or drop it"
            )
        if params["t_ref"] is not None:
            raise ValueError(
                "t_ref only feeds a thermal load — add a temperature or "
                "temperature_from knob, or drop it"
            )
        return {}
    if not params["expansion"]:
        raise ValueError(
            "a temperature/temperature_from thermal load needs the "
            "expansion knob (alpha) — without *EXPANSION ccx runs clean "
            "and the thermal stress is silently ZERO"
        )
    if params["t_ref"] is None:
        raise ValueError(
            "a thermal load needs t_ref (the stress-free reference "
            "temperature) — it is never guessed: ccx's expansion "
            "reference is the initial temperature ONLY and a wrong datum "
            "is silently wrong stress"
        )
    nodal = None
    if frd_path:
        import numpy as np

        from meshforge.solve import nodal_temperatures, read_frd

        path = Path(frd_path)
        if not path.is_file():
            raise ValueError(
                f"temperature_from frd not found: {frd_path}"
            )
        frd = read_frd(path)
        n = vol.grid.n_points
        if len(frd.node_ids) != n or not np.array_equal(
            frd.node_ids, np.arange(1, n + 1)
        ):
            raise ValueError(
                f"temperature_from frd carries {len(frd.node_ids)} node(s) "
                f"that are not this volmesh's 1..{n} — the temperature "
                f"field belongs to a different mesh"
            )
        pts = np.asarray(vol.grid.points)
        scale = max(1.0, float(np.abs(pts).max()))
        if not np.allclose(pts, frd.points, rtol=0.0, atol=2e-5 * scale):
            raise ValueError(
                "temperature_from frd node coordinates do not match the "
                "session volmesh — the temperature field belongs to a "
                "different mesh"
            )
        nodal = nodal_temperatures(frd, time=params["temperature_time"])
    return {
        "uniform_temperature": uniform,
        "nodal_temperatures": nodal,
        "reference_temperature": params["t_ref"],
    }


@dataclass
class StepResult:
    step: str
    summary: dict[str, Any]
    text: str
    render_hints: dict[str, Any] = field(default_factory=dict)


def crop_text(rep: dict[str, Any]) -> str:
    """Human-readable lines of a crop report (``geometry.crop.CropResult.report``)
    — shared by the crop step and the CLI."""
    lines = []
    lm = rep.get("landmarks")
    if lm:
        lines.append(
            f"landmarks   : sac h {lm['sac_height_mm']:.1f} mm, d_equiv "
            f"{lm['sac_d_equiv_mm']:.1f} mm (max diameter {lm['sac_max_diameter_mm']:.1f}); "
            f"neck from h {lm['neck_start_height_mm']:.1f} mm, d {lm['neck_d_equiv_mm']:.1f} "
            f"mm, length {'>= ' if lm['neck_open_ended'] else ''}{lm['neck_length_mm']:.1f} mm; "
            f"sac/neck {lm['sac_neck_ratio']:.2f}; flare top h {lm['flare_top_height_mm']:.1f} mm"
        )
    else:
        lines.append("landmarks   : not found (manual heights or planes)")
    for name in ("distal", "proximal"):
        pl, cap = rep["planes"][name], rep["caps"][name]
        lines.append(
            f"{name + ' cap':<12}: h {pl['height_mm']:.1f} mm, d_equiv {cap['d_equiv_mm']:.1f} mm, "
            f"{cap['rim_vertices']} rim vertices, planar to {cap['planarity_mm']:.1e} mm, rim "
            f"crease >= {cap['rim_crease_min_deg']:.1f} deg, normal "
            f"{[round(v, 4) for v in pl['normal_out']]}"
        )
    srf = rep["surface"]
    lines.append(
        f"cropped     : {srf['n_points']} pts, {srf['n_triangles']} tris, watertight "
        f"{srf['watertight']}, {srf['volume_ml']:.2f} mL"
    )
    if rep.get("lumen"):
        lum = rep["lumen"]
        lines.append(
            f"lumen       : {lum['n_triangles']} tris, watertight {lum['watertight']}, "
            f"{lum['volume_ml']:.2f} mL, same two planes"
        )
    lines += [f"note        : {n}" for n in rep.get("notes", [])]
    return "\n".join(lines) + "\n"


def auto_cso_surface(data, *, n_ring: int, voxel_size, cap_ends: bool):
    """V40: pick the first CSO surface + centreline whose swept volmesh has
    ``inverted == 0`` — loft + skeleton centreline, then loft + the interior
    contour centreline (rescues tortuous arches whose skeleton centreline exits
    the lumen), then the voxel fallback.

    For the two loft strategies the validated centreline is **embedded in the
    returned surface** (``geometry.embed_centerline``) so the later ``volmesh``
    step reuses it even across a save/load or a separate CLI invocation; the voxel
    fallback embeds nothing (a fresh skeleton centreline on the voxel surface is
    what sweeps clean).  Returns ``(surface, provenance, centerline_or_None,
    strategy)``.  The trial sweep uses fixed validation params; the user's own
    ``volmesh`` params are applied later (documented trade-off).

    Lives in the pipeline (not ``io/cso.py``) because it needs ``meshing``; that
    keeps ``io`` free of a ``meshing`` import (rule 4, no cycle)."""
    from meshforge.geometry import compute_centerline, embed_centerline
    from meshforge.io.cso import contour_centerline, cso_to_surface
    from meshforge.meshing import get_backend

    def sweeps_clean(surface, cline) -> bool:
        try:
            wm = get_backend("swept").mesh_wall(
                surface, 1.0, centerline=cline, n_theta=24, n_axial=40, n_layers=3
            )
        except ValueError:
            return False  # loud ray-miss / degenerate lattice on this centreline
        return int(wm.summary()["quality"]["n_inverted"]) == 0

    loft, lprov = cso_to_surface(data, method="loft", n_ring=n_ring, cap_ends=cap_ends)
    if lprov["watertight"]:
        skel = compute_centerline(loft)
        if sweeps_clean(loft, skel):
            embed_centerline(loft, skel)
            return loft, lprov, skel, "loft + skeleton centreline"
        cline = contour_centerline(data)
        if sweeps_clean(loft, cline):
            embed_centerline(loft, cline)
            return (
                loft,
                lprov,
                cline,
                "loft + contour centreline (skeleton centreline exited the lumen)",
            )
    voxel, vprov = cso_to_surface(data, method="voxel", voxel_size=voxel_size)
    return voxel, vprov, None, "voxel fallback (loft did not sweep clean)"


class MeshingSession:
    """Holds pipeline state: surface → centerline → sections → wall → volmesh."""

    def __init__(self) -> None:
        self.input_path: Path | None = None
        self.input_sha256: str | None = None
        self.surface = None
        self.truth = None  # AnalyticTruth when the surface is synthetic
        self.centerline = None
        self.sections = None
        self.constrictions = None
        self.wall = None
        self.volmesh = None
        self.solve = None  # SolveResult from compute_solve (V13)
        self.cfd = None  # CfdResult from compute_cfd (V23)
        self.aaa_stress = None  # AaaStressResult from compute_aaa_stress (D88)
        self.profile = None  # Profile2D from compute_import_cad (V12)
        # Length unit of the current source, or None when it declares none
        # (STL/VTK/synthetic). Set by each source step; CSO and DICOM are
        # millimetres by definition. Gates the cardiac CFD presets (D80).
        self.length_unit: str | None = None
        # V10 journal: ordered records of every step call — step name,
        # JSON-safe params (incl. paths + input checksums), result summary.
        self.journal: list[dict[str, Any]] = []

    def _record(
        self, step: str, params: dict[str, Any], result: "StepResult", **extra: Any
    ) -> None:
        self.journal.append(
            {"step": step, "params": params, "summary": result.summary, **extra}
        )

    # -- sources ---------------------------------------------------------
    def load(self, path: str | Path) -> StepResult:
        from meshforge.io import file_sha256, load_surface, normalize_path
        from meshforge.quality import inspect_surface

        path = normalize_path(path)
        # load_surface FIRST: it owns the friendly "surface file not found" and
        # the wrong-front-door suffix routing. Hashing first would have opened
        # the file and raised a bare [Errno 2] before either guard could speak.
        surface = load_surface(path)
        self.input_sha256 = file_sha256(path)
        self.surface = surface
        self.input_path = path
        self.truth = None
        self._invalidate_downstream()
        self.length_unit = None  # STL/VTK/... carry no unit
        report = inspect_surface(self.surface, path=str(path), sha256=self.input_sha256)
        result = StepResult(
            step="load",
            summary=report.to_dict(),
            text=report.to_text(),
            render_hints={"mesh": self.surface, "kind": "surface", "label": path.name},
        )
        self._record(
            "load", {"path": str(path)}, result, input_sha256=self.input_sha256
        )
        return result

    def generate_synth(self, shape: str, **params: Any) -> StepResult:
        from meshforge.synth import generate

        self.surface, self.truth = generate(shape, **params)
        self.input_path = None
        self.input_sha256 = None
        self._invalidate_downstream()
        self.length_unit = None  # synthetic shapes are unitless
        text = (
            f"synth {shape}: {self.surface.n_points} pts, {self.surface.n_cells} tris, "
            f"length {self.truth.length:.6g}, closed {self.truth.closed}\n"
        )
        result = StepResult(
            step="generate_synth",
            summary={"shape": shape, "params": params, "length": self.truth.length},
            text=text,
            render_hints={"mesh": self.surface, "kind": "surface", "label": shape},
        )
        self._record("generate_synth", {"shape": shape, **params}, result)
        return result

    @staticmethod
    def available_shapes() -> list[str]:
        """The synthetic-source shape names ``generate_synth`` accepts.

        Read-only catalog (the sorted ``synth.GENERATORS`` keys), used by the
        web layer's ``GET /catalog`` and mirrored by the Qt GUI's shape combo
        (``gui/app.py`` imports ``GENERATORS`` directly). Lazy-imported so this
        module stays cheap to import."""
        from meshforge.synth import GENERATORS

        return sorted(GENERATORS)

    # -- pipeline steps ---------------------------------------------------
    def compute_centerline(self, **knobs: Any) -> StepResult:
        surface = self._require_surface()
        from meshforge.geometry import compute_centerline

        self.centerline = compute_centerline(surface, **knobs)
        s = self.centerline.summary()
        text = (
            f"centerline: {s['n_samples']} samples, length {s['length']:.6g}, "
            f"radius {s['radius_min']:.4g}..{s['radius_max']:.4g}, closed {s['closed']}\n"
        )
        import pyvista as pv

        poly = pv.lines_from_points(self.centerline.points)
        result = StepResult(
            step="centerline",
            summary=s,
            text=text,
            render_hints={"mesh": poly, "kind": "polyline", "label": "centerline"},
        )
        self._record("centerline", dict(knobs), result)
        return result

    def compute_sections(self, *, every: int = 5, rel_drop: float = 0.2) -> StepResult:
        surface = self._require_surface()
        cl = self._require(self.centerline, "centerline", "compute_centerline")
        from meshforge.geometry import compute_sections, detect_constrictions

        stations = range(0, len(cl.points), max(1, int(every)))
        self.sections = compute_sections(surface, cl, stations=list(stations))
        self.constrictions = detect_constrictions(self.sections, rel_drop=rel_drop)
        areas = [sec.area for sec in self.sections]
        text = (
            f"sections: {len(self.sections)} planes, area "
            f"{min(areas):.6g}..{max(areas):.6g}; "
            f"constrictions: {len(self.constrictions)}\n"
        )
        for c in self.constrictions:
            text += f"  constriction at s={c.s:.4g}, area ratio {c.area_ratio:.3f}\n"
        summary = {
            "n_sections": len(self.sections),
            "area_min": min(areas),
            "area_max": max(areas),
            "constrictions": [
                {"s": c.s, "area": c.area, "area_ratio": c.area_ratio}
                for c in self.constrictions
            ],
        }
        import pyvista as pv

        centers = pv.PolyData([sec.center for sec in self.sections])
        result = StepResult(
            step="sections",
            summary=summary,
            text=text,
            render_hints={"mesh": centers, "kind": "points", "label": "section centers"},
        )
        self._record("sections", {"every": every, "rel_drop": rel_drop}, result)
        return result

    def compute_wall(
        self,
        *,
        thickness: float | None = None,
        outer_surface: str = "",
        **knobs: Any,
    ) -> StepResult:
        surface = self._require_surface()
        from meshforge.geometry import offset_wall

        outer_surface = _clean_path(outer_surface)
        if outer_surface:
            # Path was gathered earlier; the file is read HERE (trap #7).
            from meshforge.geometry import ThicknessSpec
            from meshforge.io import load_surface

            knobs["thickness_spec"] = ThicknessSpec.from_outer_surface(
                load_surface(outer_surface)
            )
        if knobs.get("mode") == "radius_scaled":
            knobs["centerline"] = self._require(
                self.centerline, "centerline", "compute_centerline"
            )
        recorded = {"thickness": thickness, "outer_surface": outer_surface}
        recorded.update(
            {k: v for k, v in knobs.items() if k not in ("thickness_spec", "centerline")}
        )
        self.wall = offset_wall(surface, thickness, **knobs)
        s = self.wall.summary()
        text = (
            f"wall: thickness {s['thickness_min']:.4g}..{s['thickness_max']:.4g}, "
            f"capped {s['n_capped']}, floored {s['n_floored']}\n"
        )
        for note in s["notes"]:
            text += f"  note: {note}\n"
        result = StepResult(
            step="wall",
            summary=s,
            text=text,
            render_hints={"mesh": self.wall.outer, "kind": "surface", "label": "outer wall"},
        )
        self._record("wall", recorded, result)
        return result

    def compute_volmesh(self, *, backend: str = "swept", thickness: float = 0.5, **knobs: Any) -> StepResult:
        surface = self._require_surface()
        from meshforge.meshing import get_backend

        recorded = {"backend": backend, "thickness": thickness}
        recorded.update({k: v for k, v in knobs.items() if k != "centerline"})
        if self.centerline is not None:
            knobs.setdefault("centerline", self.centerline)
        self.volmesh = get_backend(backend).mesh_wall(surface, thickness, **knobs)
        s = self.volmesh.summary()
        q = s["quality"]
        text = (
            f"volmesh[{backend}]: {s['n_cells']} cells, inverted {q['n_inverted']}, "
            f"sj_min {q['sj_min']:.4g}, volume {q['total_volume']:.6g}\n"
            f"  boundary: {s['boundary_faces']}\n"
        )
        if "lattice" in s:
            lat = s["lattice"]
            text += (
                f"  wall rays: capped {lat['n_capped']}, floored {lat['n_floored']} of "
                f"{lat['n_rays']} (curvature_cap {lat['curvature_cap']:g}); thickness "
                f"{lat['thickness_min']:.4g}..{lat['thickness_max']:.4g}\n"
            )
            if lat.get("sweep") == "planes":
                text += (
                    f"  plane sweep ({lat['side']}): capped by tilt {lat['n_capped_tilt']}, "
                    f"by concavity {lat['n_capped_concave']}; tilt max "
                    f"{lat['tilt_max_deg']:.1f} deg; nodes off the surface max "
                    f"{lat['surface_offset_max']:.3g} (section_smoothing "
                    f"{lat['section_smoothing']:g})\n"
                )
        result = StepResult(
            step="volmesh",
            summary=s,
            text=text,
            render_hints={"mesh": self.volmesh.grid, "kind": "volume", "label": f"wall ({backend})"},
        )
        self._record("volmesh", recorded, result)
        return result

    def compute_lumen(self, *, method: str = "auto", **knobs: Any) -> StepResult:
        surface = self._require_surface()
        from meshforge.meshing.lumen import fill_lumen

        recorded = {"method": method}
        recorded.update({k: v for k, v in knobs.items() if k != "centerline"})
        if self.centerline is not None:
            knobs.setdefault("centerline", self.centerline)
        # The lumen mesh takes the volmesh slot: it IS the session's volume
        # mesh (region-tagged, wall/inlet/outlet groups), so deck export,
        # solve, CFD and save work on it unchanged (V47).
        self.volmesh = fill_lumen(surface, method=method, **knobs)
        s = self.volmesh.summary()
        q = s["quality"]
        text = (
            f"lumen[{s['backend']}]: {s['n_cells']} cells, "
            f"inverted {q['n_inverted']}, sj_min {q['sj_min']:.4g}, "
            f"volume {q['total_volume']:.6g}\n"
            f"  boundary: {s['boundary_faces']}\n"
        )
        for note in s["notes"]:
            text += f"  note: {note}\n"
        result = StepResult(
            step="lumen",
            summary=s,
            text=text,
            render_hints={
                "mesh": self.volmesh.grid,
                "kind": "volume",
                "label": f"lumen ({s['backend']})",
            },
        )
        self._record("lumen", recorded, result)
        return result

    # -- roadmap-v2 steps (V12 wiring) --------------------------------------
    def compute_import_cad(
        self, *, path: str = "", block: str = "", mode: str = "auto"
    ) -> StepResult:
        path = _clean_path(path)
        if not path:
            raise ValueError("import_cad needs a drawing path")
        from meshforge.io.cad import load_profile

        self.profile = load_profile(path, block=block or None, mode=mode)
        # Drawing units ($INSUNITS) are carried as text, never converted; a
        # blocks mesh built from them must not inherit an earlier CSO's "mm".
        self.length_unit = None
        p = self.profile
        text = p.to_text()
        import numpy as np
        import pyvista as pv

        # Render hint: all loops as one polyline set.
        pts_list, lines = [], []
        offset = 0
        for lp in p.loops:
            n = len(lp)
            pts_list.append(np.column_stack([lp, np.zeros(n)]))
            lines.append(
                np.concatenate([[n + 1], np.arange(offset, offset + n), [offset]])
            )
            offset += n
        poly = pv.PolyData(np.vstack(pts_list))
        poly.lines = np.concatenate(lines)
        result = StepResult(
            step="import_cad",
            summary={
                "loops": len(p.loops),
                "components": len(p.components),
                "units": p.units,
                "mode": p.provenance["mode"],
                "mesh_ready": p.mesh_ready,
            },
            text=text,
            render_hints={"mesh": poly, "kind": "polyline", "label": "profile"},
        )
        self._record(
            "import_cad", {"path": path, "block": block, "mode": mode}, result
        )
        return result

    def compute_import_cad3d(
        self,
        *,
        path: str = "",
        mesh_size: float | None = None,
        mesh_size_factor: float | None = None,
    ) -> StepResult:
        """V16: STEP/IGES/BREP solid -> triangulated surface as the session
        source (replaces load/generate_synth output; downstream invalidated)."""
        path = _clean_path(path)
        if not path:
            raise ValueError("import_cad3d needs a STEP/IGES/BREP path")
        from meshforge.io import file_sha256
        from meshforge.io.cad3d import load_solid
        from meshforge.quality import inspect_surface

        # load_solid FIRST — it owns the friendly "CAD file not found" and the
        # unsupported-suffix message; hashing first would raise a bare errno.
        self.surface, provenance = load_solid(
            path, mesh_size=mesh_size, mesh_size_factor=mesh_size_factor
        )
        self.input_sha256 = file_sha256(path)
        self.input_path = Path(path)
        self.truth = None
        self._invalidate_downstream()
        self.length_unit = None  # the gmsh import does not carry one
        report = inspect_surface(
            self.surface, path=str(path), sha256=self.input_sha256
        )
        text = (
            f"cad3d: {provenance['format']} with {provenance['n_volumes']} "
            f"volume(s), {provenance['n_surfaces']} surface(s), "
            f"mesh_size {provenance['mesh_size']:.6g}\n"
        ) + report.to_text()
        result = StepResult(
            step="import_cad3d",
            summary={**report.to_dict(), "cad3d": provenance},
            text=text,
            render_hints={
                "mesh": self.surface, "kind": "surface", "label": Path(path).name
            },
        )
        self._record(
            "import_cad3d",
            {
                "path": path,
                "mesh_size": mesh_size,
                "mesh_size_factor": mesh_size_factor,
            },
            result,
            input_sha256=self.input_sha256,
        )
        return result

    def compute_import_image(
        self,
        *,
        path: str = "",
        level: float | None = None,
        spacing: str = "",
    ) -> StepResult:
        """V18: volume (.npy/.nii/DICOM dir) -> iso-surface as the session
        source. ``level`` is required; ``spacing`` overrides .npy voxels."""
        path = _clean_path(path)
        if not path:
            raise ValueError("import_image needs a volume path")
        if level is None:
            raise ValueError(
                "import_image needs an iso level (a segmentation threshold "
                "is a modelling decision — no default)"
            )
        from meshforge.io.image import (
            load_volume_affine,
            override_spacing,
            volume_input_sha256,
            volume_to_surface,
        )
        from meshforge.quality import inspect_surface

        # D83: the full affine, so a rotated/flipped NIfTI maps exactly.
        array, affine, vinfo = load_volume_affine(path)
        if spacing:
            affine = override_spacing(affine, spacing)
        self.input_sha256 = volume_input_sha256(path)
        surface, provenance = volume_to_surface(array, float(level), affine=affine)
        provenance["notes"] = list(vinfo["notes"]) + provenance["notes"]
        self.surface = surface
        self.input_path = Path(path)
        self.truth = None
        self._invalidate_downstream()
        # DICOM patient coordinates are millimetres by the standard; NIfTI
        # follows its xyzt_units (mm, converted, or assumed with a note —
        # D83). A .npy or a spacing override declares nothing we can trust.
        self.length_unit = None if spacing else vinfo["length_unit"]
        report = inspect_surface(
            self.surface, path=str(path), sha256=self.input_sha256
        )
        text = (
            f"image: shape {provenance['shape']}, level {provenance['level']:g}, "
            f"{provenance['n_triangles']} tris, watertight {provenance['watertight']}\n"
        )
        for note in provenance["notes"]:
            text += f"  note: {note}\n"
        text += report.to_text()
        result = StepResult(
            step="import_image",
            summary={**report.to_dict(), "image": provenance},
            text=text,
            render_hints={
                "mesh": self.surface, "kind": "surface", "label": Path(path).name
            },
        )
        self._record(
            "import_image",
            {"path": path, "level": float(level), "spacing": spacing},
            result,
            input_sha256=self.input_sha256,
        )
        return result

    def compute_import_cso(
        self,
        *,
        path: str = "",
        method: str = "auto",
        n_ring: int = 48,
        voxel_size: float | None = None,
        cap_ends: int = 1,
        on_open: str = "skip",
        dicom: str = "",
    ) -> StepResult:
        """V38: MeVisLab Binary .cso contours -> a watertight tube surface as
        the session source. All geometry lives in io/cso.py (rule 4).

        ``method`` (V40): ``auto`` (default) picks the first surface + centreline
        strategy that sweeps to a swept volmesh with ``inverted == 0`` — loft with
        the skeleton centreline, then loft with the interior contour centreline
        (rescues tortuous arches whose skeleton centreline exits the lumen), then
        the voxel fallback.  The chosen centreline is stashed on the session so the
        later ``volmesh`` step reuses the *validated* one.  ``loft``/``voxel`` force
        that single method (no trial), unchanged from V38/V39.
        ``on_open`` (V39) is the open-editor-contour policy: skip | close | error.
        ``dicom`` (V39) anchors the segmentation to a DICOM series: the contours
        are voxelised onto THAT image's grid so the mask aligns with the scan
        (the DICOM is read-only + checksummed, rule 1; forces method='voxel').
        """
        path = _clean_path(path)
        dicom = _clean_path(dicom)
        if not path:
            raise ValueError("import_cso needs a .cso path")
        from meshforge.io.cso import (
            contours_polydata,
            cso_to_surface,
            load_cso_checked,
        )
        from meshforge.quality import inspect_surface

        # Read the .cso ONCE — checksum and parse come from the same buffer, so
        # there is no window for the file to change between the two reads
        # (F-INFO-8 / TOCTOU); the digest still matches sha256(file bytes).
        self.input_sha256, data = load_cso_checked(path, on_open=on_open)
        grid = dicom_sha = None
        if dicom:
            from meshforge.io.image import dicom_grid_checked

            # Read the DICOM once: the rule-1 checksum and the anchoring grid
            # come from the same buffer (no TOCTOU window — closes the D62
            # BACKLOG "--dicom two-read").
            dicom_sha, grid = dicom_grid_checked(dicom)
            method = "voxel"  # image-anchored path forces voxel (V39)
        chosen_centerline = None
        if method == "auto":
            surface, provenance, chosen_centerline, strategy = auto_cso_surface(
                data, n_ring=n_ring, voxel_size=voxel_size, cap_ends=bool(cap_ends)
            )
        else:
            surface, provenance = cso_to_surface(
                data,
                method=method,
                n_ring=n_ring,
                voxel_size=voxel_size,
                cap_ends=bool(cap_ends),
                grid=grid,
            )
            strategy = provenance["method"]
        provenance["strategy"] = strategy
        provenance.update(
            {
                "version": list(data.version),
                "n_contours": len(data.contours),
                "n_guides": len(data.guides),
                "on_open": on_open,
                "frameOfReferenceUID": data.frame_of_reference_uid,
                "seriesInstanceUID": data.series_instance_uid,
            }
        )
        if dicom:
            provenance["dicom"] = str(dicom)
            provenance["dicom_sha256"] = dicom_sha
        provenance.setdefault("notes", [])
        provenance["notes"] = list(provenance["notes"]) + list(data.notes)
        self.surface = surface
        self.input_path = Path(path)
        self.truth = None
        self._invalidate_downstream()
        self.length_unit = "mm"  # CSO world coordinates are millimetres
        # V40: reuse the validated centreline at the volmesh step (None => the
        # volmesh step computes the skeleton centreline itself, as before).
        self.centerline = chosen_centerline
        report = inspect_surface(self.surface, path=str(path), sha256=self.input_sha256)
        text = (
            f"cso: {len(data.contours)} contour(s) via {strategy}, "
            f"{provenance['n_triangles']} tris, watertight {provenance['watertight']}\n"
        )
        for note in provenance["notes"]:
            text += f"  note: {note}\n"
        text += report.to_text()
        result = StepResult(
            step="import_cso",
            summary={**report.to_dict(), "cso": provenance},
            text=text,
            render_hints={
                "mesh": self.surface, "kind": "surface", "label": Path(path).name,
                # QC overlay: the RAW parsed rings drawn over the reconstructed
                # surface (exact control points — a display passthrough).
                "overlay": contours_polydata(data),
                "overlay_label": f"{len(data.contours)} raw contour(s)",
            },
        )
        self._record(
            "import_cso",
            {
                "path": path,
                "method": method,
                "n_ring": n_ring,
                "voxel_size": voxel_size,
                "cap_ends": cap_ends,
                "on_open": on_open,
                "dicom": dicom,
            },
            result,
            input_sha256=self.input_sha256,
        )
        return result

    def compute_segment(
        self,
        *,
        path: str = "",
        device: str = "mps",
        fast: int = 0,
        smoothing: float = 1.0,
        timeout: float = 1800.0,
        lumen_out: str = "",
        lumen_hu: float = 150.0,
    ) -> StepResult:
        """D83 (M1): CT (.nrrd/.nii/.nii.gz) -> TotalSegmentator aorta mask ->
        watertight surface as the session source, in RAS millimetres.

        D85: ``lumen_out`` also writes the contrast blood lumen (CT >=
        ``lumen_hu`` inside the AI mask, largest piece — a threshold, not AI)
        to that .vtp; the session source stays the AI outline.

        All work lives in ``io/segment.py`` (rule 4); TotalSegmentator runs in
        its own env as a subprocess, staged in a temp dir — nothing is written
        next to the CT or into outputs/ by this step.
        """
        path = _clean_path(path)
        if not path:
            raise ValueError("segment needs a CT volume path (.nrrd or .nii/.nii.gz)")
        from meshforge.io.segment import segment_ct
        from meshforge.quality import inspect_surface

        lumen_out = _clean_path(lumen_out)
        if lumen_out:
            from meshforge.export import guard_output_path

            guard_output_path(lumen_out, input_paths=[path])
        seg = segment_ct(
            path,
            device=device,
            fast=bool(fast),
            smoothing=float(smoothing),
            timeout=float(timeout),
            lumen_hu=float(lumen_hu) if lumen_out else None,
        )
        if lumen_out:
            Path(lumen_out).parent.mkdir(parents=True, exist_ok=True)
            seg.lumen_surface.save(lumen_out)
        self.surface = seg.surface
        self.input_path = Path(path)
        self.input_sha256 = seg.input_sha256
        self.truth = None
        self._invalidate_downstream()
        self.length_unit = "mm"  # NRRD anatomical space / NIfTI mm (D83)
        summ = seg.summary
        ts, mask, surf = summ["totalseg"], summ["mask"], summ["surface"]
        report = inspect_surface(self.surface, path=str(path), sha256=self.input_sha256)
        text = (
            f"segment: TotalSegmentator {ts['version'] or '(version unknown)'} "
            f"task total, roi {'+'.join(ts['rois'])}, "
            f"{'fast' if ts['fast'] else 'full'} on {ts['device']}, "
            f"{ts['wall_time_s']:.1f} s\n"
            f"  mask: {mask['kept_voxels']} voxels kept "
            f"({summ['mask_volume_ml']:.1f} mL); dropped "
            f"{mask['dropped_components']} component(s) / "
            f"{mask['dropped_voxels']} voxels; smoothing sigma {mask['sigma']:g} vox\n"
            f"  surface: {surf['n_points']} pts, {surf['n_triangles']} tris, "
            f"watertight {surf['watertight']}, frame {surf['frame']}\n"
        )
        for note in summ["notes"]:
            text += f"  note: {note}\n"
        if lumen_out:
            lum = summ["lumen"]
            text += (
                f"  lumen: {lum['kept_voxels']} voxels >= {lum['lumen_hu']:g} HU kept "
                f"({lum['lumen_volume_ml']:.1f} mL, {100 * lum['fraction_of_mask']:.1f}% of "
                f"the AI mask, Dice {lum['dice_vs_ai_mask']:.4f}); dropped "
                f"{lum['dropped_components']} piece(s) / {lum['dropped_voxels']} voxels -> "
                f"{lumen_out}\n  lumen: {lum['method']}\n"
            )
        text += report.to_text()
        result = StepResult(
            step="segment",
            summary={**report.to_dict(), "segment": summ},
            text=text,
            render_hints={
                "mesh": self.surface, "kind": "surface", "label": Path(path).name
            },
        )
        self._record(
            "segment",
            {
                "path": path,
                "device": device,
                "fast": int(fast),
                "smoothing": float(smoothing),
                "timeout": float(timeout),
                "lumen_out": lumen_out,
                "lumen_hu": float(lumen_hu),
            },
            result,
            input_sha256=self.input_sha256,
        )
        return result

    def compute_crop(
        self,
        *,
        neck_extension_mm: float = 25.0,
        distal_margin_mm: float = 10.0,
        distal_height_mm: float | None = None,
        proximal_height_mm: float | None = None,
        lumen: str = "",
        lumen_out: str = "",
    ) -> StepResult:
        """D85 (M2): crop the session surface (an aorta in millimetres, +z
        superior) to its infrarenal aneurysm segment with two flat caps square
        to the centreline; an optional lumen surface is cut by the SAME planes
        and written to ``lumen_out``. All work lives in ``geometry/crop.py``."""
        surface = self._require_surface()
        if self.length_unit not in (None, "mm"):
            raise ValueError(
                f"crop works in millimetres; this surface is in {self.length_unit!r}"
            )
        lumen, lumen_out = _clean_path(lumen), _clean_path(lumen_out)
        if bool(lumen) != bool(lumen_out):
            raise ValueError(
                "crop: give both lumen (the surface to cut) and lumen_out (where the cut "
                "lumen is written), or neither"
            )
        from meshforge.geometry.crop import crop_aaa
        from meshforge.quality import inspect_surface

        lumen_mesh = None
        if lumen:
            from meshforge.export import guard_output_path
            from meshforge.io import load_surface

            guard_output_path(lumen_out, input_paths=[self.input_path, lumen])
            lumen_mesh = load_surface(lumen)
        res = crop_aaa(
            surface,
            neck_extension_mm=float(neck_extension_mm),
            distal_margin_mm=float(distal_margin_mm),
            distal_height_mm=None if distal_height_mm is None else float(distal_height_mm),
            proximal_height_mm=None if proximal_height_mm is None else float(proximal_height_mm),
            lumen=lumen_mesh,
        )
        rep = res.report()
        rep.pop("profile", None)  # every section: in the CLI's JSON, not the journal
        if self.length_unit is None:
            rep["notes"].append("the surface carries no length unit: millimetres assumed")
        self.surface = res.surface
        self._invalidate_downstream()
        if lumen_out:
            Path(lumen_out).parent.mkdir(parents=True, exist_ok=True)
            res.lumen.save(lumen_out)
        report = inspect_surface(self.surface, path=str(self.input_path), sha256=self.input_sha256)
        text = crop_text(rep) + report.to_text()
        import pyvista as pv

        from meshforge.geometry import centerline_from_surface

        hints: dict[str, Any] = {"mesh": self.surface, "kind": "surface", "label": "AAA crop"}
        seg_cl = centerline_from_surface(self.surface)
        if seg_cl is not None:
            hints["overlay"] = pv.lines_from_points(seg_cl.points)
            hints["overlay_label"] = "segment centreline"
        result = StepResult(
            step="crop", summary={**report.to_dict(), "crop": rep}, text=text,
            render_hints=hints,
        )
        self._record(
            "crop",
            {
                "neck_extension_mm": float(neck_extension_mm),
                "distal_margin_mm": float(distal_margin_mm),
                "distal_height_mm": distal_height_mm,
                "proximal_height_mm": proximal_height_mm,
                "lumen": lumen,
                "lumen_out": lumen_out,
            },
            result,
            input_sha256=self.input_sha256,
        )
        return result

    def compute_sac(
        self,
        *,
        path: str = "",
        lumen_mask: str = "",
        lumen_hu: float = 150.0,
        fat_hu: float = -30.0,
        max_thickness_mm: float = 40.0,
        n_rays: int = 72,
        roi_mm: float = 150.0,
        min_resolved: float = 0.5,
        smoothing: float = 1.0,
    ) -> StepResult:
        """D86 (M2): CT + lumen-only mask -> outer aneurysm sac (lumen +
        thrombus + wall) as the session source surface, RAS millimetres, with
        point fields ``ilt_thickness_mm`` and ``sac_resolved``.

        All work lives in ``geometry/sac.py`` (rule 4); nothing is written.
        """
        path, lumen_mask = _clean_path(path), _clean_path(lumen_mask)
        if not path or not lumen_mask:
            raise ValueError(
                "sac needs a CT volume path and a lumen mask path on the same grid"
            )
        from meshforge.geometry.sac import recover_sac_from_files
        from meshforge.quality import inspect_surface

        run = recover_sac_from_files(
            path,
            lumen_mask,
            lumen_hu=float(lumen_hu),
            fat_hu=float(fat_hu),
            max_thickness_mm=float(max_thickness_mm),
            n_rays=int(n_rays),
            roi_mm=float(roi_mm),
            min_resolved=float(min_resolved),
            smoothing=float(smoothing),
        )
        self.surface = run.surface
        self.input_path = Path(path)
        self.input_sha256 = run.input_sha256["ct"]
        self.truth = None
        self._invalidate_downstream()
        self.length_unit = "mm"
        summ = run.summary
        report = inspect_surface(self.surface, path=str(path), sha256=self.input_sha256)
        text = sac_text(summ)
        text += report.to_text()
        result = StepResult(
            step="sac",
            summary={**report.to_dict(), "sac": summ},
            text=text,
            render_hints={"mesh": self.surface, "kind": "surface", "label": "sac"},
        )
        self._record(
            "sac",
            {
                "path": path,
                "lumen_mask": lumen_mask,
                "lumen_hu": float(lumen_hu),
                "fat_hu": float(fat_hu),
                "max_thickness_mm": float(max_thickness_mm),
                "n_rays": int(n_rays),
                "roi_mm": float(roi_mm),
                "min_resolved": float(min_resolved),
                "smoothing": float(smoothing),
            },
            result,
            input_sha256=self.input_sha256,
        )
        return result

    def compute_blocks(
        self,
        *,
        target_size: float | None = None,
        thickness: float = 1.0,
        n_layers: int = 1,
        holes_like: str = "",
        project: int = 0,
        ogrid: int = 0,
        core_fraction: float = 0.5,
        smooth: int = 0,
        grade_ratio: float = 1.0,
    ) -> StepResult:
        profile = self._require(
            getattr(self, "profile", None), "profile", "compute_import_cad"
        )
        import numpy as np

        from meshforge.meshing import build_block_mesh, extrude_blocks
        from meshforge.meshing.blocks import select_loops

        if ogrid and project:
            raise ValueError("ogrid and project are mutually exclusive")
        if ogrid and grade_ratio != 1.0:
            raise ValueError(
                "grade_ratio applies to lattice meshes only, not ogrid"
            )
        selected, n_matched = select_loops(
            profile.loops, profile.components, holes_like=holes_like or None
        )
        if target_size is None:
            allpts = np.vstack(selected)
            target_size = float(np.hypot(*(allpts.max(0) - allpts.min(0)))) / 60.0
        if ogrid:
            from meshforge.meshing import ogrid_disk

            bm = ogrid_disk(
                selected, target_size=target_size, core_fraction=core_fraction
            )
        elif project:
            from meshforge.meshing import project_profile

            bm = project_profile(
                selected, target_size=target_size, grade_ratio=grade_ratio
            )
        else:
            bm = build_block_mesh(
                selected, target_size=target_size, grade_ratio=grade_ratio
            )
        if smooth:
            from meshforge.meshing.smooth import smooth_lattice

            bm = smooth_lattice(bm)
            if grade_ratio > 1.0:
                bm.notes.append(
                    "WARNING: Winslow smoothing equidistributes — it partially "
                    "undoes the transition grading"
                )
        self.volmesh = extrude_blocks(bm, thickness=thickness, n_layers=n_layers)
        s = self.volmesh.summary()
        q = s["quality"]
        text = (
            f"blocks: {s['n_cells']} hexes ({n_matched} holes-like match), "
            f"inverted {q['n_inverted']}, sj_min {q['sj_min']:.4g}, "
            f"volume {q['total_volume']:.6g}\n"
        )
        result = StepResult(
            step="blocks",
            summary=s,
            text=text,
            render_hints={"mesh": self.volmesh.grid, "kind": "volume", "label": "blocks"},
        )
        self._record(
            "blocks",
            {
                "target_size": target_size,
                "thickness": thickness,
                "n_layers": n_layers,
                "holes_like": holes_like,
                "project": project,
                "ogrid": ogrid,
                "core_fraction": core_fraction,
                "smooth": smooth,
                "grade_ratio": grade_ratio,
            },
            result,
        )
        return result

    def compute_repair(
        self, *, merge_tol: float | None = None, max_hole_edges: int = 16
    ) -> StepResult:
        surface = self._require_surface()
        from meshforge.quality import repair_surface

        repaired, report = repair_surface(
            surface, merge_tol=merge_tol, max_hole_edges=max_hole_edges
        )
        self.surface = repaired
        self._invalidate_downstream()
        result = StepResult(
            step="repair",
            summary=report.to_dict(),
            text=report.to_text(),
            render_hints={"mesh": repaired, "kind": "surface", "label": "repaired"},
        )
        self._record(
            "repair",
            {"merge_tol": merge_tol, "max_hole_edges": max_hole_edges},
            result,
        )
        return result

    def compute_recipe(self, *, path: str = "") -> StepResult:
        path = _clean_path(path)
        if not path:
            raise ValueError("recipe needs a .yaml path")
        from meshforge.recipes import run_recipe

        manifest = run_recipe(path)
        text = "".join(
            f"variant {v['status']}: {v['slug']}"
            + (f" ({v['error']})" if v["error"] else "")
            + "\n"
            for v in manifest["variants"]
        )
        text += f"recipe {manifest['recipe']}: {manifest['n_ok']} ok, {manifest['n_failed']} failed\n"
        return StepResult(
            step="recipe",
            summary={
                "recipe": manifest["recipe"],
                "n_ok": manifest["n_ok"],
                "n_failed": manifest["n_failed"],
            },
            text=text,
        )

    def compute_replay(self, *, workfile: str = "", override: str = "") -> StepResult:
        workfile = _clean_path(workfile)
        overrides: dict[str, dict[str, Any]] = {}
        if override:
            key, sep, raw = override.partition("=")
            step, dot, knob = key.partition(".")
            if not sep or not dot:
                raise ValueError(
                    f"override expects STEP.KNOB=VALUE, got {override!r}"
                )
            try:
                value: Any = int(raw)
            except ValueError:
                try:
                    value = float(raw)
                except ValueError:
                    value = raw
            overrides = {step: {knob: value}}
        target = load_workfile(workfile) if workfile else self
        results = replay(target, overrides)
        text = "".join(r.text for r in results)
        text += f"replayed {len(results)} step(s)\n"
        return StepResult(
            step="replay",
            summary={"n_steps": len(results), "workfile": workfile or "(this session)"},
            text=text,
        )

    def compute_blayers(
        self,
        *,
        first: float | None = None,
        n_layers: int = 5,
        growth: float = 1.2,
        normal_smoothing: int = 10,
    ) -> StepResult:
        """V22: grow external-flow wedge boundary layers outward from the
        session surface; the result becomes the session volmesh (so
        ``save("foam", …)`` completes the CFD chain)."""
        surface = self._require_surface()
        if first is None:
            raise ValueError("blayers needs a first layer thickness (first=...)")
        from meshforge.meshing.prism import mesh_boundary_layers

        self.volmesh = mesh_boundary_layers(
            surface,
            first,
            n_layers=n_layers,
            growth=growth,
            normal_smoothing=normal_smoothing,
        )
        s = self.volmesh.summary()
        q = s["quality"]
        text = (
            f"blayers: {s['n_cells']} wedges, inverted {q['n_inverted']}, "
            f"sj_min {q['sj_min']:.4g}, volume {q['total_volume']:.6g}\n"
        )
        for note in s["notes"]:
            text += f"  note: {note}\n"
        result = StepResult(
            step="blayers",
            summary=s,
            text=text,
            render_hints={"mesh": self.volmesh.grid, "kind": "volume", "label": "boundary layers"},
        )
        self._record(
            "blayers",
            {
                "first": first,
                "n_layers": n_layers,
                "growth": growth,
                "normal_smoothing": normal_smoothing,
            },
            result,
        )
        return result

    def compute_solve(
        self,
        *,
        deck: str = "",
        analysis: str = "static",
        preloaded: int = 0,
        E: float = 1000.0,
        nu: float = 0.3,
        density: float = 0.0,
        n_modes: int = 10,
        conductivity: float = 0.0,
        conductivity_table: str = "",
        temp: str = "",
        flux: str = "",
        film: str = "",
        radiate: str = "",
        stefan_boltzmann: float | None = None,
        absolute_zero: float | None = None,
        specific_heat: float = 0.0,
        transient: int = 0,
        dt: float = 0.0,
        period: float = 0.0,
        t0: float | None = None,
        pressure: str = "",
        cload: str = "",
        expansion: float = 0.0,
        temperature: float | None = None,
        temperature_from: str = "",
        temperature_time: float | None = None,
        t_ref: float | None = None,
        fix: str = "",
        pin_lateral: int = 0,
        nlgeom: int = 0,
        formulation: str = "",
        riks: int = 0,
        riks_max_lpf: float = 0.0,
        riks_dl_init: float = 0.0,
        riks_dl_min: float = 0.0,
        riks_dl_max: float = 0.0,
        ccx: str = "",
        timeout: float = 600.0,
    ) -> StepResult:
        """V13/V24/V31/V32/V41: run CalculiX and read the results back.

        With ``deck`` empty, a ccx deck is built from the session volmesh:
        ``analysis`` picks static (elastic; pressure/fix knobs), frequency
        (*FREQUENCY, needs ``density``; ``preloaded=1`` runs a *STATIC
        preload from the pressure/fix knobs first), heat (*HEAT TRANSFER,
        needs ``conductivity`` or a temperature-dependent
        ``conductivity_table`` k@T,k@T + ``temp``/``flux``/``film``/
        ``radiate`` — ``radiate`` NAME=SINK,EMISS also needs
        ``stefan_boltzmann`` + ``absolute_zero`` for *PHYSICAL CONSTANTS, V41;
        ``transient=1`` adds DIRECT time stepping with ``dt``/``period``/
        ``t0`` and needs ``density`` + ``specific_heat``) or buckle
        (*BUCKLE: factor × load = critical load; loads from the
        pressure/cload knobs). A static solve takes a thermal load (V45):
        ``expansion`` (alpha) + ``t_ref`` (stress-free reference — REQUIRED,
        never guessed) + either ``temperature`` (uniform) or
        ``temperature_from`` (a prior heat solve's .frd, chaining heat ->
        thermal stress); ``temperature_time`` picks one snapshot of a
        TRANSIENT heat history by the frd's own recorded time (V46; default
        = last increment; exact match only, no interpolation). An external
        .inp is solved as-is and every build knob must stay at its default.
        """
        import numpy as np

        from meshforge.solve import attach_fields, frd_to_grid, solve_deck

        deck = _clean_path(deck)
        ccx = _clean_path(ccx)
        temperature_from = _clean_path(temperature_from)
        params = {
            "deck": deck, "analysis": analysis, "preloaded": preloaded,
            "E": E, "nu": nu,
            "density": density, "n_modes": n_modes,
            "conductivity": conductivity,
            "conductivity_table": conductivity_table,
            "temp": temp, "flux": flux,
            "film": film, "radiate": radiate,
            "stefan_boltzmann": stefan_boltzmann,
            "absolute_zero": absolute_zero,
            "specific_heat": specific_heat,
            "transient": transient, "dt": dt, "period": period, "t0": t0,
            "pressure": pressure, "cload": cload,
            "expansion": expansion, "temperature": temperature,
            "temperature_from": temperature_from,
            "temperature_time": temperature_time, "t_ref": t_ref,
            "fix": fix,
            "pin_lateral": pin_lateral,
            "nlgeom": nlgeom, "formulation": formulation,
            "riks": riks, "riks_max_lpf": riks_max_lpf,
            "riks_dl_init": riks_dl_init, "riks_dl_min": riks_dl_min,
            "riks_dl_max": riks_dl_max, "ccx": ccx, "timeout": timeout,
        }
        riks_knobs_set = bool(
            riks_max_lpf or riks_dl_init or riks_dl_min or riks_dl_max
        )
        _validate_solve_knobs(params, riks_knobs_set)
        if deck:
            _refuse_external_deck_build_knobs(params, riks_knobs_set)
            deck_path = Path(deck)
        else:
            vol = self._require(self.volmesh, "volmesh", "compute_volmesh or compute_blocks")
            if vol.quality.n_inverted > 0:
                raise ValueError(
                    f"refusing to solve a mesh with {vol.quality.n_inverted} "
                    f"inverted cell(s)"
                )
            from meshforge.export.deck import DeckSpec, write_deck

            if analysis == "heat":
                material, step, bcs, loads, physical_constants = (
                    _heat_solve_config(params)
                )
                thermal = {}
            else:
                material, step, bcs, loads, thermal = (
                    _mech_solve_config(vol, params)
                )
                physical_constants = None
            spec = DeckSpec(
                material=material,
                step=step,
                boundary_conditions=tuple(bcs),
                loads=tuple(loads),
                target="ccx",
                formulation=formulation or None,
                physical_constants=physical_constants,
                **thermal,
            )
            deck_path = write_deck(
                vol, spec, Path("outputs") / "solve" / "session",
                input_path=self.input_path,
            )
        out_dir = Path("outputs") / "solve" / deck_path.stem
        self.solve = solve_deck(
            deck_path, out_dir, ccx=ccx or None, timeout=timeout
        )
        frd = self.solve.frd
        text = self.solve.to_text()

        # Read-back: attach to the session grid when the frd nodes ARE that
        # grid (deck node ids are 1..N in point order); else rebuild from frd.
        grid = None
        if (
            self.volmesh is not None
            and len(frd.node_ids) == self.volmesh.grid.n_points
            and np.array_equal(frd.node_ids, np.arange(1, len(frd.node_ids) + 1))
        ):
            pts = np.asarray(self.volmesh.grid.points)
            scale = max(1.0, float(np.abs(pts).max()))
            if np.allclose(pts, frd.points, rtol=0.0, atol=2e-5 * scale):
                attach_fields(self.volmesh.grid, frd)
                grid = self.volmesh.grid
                text += "  results attached to the session volmesh point data\n"
        if grid is None:
            grid = frd_to_grid(frd)  # loud when the frd has no element block
            text += "  results grid rebuilt from the frd element block\n"
        for name in ("S_mises", "U_mag"):
            if name in grid.point_data:
                grid.point_data.active_scalars_name = name
                break

        result = StepResult(
            step="solve",
            summary=self.solve.summary(),
            text=text,
            render_hints={"mesh": grid, "kind": "volume", "label": "solve results"},
        )
        self._record("solve", params, result)
        return result

    def compute_aaa_stress(
        self,
        *,
        systolic_mmhg: float = 140.0,
        whatif_mmhg: float = 120.0,
        E: float = 100.0,
        nu: float = 0.49,
        end_zone: str = "neck",
        crop_report: str = "",
        ccx: str = "",
        timeout: float = 600.0,
    ) -> StepResult:
        """D88 (M3): AAA wall stress on the session's swept wall — an
        INVESTIGATIONAL metric. Stiff-linear CalculiX solves (pressure on
        ``inner``, ``inlet``/``outlet`` encastre) at ``systolic_mmhg`` and
        ``whatif_mmhg``; peak wall stress (99th percentile of the
        integration-point von Mises outside the end zones), max principal,
        wall tension. A wall with inverted cells or capped/floored wall rays
        is refused (the metrics are None and the text says why). All work
        lives in ``solve/aaa_metrics.py``; decks, results and the
        ``session_aaa_stress.vtu/.json`` go to ``outputs/aaa_stress/session``.
        """
        import json

        from meshforge.solve.aaa_metrics import crop_report_from_journal, run_aaa_stress

        vol = self._require(self.volmesh, "volmesh", "compute_volmesh")
        crop_report, ccx = _clean_path(crop_report), _clean_path(ccx)
        if crop_report:
            path = Path(crop_report)
            if not path.is_file():
                raise ValueError(f"crop report not found: {crop_report}")
            report = json.loads(path.read_text())
        else:
            report = crop_report_from_journal(self.journal)
        params = {
            "systolic_mmhg": float(systolic_mmhg), "whatif_mmhg": float(whatif_mmhg),
            "E": float(E), "nu": float(nu), "end_zone": str(end_zone),
            "crop_report": crop_report, "ccx": ccx, "timeout": float(timeout),
        }
        out_dir = Path("outputs") / "aaa_stress" / "session"
        res = run_aaa_stress(
            vol, vol.summary(), out_dir=out_dir, stem="session", crop_report=report,
            systolic_mmhg=params["systolic_mmhg"], whatif_mmhg=params["whatif_mmhg"],
            E=params["E"], nu=params["nu"], end_zone=params["end_zone"],
            ccx=ccx or None, timeout=params["timeout"], input_path=self.input_path,
        )
        self.aaa_stress = res
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "session_aaa_stress.json").write_text(
            json.dumps(res.summary, indent=2) + "\n"
        )
        if res.grid is not None:
            res.grid.save(out_dir / "session_aaa_stress.vtu")
        result = StepResult(
            step="aaa_stress",
            summary=res.summary,
            text=res.to_text(),
            render_hints={
                "mesh": res.grid if res.grid is not None else vol.grid,
                "kind": "volume",
                "label": "wall stress (investigational)",
            },
        )
        self._record("aaa_stress", params, result)
        return result

    def compute_split_patch(
        self,
        *,
        group: str = "outer_rim",
        axis: str = "y",
        threshold: float | None = None,
        name: str = "",
        side: str = "above",
    ) -> StepResult:
        """V30: carve a new boundary patch out of a volmesh group by a
        face-centroid coordinate (e.g. the moving lid of a cavity)."""
        import numpy as np

        from meshforge.meshing import split_boundary_group

        params = {
            "group": group, "axis": axis, "threshold": threshold,
            "name": name, "side": side,
        }
        vol = self._require(
            self.volmesh, "volmesh", "compute_volmesh or compute_blocks"
        )
        if threshold is None:
            raise ValueError("split_patch needs a threshold")
        if not name:
            raise ValueError("split_patch needs a non-empty new group name")
        split_boundary_group(
            vol, group, axis=axis, threshold=float(threshold),
            name=name, side=side,
        )
        note = vol.notes[-1]
        summary = {
            "groups": {k: g.n_faces for k, g in vol.boundary.items()},
            "note": note,
        }
        result = StepResult(
            step="split_patch",
            summary=summary,
            text=f"split_patch  : {note}\n",
            render_hints={
                "mesh": vol.boundary[name].to_polydata(
                    np.asarray(vol.grid.points)
                ),
                "kind": "surface",
                "label": f"patch {name}",
            },
        )
        self._record("split_patch", params, result)
        return result

    def compute_cfd(
        self,
        *,
        case: str = "",
        solver: str = "simpleFoam",
        nu: float = 1e-3,
        inlet: str = "",
        outlet: str = "",
        end_time: float = 2000.0,
        delta_t: float = 0.0,
        write_interval: float = 0.0,
        timeout: float = 600.0,
        turbulence: str = "laminar",
        intensity: float = 0.05,
        turb_length: float = 0.0,
        series: int = 0,
        moving_wall: str = "",
        empty: str = "",
        closed_cavity: int = 0,
        p_ref_cell: int = 0,
        p_ref_value: float = 0.0,
    ) -> StepResult:
        """V23/V27: run OpenFOAM and read U/p (+k/omega/nut) back.

        With ``case`` empty, an incompressible case — laminar or steady
        kOmegaSST RANS per the ``turbulence`` knob — is built from the
        session volmesh under outputs/cfd/session/ (patch BCs from the
        inlet/outlet knobs, every other patch a no-slip wall); otherwise
        the given complete case runs as-is and the BC knobs must stay empty.
        With ``series`` (V29), EVERY written time step is read back as a
        frame — a read-back knob, so it applies to both forms.
        """
        from meshforge.solve import solve_case

        case = _clean_path(case)
        params = {
            "case": case, "solver": solver, "nu": nu, "inlet": inlet,
            "outlet": outlet, "end_time": end_time, "delta_t": delta_t,
            "write_interval": write_interval, "timeout": timeout,
            "turbulence": turbulence, "intensity": intensity,
            "turb_length": turb_length, "series": series,
            "moving_wall": moving_wall, "empty": empty,
            "closed_cavity": closed_cavity, "p_ref_cell": p_ref_cell,
            "p_ref_value": p_ref_value,
        }
        inflow = section = None  # session-built cases only (D80/D82)
        if case:
            if inlet or outlet or moving_wall or empty:
                raise ValueError(
                    "an existing case already carries its BCs — the "
                    "inlet/outlet/moving_wall/empty knobs apply only to "
                    "session-built cases"
                )
            if turbulence != "laminar":
                raise ValueError(
                    "an existing case already carries its turbulence setup "
                    "— the turbulence knobs apply only to session-built cases"
                )
            if closed_cavity:
                raise ValueError(
                    "an existing case already carries its fvSolution — the "
                    "closed_cavity/pRef knobs apply only to session-built "
                    "cases"
                )
            case_dir = Path(case)
            self.cfd = solve_case(case_dir, timeout=timeout, series=bool(series))
        else:
            # Cheap validation first: knob syntax and spec consistency are
            # checked before any mesh is required.
            import dataclasses

            from meshforge.solve import CfdBC, CfdSpec, write_case

            bcs = {}
            inlet_name = inlet_speed = None
            if inlet:
                inlet_name, velocity = _parse_inlet_knob(inlet)
                if isinstance(velocity, float):
                    # D80: a scalar speed along the patch's inward normal,
                    # resolved once the mesh is in hand. A provisional vector
                    # of the same magnitude lets the spec validate now (the
                    # turbulence inlet scales use |U| only).
                    inlet_speed, velocity = velocity, (velocity, 0.0, 0.0)
                bcs[inlet_name] = CfdBC("velocity", velocity)
            if outlet:
                name, p_value = _parse_outlet_spec(outlet)
                bcs[name] = CfdBC("pressure", p_value)
            if moving_wall:
                name, velocity = _parse_inlet_spec(moving_wall)
                bcs[name] = CfdBC("moving_wall", velocity)
            for patch in filter(None, (p.strip() for p in empty.split(","))):
                bcs[patch] = CfdBC("empty")
            spec = CfdSpec(
                solver=solver,
                nu=nu,
                bcs=bcs,
                end_time=end_time,
                delta_t=delta_t or None,
                write_interval=write_interval or None,
                turbulence=turbulence,
                turbulence_intensity=intensity,
                turbulence_length=turb_length or None,
                closed_cavity=bool(closed_cavity),
                p_ref_cell=p_ref_cell,
                p_ref_value=p_ref_value,
            )
            vol = self._require(
                self.volmesh, "volmesh", "compute_volmesh or compute_blocks"
            )
            if vol.quality.n_inverted > 0:
                raise ValueError(
                    f"refusing to run CFD on a mesh with "
                    f"{vol.quality.n_inverted} inverted cell(s)"
                )
            inflow_notes_ = []
            if inlet_name is not None:
                from meshforge.solve.inflow import (
                    inflow_notes,
                    inflow_report,
                    patch_geometry,
                    resolve_inlet_speed,
                )

                geo = None
                if inlet_speed is not None:
                    velocity, geo = resolve_inlet_speed(vol, inlet_name, inlet_speed)
                    spec = dataclasses.replace(
                        spec,
                        bcs={**spec.bcs, inlet_name: CfdBC("velocity", velocity)},
                    )
                else:
                    velocity = spec.bcs[inlet_name].value
                    try:  # the vector form needs no geometry - report if we can
                        geo = patch_geometry(vol, inlet_name)
                    except ValueError as exc:
                        inflow_notes_.append(f"inflow report unavailable: {exc}")
                if geo is not None:
                    inflow = inflow_report(geo, velocity, nu, self.length_unit)
                    inflow_notes_ += inflow_notes(
                        inflow, laminar=turbulence == "laminar"
                    )
            case_dir = Path("outputs") / "cfd" / "session"
            write_case(vol, case_dir, spec, input_path=self.input_path)
            self.cfd = solve_case(
                case_dir, solver=solver, timeout=timeout, series=bool(series)
            )
            self.cfd.notes.extend(inflow_notes_)
            section = self._flow_section(vol, through_flow=inlet_name is not None)

        text = self.cfd.to_text()
        grid = self.cfd.grid
        render_hints = {"mesh": grid, "kind": "volume", "label": "cfd results"}
        if self.cfd.frames is not None:
            render_hints["frames"] = list(
                zip(self.cfd.times, self.cfd.frames)
            )
        if section is not None:
            # D82: the viewport samples the flow onto this centreline section
            # and looks at it face-on, so the inflow is visible, not the wall.
            render_hints["section"] = section.surface
            render_hints["view_dir"] = list(section.view_dir)
        summary = self.cfd.summary()
        if inflow is not None:
            summary["inflow"] = inflow
        result = StepResult(
            step="cfd",
            summary=summary,
            text=text,
            render_hints=render_hints,
        )
        self._record("cfd", params, result)
        return result

    def compute_screenshot(self, *, path: str = "") -> StepResult:
        """Offscreen-safe render of the current state (no Qt involved)."""
        path = _clean_path(path)
        import pyvista as pv

        out = Path(path) if path else Path("outputs") / "session.png"
        out.parent.mkdir(parents=True, exist_ok=True)
        plotter = pv.Plotter(off_screen=True, window_size=(1024, 768))
        shown = []
        if self.volmesh is not None:
            plotter.add_mesh(self.volmesh.grid, show_edges=True)
            shown.append("volmesh")
        elif self.wall is not None:
            plotter.add_mesh(self.wall.outer, opacity=0.5)
            shown.append("wall")
        elif self.surface is not None:
            plotter.add_mesh(self.surface)
            shown.append("surface")
        else:
            raise RuntimeError("nothing to render — run a source step first")
        if self.centerline is not None:
            plotter.add_mesh(
                pv.lines_from_points(self.centerline.points), color="red", line_width=3
            )
            shown.append("centerline")
        plotter.screenshot(str(out))
        plotter.close()
        text = f"screenshot ({', '.join(shown)}) -> {out}\n"
        return StepResult(
            step="screenshot", summary={"path": str(out), "shown": shown}, text=text
        )

    # -- output -----------------------------------------------------------
    def save(self, what: str, out_path: str | Path, *, fmt: str | None = None) -> StepResult:
        from meshforge.export import export_surface, guard_output_path

        out_path = Path(_clean_path(out_path))
        if what == "surface":
            mesh = self._require_surface()
            written = export_surface(mesh, out_path, fmt=fmt, input_path=self.input_path)
        elif what == "wall_outer":
            wall = self._require(self.wall, "wall", "compute_wall")
            written = export_surface(wall.outer, out_path, fmt=fmt, input_path=self.input_path)
        elif what == "volmesh":
            vol = self._require(self.volmesh, "volmesh", "compute_volmesh")
            guard_output_path(out_path, input_paths=[self.input_path])
            out_path.parent.mkdir(parents=True, exist_ok=True)
            vol.grid.save(out_path)
            written = out_path
        elif what == "abaqus":
            from meshforge.export import export_abaqus

            vol = self._require(self.volmesh, "volmesh", "compute_volmesh")
            written = export_abaqus(
                vol, out_path, formulation=fmt or "hybrid", input_path=self.input_path
            )
        elif what == "foam":
            from meshforge.export.foam import write_polymesh

            vol = self._require(self.volmesh, "volmesh", "compute_volmesh")
            written = write_polymesh(vol, out_path, input_path=self.input_path)
        elif what == "fluent":
            from meshforge.export.fluent import write_fluent

            vol = self._require(self.volmesh, "volmesh", "compute_volmesh")
            written = write_fluent(vol, out_path, input_path=self.input_path)
        else:
            raise ValueError(
                f"unknown save target {what!r}; "
                f"use surface | wall_outer | volmesh | abaqus | foam | fluent"
            )
        text = f"saved {what} -> {written}\n"
        result = StepResult(
            step="save", summary={"what": what, "path": str(written)}, text=text
        )
        self._record(
            "save", {"what": what, "out_path": str(out_path), "fmt": fmt}, result
        )
        return result

    # -- D80: inflow picker + presets (read-only; nothing is journaled) ----
    # The Workbench turns these into the cfd knobs' plain scalar form
    # (inlet=NAME=SPEED, outlet=NAME=0, nu=...), so a recipe or replay of the
    # recorded step is identical to the click that produced it.
    def flow_ends(self, point) -> dict[str, Any]:
        """Which flat end cap of the session volume mesh is the inflow, given
        a point picked near it; the other cap is the outflow."""
        from meshforge.solve.inflow import flow_ends

        vol = self._require(self.volmesh, "volmesh", "compute_lumen or compute_volmesh")
        return flow_ends(vol, point)

    def inflow_preview(self, patch: str, speed: float, nu: float) -> dict[str, Any]:
        """Velocity, flow rate and Reynolds number a scalar inlet speed would
        prescribe on *patch* - before anything runs."""
        from meshforge.solve.inflow import inflow_notes, inflow_report, resolve_inlet_speed

        vol = self._require(self.volmesh, "volmesh", "compute_lumen or compute_volmesh")
        if not nu > 0.0:
            raise ValueError(f"kinematic viscosity must be positive, got {nu}")
        velocity, geo = resolve_inlet_speed(vol, patch, speed)
        report = inflow_report(geo, velocity, nu, self.length_unit)
        report["notes"] = inflow_notes(report)
        return report

    def cfd_presets(self) -> dict[str, Any]:
        """Cardiac nu / inlet-speed presets in this source's length unit
        (loud when the source declares no unit)."""
        from meshforge.solve.inflow import cardiac_presets

        return cardiac_presets(self.length_unit)

    def _flow_section(self, vol, *, through_flow: bool):
        """D82: the centreline section a CFD result is viewed on, or None.

        A display aid, never a result: failures become a note on the CFD
        text instead of failing a solve that succeeded."""
        if self.centerline is None:
            if through_flow:
                self.cfd.notes.append(
                    "flow section: no session centreline (run centerline) - "
                    "the viewport shows the boundary only"
                )
            return None
        from meshforge.geometry.ribbon import centerline_ribbon

        try:
            section = centerline_ribbon(self.centerline, vol.grid)
        except ValueError as exc:
            self.cfd.notes.append(f"flow section unavailable: {exc}")
            return None
        self.cfd.notes.extend(f"flow section: {n}" for n in section.notes)
        return section

    # -- helpers -----------------------------------------------------------
    def _require_surface(self):
        return self._require(self.surface, "surface", "load or generate_synth")

    @staticmethod
    def _require(value, name: str, producer: str):
        if value is None:
            raise RuntimeError(f"no {name} available — run {producer} first")
        return value

    def _invalidate_downstream(self) -> None:
        self.centerline = None
        self.sections = None
        self.constrictions = None
        self.wall = None
        self.volmesh = None
        self.solve = None
        self.cfd = None
        self.aaa_stress = None

    # -- work files (V10) --------------------------------------------------
    def save_workfile(self, path: str | Path) -> Path:
        """Persist the journal as a .mfwork JSON work file."""
        import json

        if not self.journal:
            raise ValueError("nothing to save: the session journal is empty")
        path = Path(_clean_path(path))
        if path.suffix != ".mfwork":
            path = path.with_suffix(".mfwork")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"version": 1, "journal": self.journal}, indent=2) + "\n"
        )
        return path


_WORKFILE_STEPS = (
    "load",
    "generate_synth",
    "centerline",
    "sections",
    "wall",
    "volmesh",
    "lumen",
    "save",
    "import_cad",
    "import_cad3d",
    "import_image",
    "import_cso",
    "segment",
    "sac",
    "crop",
    "blocks",
    "repair",
    "solve",
    "aaa_stress",
    "blayers",
    "split_patch",
    "cfd",
)

def load_workfile(path: str | Path) -> MeshingSession:
    """Load a .mfwork file into a fresh session (journal only, no state);
    run :func:`replay` to recompute. Corrupted files fail loudly."""
    import json

    path = Path(_clean_path(path))
    if not path.is_file():
        raise FileNotFoundError(f"work file not found: {path}")
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise ValueError(f"corrupted work file (not valid JSON): {path}: {exc}") from exc
    if (
        not isinstance(data, dict)
        or data.get("version") != 1
        or not isinstance(data.get("journal"), list)
        or not data["journal"]
    ):
        raise ValueError(
            f"corrupted work file: {path} needs version=1 and a non-empty journal"
        )
    for i, rec in enumerate(data["journal"]):
        if (
            not isinstance(rec, dict)
            or rec.get("step") not in _WORKFILE_STEPS
            or not isinstance(rec.get("params"), dict)
        ):
            raise ValueError(
                f"corrupted work file: journal[{i}] needs a known step "
                f"({'/'.join(_WORKFILE_STEPS)}) and a params mapping"
            )
    session = MeshingSession()
    session.journal = data["journal"]
    return session


def replay(
    session: MeshingSession, overrides: dict[str, dict[str, Any]] | None = None
) -> list[StepResult]:
    """Re-run the journal, applying ``overrides`` ({step: {param: value}}).

    Only steps from the earliest overridden one onward are re-executed —
    upstream results already held by the session are reused (the existing
    downstream-invalidation semantics). A freshly loaded work-file session
    holds no state, so everything runs.
    """
    if not session.journal:
        raise ValueError("cannot replay: session journal is empty")
    overrides = overrides or {}
    step_names = [rec["step"] for rec in session.journal]
    for name in overrides:
        if name not in step_names:
            raise ValueError(
                f"override targets unknown step {name!r}; journal has {step_names}"
            )

    start = 0
    if overrides and session.surface is not None:
        start = min(step_names.index(n) for n in overrides)
        # A source override invalidates everything anyway.
    if session.surface is None:
        start = 0

    plan = [
        {
            "step": rec["step"],
            "params": {**rec["params"], **overrides.get(rec["step"], {})},
        }
        for rec in session.journal
    ]
    session.journal = session.journal[:start]  # re-run steps re-record

    results: list[StepResult] = []
    for entry in plan[start:]:
        name, params = entry["step"], dict(entry["params"])
        if name == "load":
            results.append(session.load(params["path"]))
        elif name == "generate_synth":
            results.append(session.generate_synth(params.pop("shape"), **params))
        elif name == "centerline":
            results.append(session.compute_centerline(**params))
        elif name == "sections":
            results.append(session.compute_sections(**params))
        elif name == "wall":
            results.append(session.compute_wall(**params))
        elif name == "volmesh":
            results.append(session.compute_volmesh(**params))
        elif name == "lumen":
            results.append(session.compute_lumen(**params))
        elif name == "save":
            results.append(
                session.save(
                    params["what"], params["out_path"], fmt=params.get("fmt")
                )
            )
        elif name == "import_cad":
            results.append(session.compute_import_cad(**params))
        elif name == "import_cad3d":
            results.append(session.compute_import_cad3d(**params))
        elif name == "import_image":
            results.append(session.compute_import_image(**params))
        elif name == "import_cso":
            results.append(session.compute_import_cso(**params))
        elif name == "segment":
            results.append(session.compute_segment(**params))
        elif name == "crop":
            results.append(session.compute_crop(**params))
        elif name == "sac":
            results.append(session.compute_sac(**params))
        elif name == "blocks":
            results.append(session.compute_blocks(**params))
        elif name == "repair":
            results.append(session.compute_repair(**params))
        elif name == "solve":
            results.append(session.compute_solve(**params))
        elif name == "aaa_stress":
            results.append(session.compute_aaa_stress(**params))
        elif name == "blayers":
            results.append(session.compute_blayers(**params))
        elif name == "split_patch":
            results.append(session.compute_split_patch(**params))
        elif name == "cfd":
            results.append(session.compute_cfd(**params))
        else:  # pragma: no cover - load_workfile validates step names
            raise ValueError(f"unknown journal step {name!r}")
    return results

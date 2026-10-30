from __future__ import annotations

import argparse
import sys
from pathlib import Path


def _path_arg(raw: str) -> Path:
    r"""argparse ``type=`` for every path argument.

    Normalises the shell habits a user carries in — ``~``, surrounding quotes,
    ``\ `` drag-into-terminal escapes, stray whitespace — BEFORE anything
    opens or checksums the file, so a mistyped path reports the loader's own
    message instead of a raw ``[Errno 2]``. Same core helper the Workbench and
    the service layer use (``meshforge.io.paths``); lazy-imported so ``--help``
    stays cheap.
    """
    from meshforge.io.paths import normalize_path

    return normalize_path(raw)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="meshforge",
        description="Surfaces in, solver-ready wall volume meshes out.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_inspect = sub.add_parser(
        "inspect",
        help="Read-only inspection of a surface file (counts, bounds, area, "
        "watertightness, volume); writes a report to the outputs dir.",
    )
    p_inspect.add_argument("surface", type=_path_arg, help="surface file (.stl/.ply/.vtp/...)")
    p_inspect.add_argument(
        "--outputs",
        type=_path_arg,
        default=Path("outputs"),
        help="directory for generated reports (default: outputs/)",
    )

    p_export = sub.add_parser(
        "export",
        help="Export a surface to a neutral format (VTU default, VTK/PLY/OBJ); "
        "never overwrites the input.",
    )
    p_export.add_argument("surface", type=_path_arg, help="surface file (.stl/.ply/.vtp/...)")
    p_export.add_argument(
        "--out",
        type=_path_arg,
        default=None,
        help="output path (default: outputs/<stem>.<format>)",
    )
    p_export.add_argument(
        "--format",
        dest="fmt",
        default=None,
        help="vtu (default) | vtk | ply | obj; inferred from --out suffix when omitted",
    )
    p_export.add_argument("--outputs", type=_path_arg, default=Path("outputs"))

    p_check = sub.add_parser(
        "check",
        help="Surface readiness check: open edges, non-manifold edges, "
        "watertight, all-triangles, mesh_ready.",
    )
    p_check.add_argument("surface", type=_path_arg, help="surface file (.stl/.ply/.vtp/...)")
    p_check.add_argument("--outputs", type=_path_arg, default=Path("outputs"))

    p_synth = sub.add_parser(
        "synth",
        help="Generate a synthetic watertight tube surface + exact analytic "
        "centerline truth (straight_tube, curved_tube, tapered_tube, "
        "constricted_tube, torus).",
    )
    p_synth.add_argument("shape", help="generator name (see --help text)")
    p_synth.add_argument(
        "--set",
        dest="params",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="override a generator parameter (repeatable), e.g. --set radius=2.5",
    )
    p_synth.add_argument(
        "--degrade-noise",
        type=float,
        default=0.0,
        help="degrade: gaussian vertex noise sigma (repair regression aid)",
    )
    p_synth.add_argument(
        "--degrade-holes", type=int, default=0, help="degrade: poke N face holes"
    )
    p_synth.add_argument(
        "--degrade-dup-faces", type=int, default=0, help="degrade: duplicate N faces"
    )
    p_synth.add_argument("--outputs", type=_path_arg, default=Path("outputs"))

    p_repair = sub.add_parser(
        "repair",
        help="Repair a surface: merge duplicate vertices, drop degenerate/"
        "duplicate faces, fill small holes; non-manifold edges are reported, "
        "never auto-fixed.",
    )
    p_repair.add_argument("surface", type=_path_arg, help="surface file (.stl/.ply/.vtp/...)")
    p_repair.add_argument(
        "--merge-tol",
        type=float,
        default=None,
        help="absolute vertex merge tolerance (default: 1e-9 x bbox diagonal)",
    )
    p_repair.add_argument(
        "--max-hole-edges",
        type=int,
        default=16,
        help="fill holes up to this many boundary edges (default 16)",
    )
    p_repair.add_argument("--out", type=_path_arg, default=None, help="repaired surface path")
    p_repair.add_argument("--outputs", type=_path_arg, default=Path("outputs"))

    p_vol = sub.add_parser(
        "volmesh",
        help="Volume-mesh the wall region of a tubular surface "
        "(backends: swept hexahedra, gmsh tetrahedra).",
    )
    p_vol.add_argument("surface", type=_path_arg)
    p_vol.add_argument(
        "--thickness", type=float, default=None, help="wall thickness (scalar)"
    )
    p_vol.add_argument(
        "--outer-surface",
        type=_path_arg,
        default=None,
        help="measured outer surface file (mutually exclusive with --thickness)",
    )
    p_vol.add_argument("--backend", default="swept", help="swept (default) | tet")
    p_vol.add_argument("--n-theta", type=int, default=32)
    p_vol.add_argument("--n-axial", type=int, default=60)
    p_vol.add_argument("--n-layers", type=int, default=4, help="radial layers (swept only)")
    p_vol.add_argument(
        "--layer-ratio",
        type=float,
        default=1.0,
        help="radial grading ratio, outer/inner layer thickness (swept only)",
    )
    p_vol.add_argument(
        "--curvature-cap",
        type=float,
        default=0.5,
        help="swept only: fraction of the fold radius 1/|kappa| a wall ray may reach on a "
        "concave bend before it is capped (default 0.5; 0.9 for an aneurysm sac)",
    )
    p_vol.add_argument(
        "--sweep",
        choices=("centreline", "planes", "auto"),
        default="centreline",
        help="swept only: station layout — centreline (default: rays square to the "
        "centreline), planes (sections parallel to two flat parallel end caps; for a "
        "short, wide, eccentric segment such as a cropped aneurysm sac), auto "
        "(centreline unless its wall inverts or is capped/floored, then planes)",
    )
    p_vol.add_argument(
        "--side",
        choices=("outward", "inward"),
        default="outward",
        help="planes only: grow the wall outward of the surface (default) or inward "
        "(the surface is the outer wall, e.g. a recovered sac)",
    )
    p_vol.add_argument(
        "--max-tilt",
        type=float,
        default=None,
        help="planes only: largest surface tilt to the station planes (degrees) before "
        "the wall is thinned and counted as capped (default 80)",
    )
    p_vol.add_argument(
        "--section-smoothing",
        type=float,
        default=0.0,
        help="planes only: Gaussian smoothing of each section along its length "
        "(length units; default 0 = exact) for a surface rougher than the wall; the "
        "distance the nodes move off the surface is reported",
    )
    p_vol.add_argument("--mesh-size", type=float, default=None, help="target size (tet only)")
    p_vol.add_argument(
        "--no-optimize",
        action="store_true",
        help="skip gmsh mesh optimization (tet only; default: Netgen with fallback)",
    )
    p_vol.add_argument("--out", type=_path_arg, default=None, help="output .vtu path")
    p_vol.add_argument("--outputs", type=_path_arg, default=Path("outputs"))

    p_lumen = sub.add_parser(
        "lumen",
        help="Fill the blood volume (lumen) of a tubular surface: swept "
        "O-grid hexahedra where the geometry permits, automatic gmsh tet "
        "fallback with a loud fold-number note where it does not.",
    )
    p_lumen.add_argument("surface", type=_path_arg)
    p_lumen.add_argument(
        "--method",
        default="auto",
        help="auto (default: O-grid, then tet, then surface-tet on failure) | ogrid | tet | surface-tet",
    )
    p_lumen.add_argument("--n-theta", type=int, default=32, help="rays per station")
    p_lumen.add_argument(
        "--n-axial", type=int, default=40, help="stations along the centerline"
    )
    p_lumen.add_argument(
        "--core-fraction",
        type=float,
        default=0.5,
        help="O-grid core size fraction of centroid-to-rim",
    )
    p_lumen.add_argument(
        "--smooth",
        type=int,
        default=4,
        help="guarded smoothing iterations, sj_min can never regress (0 = off)",
    )
    p_lumen.add_argument(
        "--mesh-size",
        type=float,
        default=None,
        help="tet-fallback target size (default: 1.5x median ring edge)",
    )
    p_lumen.add_argument("--out", type=_path_arg, default=None, help="output .vtu path")
    p_lumen.add_argument("--outputs", type=_path_arg, default=Path("outputs"))

    p_cad = sub.add_parser(
        "import-cad",
        help="Import a 2-D CAD drawing (DXF, or DWG via dwg2dxf) as a cleaned-up "
        "profile: chains loose segments into closed loops, classifies "
        "outers/holes; writes profile JSON + report to the outputs dir.",
    )
    p_cad.add_argument("file", type=_path_arg, help="drawing file (.dxf/.dwg)")
    p_cad.add_argument(
        "--block",
        default=None,
        help="block name to import (default: modelspace, falling back to the "
        "largest named block when modelspace has no supported entities)",
    )
    p_cad.add_argument(
        "--list-blocks",
        action="store_true",
        help="list modelspace + named blocks with entity counts, then exit",
    )
    p_cad.add_argument(
        "--layers", default=None, help="comma-separated layer names to import"
    )
    p_cad.add_argument(
        "--mode",
        default="auto",
        choices=("auto", "chain", "faces"),
        help="loop recovery: 'chain' endpoint chaining (dirty part outlines), "
        "'faces' planar face extraction (tiled/architectural drawings), "
        "'auto' chain first then faces if chains stay unclosed at junctions",
    )
    p_cad.add_argument(
        "--chain-tol",
        type=float,
        default=None,
        help="endpoint chaining tolerance (default: 1e-6 x bbox diagonal)",
    )
    p_cad.add_argument(
        "--arc-tol",
        type=float,
        default=None,
        help="curve flattening tolerance, max sagitta "
        "(default: 1e-4 x bbox diagonal)",
    )
    p_cad.add_argument("--outputs", type=_path_arg, default=Path("outputs"))

    p_deck = sub.add_parser(
        "deck",
        help="Write a complete solver input deck: wall mesh + material + "
        "boundary conditions + loads + step (targets: abaqus/ccx .inp, "
        "nastran .bdf, lsdyna .k, ansys .cdb — LS-DYNA and ANSYS are "
        "format-level only, no solver installed).",
    )
    p_deck.add_argument("surface", type=_path_arg, help="inner surface file")
    p_deck.add_argument("--thickness", type=float, default=None)
    p_deck.add_argument("--outer-surface", type=_path_arg, default=None)
    p_deck.add_argument("--backend", default="swept", help="swept (default) | tet")
    p_deck.add_argument("--n-theta", type=int, default=32)
    p_deck.add_argument("--n-axial", type=int, default=60)
    p_deck.add_argument("--n-layers", type=int, default=4)
    p_deck.add_argument("--layer-ratio", type=float, default=1.0)
    p_deck.add_argument("--mesh-size", type=float, default=None)
    p_deck.add_argument(
        "--material",
        default="elastic",
        choices=("elastic", "neo_hooke", "mooney_rivlin", "thermal"),
    )
    p_deck.add_argument(
        "--analysis",
        default="static",
        choices=("static", "frequency", "heat", "buckle"),
        help="step kind: frequency = *FREQUENCY modal (needs --density; "
        "--preloaded adds a *STATIC preload step, V32), heat = *HEAT "
        "TRANSFER (needs --material thermal --conductivity; --transient "
        "for time stepping), buckle = *BUCKLE (factor x load = critical "
        "load, V32)",
    )
    p_deck.add_argument(
        "--n-modes", type=int, default=10,
        help="eigenmodes/buckling factors (frequency/buckle; buckle trap: "
        "too few factors makes ccx silently skip the lowest modes)",
    )
    p_deck.add_argument(
        "--preloaded",
        action="store_true",
        help="preloaded modes: *STATIC step with the loads, then "
        "*FREQUENCY, PERTURBATION (frequency only)",
    )
    p_deck.add_argument(
        "--conductivity", type=float, default=None, help="thermal conductivity k"
    )
    p_deck.add_argument(
        "--temp",
        action="append",
        default=[],
        metavar="NAME,value",
        help="fixed temperature on a boundary group (repeatable, heat)",
    )
    p_deck.add_argument(
        "--flux",
        action="append",
        default=[],
        metavar="NAME,value",
        help="*DFLUX surface heat flux, positive into the body (repeatable, heat)",
    )
    p_deck.add_argument(
        "--film",
        action="append",
        default=[],
        metavar="NAME,sink,h",
        help="*FILM convection on a boundary group, expanded element-wise "
        "(repeatable, heat, V31)",
    )
    p_deck.add_argument(
        "--radiate",
        action="append",
        default=[],
        metavar="NAME,sink,emiss",
        help="*RADIATE environmental radiation on a boundary group, expanded "
        "element-wise (repeatable, heat, V41; needs --stefan-boltzmann and "
        "--absolute-zero for *PHYSICAL CONSTANTS)",
    )
    p_deck.add_argument(
        "--stefan-boltzmann",
        type=float,
        default=None,
        help="Stefan-Boltzmann constant for *PHYSICAL CONSTANTS (with --radiate)",
    )
    p_deck.add_argument(
        "--absolute-zero",
        type=float,
        default=None,
        help="absolute-zero offset in the model temperature unit: 0 (Kelvin) "
        "or -273.15 (Celsius) — a wrong offset is silently wrong (with --radiate)",
    )
    p_deck.add_argument(
        "--conductivity-table",
        default=None,
        metavar="k@T,k@T,...",
        help="temperature-dependent *CONDUCTIVITY, e.g. 2@0,12@1000 "
        "(heat, V41; use instead of --conductivity)",
    )
    p_deck.add_argument(
        "--specific-heat",
        type=float,
        default=None,
        help="specific heat c_p (transient heat needs it plus --density)",
    )
    p_deck.add_argument(
        "--transient",
        action="store_true",
        help="transient *HEAT TRANSFER, DIRECT (needs --dt --period --t0; "
        "dt is the accuracy dial — ccx integrates backward Euler, first order)",
    )
    p_deck.add_argument(
        "--dt", type=float, default=None, help="transient time increment"
    )
    p_deck.add_argument(
        "--period", type=float, default=None, help="transient total time"
    )
    p_deck.add_argument(
        "--t0", type=float, default=None,
        help="initial temperature (transient — never guessed)",
    )
    p_deck.add_argument("--E", dest="E", type=float, default=None, help="Young's modulus")
    p_deck.add_argument("--nu", type=float, default=None, help="Poisson's ratio")
    p_deck.add_argument("--density", type=float, default=None)
    p_deck.add_argument("--c10", type=float, default=None)
    p_deck.add_argument("--c01", type=float, default=None)
    p_deck.add_argument("--d1", type=float, default=None)
    p_deck.add_argument(
        "--fix",
        action="append",
        default=[],
        metavar="NAME,first[,last]",
        help="zero a DOF range on a boundary group (repeatable), "
        "e.g. --fix inlet,3 or --fix inlet,1,3",
    )
    p_deck.add_argument(
        "--pin-lateral",
        action="store_true",
        help="add three tangential single-node pins on the inlet "
        "(removes Tx/Ty/Rz without radial constraint)",
    )
    p_deck.add_argument(
        "--pressure",
        action="append",
        default=[],
        metavar="NAME,value",
        help="*DSLOAD pressure on a boundary surface (repeatable), "
        "e.g. --pressure inner,1.5",
    )
    p_deck.add_argument(
        "--cload",
        action="append",
        default=[],
        metavar="NAME,dof,value",
        help="*CLOAD on a boundary group (repeatable)",
    )
    p_deck.add_argument(
        "--expansion", type=float, default=None, metavar="ALPHA",
        help="thermal-expansion coefficient -> *EXPANSION (static thermal "
        "stress; needs --temperature or --temperature-from + --t-ref, V45)",
    )
    p_deck.add_argument(
        "--temperature", type=float, default=None, metavar="VALUE",
        help="uniform *TEMPERATURE soak on all nodes (static; needs "
        "--expansion + --t-ref)",
    )
    p_deck.add_argument(
        "--temperature-from", default=None, metavar="HEAT.frd",
        help="solved heat .frd whose NDTEMP field becomes the *TEMPERATURE "
        "load — chains a heat solve into thermal stress (static; needs "
        "--expansion + --t-ref)",
    )
    p_deck.add_argument(
        "--temperature-time", type=float, default=None, metavar="TIME",
        help="snapshot time to take from the --temperature-from frd's "
        "transient history (default = last increment; must match one "
        "of the frd's own recorded times exactly — no interpolation)",
    )
    p_deck.add_argument(
        "--t-ref", type=float, default=None, metavar="VALUE",
        help="stress-free reference temperature -> *INITIAL CONDITIONS "
        "(REQUIRED with a thermal load — never guessed: ccx's expansion "
        "reference is the initial temperature only)",
    )
    p_deck.add_argument(
        "--target", default="abaqus",
        choices=("abaqus", "ccx", "nastran", "lsdyna", "ansys"),
    )
    p_deck.add_argument(
        "--formulation",
        default=None,
        choices=("full", "reduced", "hybrid"),
        help="element formulation (default: hybrid for abaqus, full for ccx)",
    )
    p_deck.add_argument("--nlgeom", action="store_true")
    p_deck.add_argument(
        "--riks", action="store_true",
        help="*STATIC, RIKS arc-length continuation for post-buckling "
        "(static step, needs --nlgeom; the true load is the reaction RF, "
        "not the frd time — auto-requested, V42)",
    )
    p_deck.add_argument(
        "--riks-max-lpf", type=float, default=None,
        help="RIKS load-proportionality-factor cap = data-line field 2 "
        "(default 1.0)",
    )
    p_deck.add_argument(
        "--riks-dl-init", type=float, default=None,
        help="RIKS initial arc-length increment (default 0.05)",
    )
    p_deck.add_argument(
        "--riks-dl-min", type=float, default=None,
        help="RIKS minimum arc-length increment (default 1e-5)",
    )
    p_deck.add_argument(
        "--riks-dl-max", type=float, default=None,
        help="RIKS maximum arc-length increment (default 0.5)",
    )
    p_deck.add_argument("--out", type=_path_arg, default=None, help="output .inp path")
    p_deck.add_argument("--outputs", type=_path_arg, default=Path("outputs"))

    p_blocks = sub.add_parser(
        "blocks",
        help="Multi-block hex mesh from a rectilinear CAD profile "
        "(import-cad JSON): block decomposition, structured quads, "
        "extrusion with named face groups.",
    )
    p_blocks.add_argument(
        "profile", type=_path_arg, help="profile JSON written by import-cad"
    )
    p_blocks.add_argument(
        "--outer",
        type=int,
        default=0,
        help="component index whose outer loop bounds the material (default 0)",
    )
    p_blocks.add_argument(
        "--component-holes",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="include the selected component's own holes (default: yes)",
    )
    p_blocks.add_argument(
        "--holes-like",
        default=None,
        metavar="WxH",
        help="also punch holes at every component whose outer bbox is "
        "W x H (e.g. 177.03x170.68 for the cottage panes)",
    )
    p_blocks.add_argument(
        "--holes-tol",
        type=float,
        default=0.5,
        help="bbox match tolerance for --holes-like (default 0.5)",
    )
    p_blocks.add_argument(
        "--project",
        action="store_true",
        help="allow curved/skewed loops: mesh their bounding-box proxies, "
        "then project rim nodes onto the true curves (projection method)",
    )
    p_blocks.add_argument(
        "--ogrid",
        action="store_true",
        help="O-grid (butterfly) disk layout: central square block + 4 ring "
        "blocks with rims exactly on the curve — one star-shaped loop, no "
        "holes; lifts the projected-disk quality floor",
    )
    p_blocks.add_argument(
        "--core-fraction",
        type=float,
        default=0.5,
        help="O-grid core square size as a fraction of the centroid-to-rim "
        "distance (default 0.5)",
    )
    p_blocks.add_argument(
        "--smooth",
        action="store_true",
        help="elliptic (Winslow) smoothing pass over block interiors before "
        "extrusion (boundaries and rims stay fixed)",
    )
    p_blocks.add_argument(
        "--grade-ratio",
        type=float,
        default=1.0,
        help="transition grading: max/min spacing within each grid interval, "
        "fine cells at the coarse grid lines (default 1 = uniform)",
    )
    p_blocks.add_argument("--target-size", type=float, default=None,
                          help="max element edge length (default: bbox diag / 60)")
    p_blocks.add_argument("--thickness", type=float, default=1.0)
    p_blocks.add_argument("--n-layers", type=int, default=1)
    p_blocks.add_argument("--out", type=_path_arg, default=None, help="output .vtu path")
    p_blocks.add_argument("--outputs", type=_path_arg, default=Path("outputs"))

    p_run = sub.add_parser(
        "run",
        help="Run a YAML recipe: pipeline steps + parameter sweeps; each "
        "variant lands in outputs/<recipe>/<slug>/ with a manifest.json.",
    )
    p_run.add_argument("recipe", type=_path_arg, help="recipe .yaml file")
    p_run.add_argument(
        "--set",
        dest="overrides",
        action="append",
        default=[],
        metavar="STEP.PARAM=VALUE",
        help="override a step parameter in every variant (repeatable; "
        "applied after the sweep, so it wins)",
    )
    p_run.add_argument("--outputs", type=_path_arg, default=Path("outputs"))

    p_replay = sub.add_parser(
        "replay",
        help="Re-run a saved .mfwork session work file, optionally overriding "
        "step parameters; only steps downstream of an override recompute.",
    )
    p_replay.add_argument("workfile", type=_path_arg, help=".mfwork file (see save-workfile)")
    p_replay.add_argument(
        "--set",
        dest="overrides",
        action="append",
        default=[],
        metavar="STEP.KNOB=VALUE",
        help="override a recorded step parameter (repeatable)",
    )
    p_replay.add_argument(
        "--save-workfile",
        type=_path_arg,
        default=None,
        help="write the updated journal back to this .mfwork path",
    )

    p_bl = sub.add_parser(
        "blayers",
        help="External-flow boundary layers: grow geometric wedge "
        "layers outward from a closed triangulated body surface "
        "(CFD pre-processing; exits 1 on inverted cells).",
    )
    p_bl.add_argument("surface", type=_path_arg, help="closed body surface")
    p_bl.add_argument(
        "--first", type=float, required=True, help="first layer thickness"
    )
    p_bl.add_argument("--n-layers", type=int, default=5)
    p_bl.add_argument("--growth", type=float, default=1.2)
    p_bl.add_argument("--normal-smoothing", type=int, default=10)
    p_bl.add_argument("--out", type=_path_arg, default=None, help="output .vtu path")
    p_bl.add_argument("--outputs", type=_path_arg, default=Path("outputs"))

    p_img = sub.add_parser(
        "import-image",
        help="Volume ingest: .npy / .nii(.gz) / DICOM directory -> "
        "marching-cubes iso-surface (.vtp) + provenance JSON; exits 1 "
        "when the surface is not mesh_ready.",
    )
    p_img.add_argument("volume", type=_path_arg, help="volume file or DICOM dir")
    p_img.add_argument(
        "--level", type=float, required=True,
        help="iso level (segmentation threshold, e.g. 300 for CT bone HU)",
    )
    p_img.add_argument(
        "--spacing", default=None, metavar="X,Y,Z",
        help="voxel spacing override (for .npy inputs)",
    )
    p_img.add_argument("--out", type=_path_arg, default=None, help="output .vtp path")
    p_img.add_argument("--outputs", type=_path_arg, default=Path("outputs"))

    p_seg = sub.add_parser(
        "segment",
        help="AI segmentation: CT (.nrrd/.nii/.nii.gz) -> TotalSegmentator "
        "aorta mask -> watertight surface (.vtp, RAS millimetres) + mask, "
        "TotalSegmentator log and summary JSON beside it; with "
        "--reference-zenodo, Dice / volume / surface distance against that "
        "case's reference segmentation. Exits 1 when the surface is not "
        "mesh_ready.",
    )
    p_seg.add_argument("ct", type=_path_arg, help="CT volume (.nrrd, .nii, .nii.gz)")
    p_seg.add_argument(
        "--out", type=_path_arg, default=None,
        help="surface .vtp path (default: <outputs>/segment/<ct stem>_aorta.vtp)",
    )
    p_seg.add_argument(
        "--fast", action="store_true",
        help="TotalSegmentator's faster 3 mm model",
    )
    p_seg.add_argument(
        "--device", choices=("mps", "cpu"), default=None,
        help="TotalSegmentator device (default: mps)",
    )
    p_seg.add_argument(
        "--smoothing", type=float, default=1.0,
        help="Gaussian blur of the mask in voxels before the surface (0 = off)",
    )
    p_seg.add_argument(
        "--timeout", type=float, default=1800.0,
        help="TotalSegmentator wall-clock limit in seconds",
    )
    p_seg.add_argument(
        "--lumen-out", type=_path_arg, default=None, metavar="PATH",
        help="also write the contrast blood lumen (.vtp): CT >= --lumen-hu inside the AI "
        "aorta mask, largest piece — a CT threshold, not AI",
    )
    p_seg.add_argument(
        "--lumen-hu", type=float, default=150.0,
        help="HU threshold for --lumen-out (default 150)",
    )
    p_seg.add_argument(
        "--reference-zenodo", type=_path_arg, default=None, metavar="CASE_DIR",
        help="a Zenodo AAA case folder (e.g. .../Dataset_root/P01): compare "
        "with its reference segmentation (DOI 10.5281/zenodo.19182977)",
    )
    p_seg.add_argument("--outputs", type=_path_arg, default=Path("outputs"))

    p_crop = sub.add_parser(
        "crop",
        help="Crop an aorta surface (mm, +z superior, e.g. from 'segment') to its "
        "infrarenal aneurysm: landmarks from the centreline diameter profile (sac, "
        "neck, bifurcation flare), two cut planes square to the centreline, exactly "
        "flat caps; --lumen cuts a second surface with the same planes. Writes the "
        "cropped .vtp and a <out>_crop.json landmark report. Exits 1 on a refusal "
        "(no sac / no neck).",
    )
    p_crop.add_argument("surface", type=_path_arg, help="closed aorta surface (.vtp/.stl/...)")
    p_crop.add_argument("--out", type=_path_arg, default=None,
                        help="cropped surface .vtp (default: <outputs>/crop/<stem>_crop.vtp)")
    p_crop.add_argument("--lumen", type=_path_arg, default=None,
                        help="second closed surface (e.g. the blood lumen) cut by the same planes")
    p_crop.add_argument("--lumen-out", type=_path_arg, default=None,
                        help="where the cut --lumen is written (default: <out stem>_lumen.vtp)")
    p_crop.add_argument("--neck-extension", type=float, default=25.0,
                        help="proximal cut, mm above the neck start (default 25)")
    p_crop.add_argument("--distal-margin", type=float, default=10.0,
                        help="distal cut, mm above the bifurcation flare top (default 10)")
    p_crop.add_argument("--distal-height", type=float, default=None,
                        help="manual distal cut, mm above the caudal end (with --proximal-height)")
    p_crop.add_argument("--proximal-height", type=float, default=None,
                        help="manual proximal cut, mm above the caudal end")
    p_crop.add_argument("--planes-from", type=_path_arg, default=None, metavar="CROP_JSON",
                        help="reuse the two planes of an earlier crop's JSON report (no "
                        "landmark rules) — e.g. to cut a later outer-sac surface identically")
    p_crop.add_argument("--outputs", type=_path_arg, default=Path("outputs"))

    p_sac = sub.add_parser(
        "sac",
        help="Outer aneurysm sac from a contrast CT and a lumen-only mask: axial "
        "rays from the lumen to the first fat plane -> lumen + thrombus + wall "
        "surface (.vtp, RAS millimetres, point fields ilt_thickness_mm and "
        "sac_resolved) + sac mask (<out>_mask.nii.gz) + summary JSON (<out>.json); with "
        "--reference-zenodo, Dice / volume / surface distance against that "
        "case's reference segmentation. Exits 1 when too few rays find a fat "
        "plane or the surface is not mesh_ready.",
    )
    p_sac.add_argument("ct", type=_path_arg, help="CT volume in HU (.nrrd, .nii, .nii.gz)")
    p_sac.add_argument(
        "--lumen-mask", type=_path_arg, required=True,
        help="lumen mask on the CT grid (.nii, .nii.gz, .nrrd), e.g. a TotalSegmentator aorta mask",
    )
    p_sac.add_argument(
        "--out", type=_path_arg, default=None,
        help="surface .vtp path (default: <outputs>/sac/<ct stem>_sac.vtp)",
    )
    p_sac.add_argument("--lumen-hu", type=float, default=150.0,
                       help="contrast threshold: mask voxels at or above it are the lumen")
    p_sac.add_argument("--fat-hu", type=float, default=-30.0,
                       help="fat / air below this HU marks the outer sac edge")
    p_sac.add_argument("--max-thickness", type=float, default=40.0,
                       help="longest thrombus + wall searched beyond the lumen (mm)")
    p_sac.add_argument("--n-rays", type=int, default=72, help="rays per axial slice")
    p_sac.add_argument("--roi-mm", type=float, default=150.0,
                       help="height above the lumen's lower end to process (mm; 0 = whole mask)")
    p_sac.add_argument("--min-resolved", type=float, default=0.5,
                       help="refuse when fewer rays than this fraction find a fat plane")
    p_sac.add_argument("--smoothing", type=float, default=1.0,
                       help="Gaussian blur of the sac mask in voxels before the surface (0 = off)")
    p_sac.add_argument(
        "--reference-zenodo", type=_path_arg, default=None, metavar="CASE_DIR",
        help="a Zenodo AAA case folder (e.g. .../Dataset_root/P01): compare "
        "with its reference segmentation (DOI 10.5281/zenodo.19182977)",
    )
    p_sac.add_argument("--outputs", type=_path_arg, default=Path("outputs"))

    p_tix = sub.add_parser(
        "tissue-index",
        help="Rupture-potential index band: peak wall stress / lab-measured wall "
        "strength (tissue library v0: open USP AAA strip tests, CC BY 4.0), "
        "P10/P50/P90 with n shown. Investigational index, not a diagnosis.",
    )
    pws_src = p_tix.add_mutually_exclusive_group(required=True)
    pws_src.add_argument("--pws", type=float, metavar="MPA",
                         help="peak wall stress in MPa (Cauchy, from the wall-stress solve)")
    pws_src.add_argument("--from-summary", type=_path_arg, metavar="JSON",
                         help="wall-stress summary JSON holding a pws_mpa value (read only)")
    p_tix.add_argument("--selection", choices=("aaa", "control", "all"), default="aaa",
                       help="strength strips: aaa = rupture-border aneurysm wall (default), "
                       "control = non-dilated aorta of the same donors, all = both")
    p_tix.add_argument("--method", choices=("empirical", "lognormal"), default="empirical",
                       help="band: empirical strip quantiles (default) or a lognormal fit")
    p_tix.add_argument("--out", type=_path_arg, default=None,
                       help="also write the band, sensitivities and provenance as JSON here")

    p_cso = sub.add_parser(
        "import-cso",
        help="MeVisLab Binary .cso contour ingest: loft (or voxelise) the "
        "closed cross-section contours into a watertight tube surface (.vtp) + "
        "provenance JSON; exits 1 when the surface is not mesh_ready.",
    )
    p_cso.add_argument("file", type=_path_arg, help="MeVisLab Binary .cso file")
    p_cso.add_argument(
        "--method", choices=("auto", "loft", "voxel"), default="auto",
        help="auto (default, V40: loft else voxel — whichever sweeps to a clean "
        "volmesh) | loft (skin rings into a tube) | voxel (label volume + "
        "marching cubes)",
    )
    p_cso.add_argument(
        "--n-ring", type=int, default=48, help="resample count per ring (loft)",
    )
    p_cso.add_argument(
        "--voxel-size", type=float, default=None,
        help="voxel size for the label volume (voxel method)",
    )
    p_cso.add_argument(
        "--no-cap", action="store_true",
        help="do not cap the end rings (leaves an open surface)",
    )
    p_cso.add_argument(
        "--on-open", choices=("skip", "close", "error"), default="skip",
        help="open editor contour policy: skip (default: drop it with a "
        "loud note) | close (only if near-closed) | error (refuse)",
    )
    p_cso.add_argument(
        "--dicom", type=_path_arg, default=None,
        help="anchor to a DICOM series/.dcm: voxelise the contours onto "
        "its image grid so the mask aligns with the scan (read-only, forces voxel)",
    )
    p_cso.add_argument("--out", type=_path_arg, default=None, help="output .vtp path")
    p_cso.add_argument("--outputs", type=_path_arg, default=Path("outputs"))

    p_cad3d = sub.add_parser(
        "import-cad3d",
        help="Import a STEP/IGES/BREP solid via gmsh OCC, surface-mesh it, "
        "and write the triangulated surface (.vtp) + provenance JSON; "
        "exits 1 when the result is not mesh_ready.",
    )
    p_cad3d.add_argument("file", type=_path_arg, help="STEP/IGES/BREP file")
    p_cad3d.add_argument("--mesh-size", type=float, default=None)
    p_cad3d.add_argument(
        "--mesh-size-factor",
        type=float,
        default=None,
        help="scale the default size (bbox diagonal / 50); "
        "mutually exclusive with --mesh-size",
    )
    p_cad3d.add_argument("--out", type=_path_arg, default=None, help="output .vtp path")
    p_cad3d.add_argument("--outputs", type=_path_arg, default=Path("outputs"))

    p_foam = sub.add_parser(
        "foam",
        help="OpenFOAM polyMesh export: mesh a wall from a surface (like "
        "volmesh) and write constant/polyMesh with named boundary patches.",
    )
    p_foam.add_argument("surface", type=_path_arg, help="inner surface file")
    p_foam.add_argument("--thickness", type=float, default=None)
    p_foam.add_argument("--outer-surface", type=_path_arg, default=None)
    p_foam.add_argument("--backend", default="swept", help="swept (default) | tet")
    p_foam.add_argument("--n-theta", type=int, default=32)
    p_foam.add_argument("--n-axial", type=int, default=60)
    p_foam.add_argument("--n-layers", type=int, default=4)
    p_foam.add_argument("--layer-ratio", type=float, default=1.0)
    p_foam.add_argument("--mesh-size", type=float, default=None)
    p_foam.add_argument(
        "--patch-type",
        action="append",
        default=[],
        metavar="NAME,TYPE",
        help="override a patch type (repeatable), e.g. --patch-type inner,wall; "
        "default: inlet/outlet -> patch, everything else -> wall",
    )
    p_foam.add_argument(
        "--default-patch",
        default=None,
        metavar="NAME",
        help="collect boundary faces not covered by any group into this patch "
        "(without it, uncovered faces are a hard error)",
    )
    p_foam.add_argument("--case", type=_path_arg, default=None, help="case directory")
    p_foam.add_argument("--outputs", type=_path_arg, default=Path("outputs"))

    p_fluent = sub.add_parser(
        "fluent",
        help="ANSYS FLUENT .msh export: mesh a wall from a surface (like "
        "volmesh) and write a face-based ASCII mesh with named boundary "
        "zones.",
    )
    p_fluent.add_argument("surface", type=_path_arg, help="inner surface file")
    p_fluent.add_argument("--thickness", type=float, default=None)
    p_fluent.add_argument("--outer-surface", type=_path_arg, default=None)
    p_fluent.add_argument("--backend", default="swept", help="swept (default) | tet")
    p_fluent.add_argument("--n-theta", type=int, default=32)
    p_fluent.add_argument("--n-axial", type=int, default=60)
    p_fluent.add_argument("--n-layers", type=int, default=4)
    p_fluent.add_argument("--layer-ratio", type=float, default=1.0)
    p_fluent.add_argument("--mesh-size", type=float, default=None)
    p_fluent.add_argument(
        "--bc-type",
        action="append",
        default=[],
        metavar="NAME,TYPE",
        help="override a boundary zone type (repeatable), e.g. "
        "--bc-type inner,symmetry; types: wall, velocity-inlet, "
        "pressure-inlet, pressure-outlet, symmetry, outflow; default: "
        "inlet/outlet names -> velocity-inlet/pressure-outlet, else wall",
    )
    p_fluent.add_argument(
        "--default-patch",
        default=None,
        metavar="NAME",
        help="collect boundary faces not covered by any group into this zone "
        "(without it, uncovered faces are a hard error)",
    )
    p_fluent.add_argument("--out", type=_path_arg, default=None, help="output .msh path")
    p_fluent.add_argument("--outputs", type=_path_arg, default=Path("outputs"))

    p_solve = sub.add_parser(
        "solve",
        help="Run a solver deck and read the results back: .inp goes to "
        "CalculiX (.dat/.frd), .bdf to MYSTRAN (F06, V25); both persist a "
        "results .vtu with displacement point data and a summary JSON "
        "under outputs/solve/<deck>/. .k and .cdb decks refuse loudly — "
        "no LS-DYNA/ANSYS solver is wired.",
    )
    p_solve.add_argument(
        "deck", type=_path_arg, help="runnable .inp or .bdf (see the deck command)"
    )
    p_solve.add_argument(
        "--ccx",
        default=None,
        help="CalculiX executable (default: MESHFORGE_CCX, then PATH, then "
        "the ccxsolve conda env)",
    )
    p_solve.add_argument(
        "--mystran",
        default=None,
        help="MYSTRAN executable for .bdf decks (default: MESHFORGE_MYSTRAN, "
        "then PATH, then the mystran conda env)",
    )
    p_solve.add_argument("--timeout", type=float, default=600.0)
    p_solve.add_argument("--outputs", type=_path_arg, default=Path("outputs"))

    p_cop = sub.add_parser(
        "copilot",
        help="Claude copilot (investigational): a plain-language request runs segment -> sac "
        "-> crop -> wall mesh (quality gate) -> wall stress -> strength index, recovering from a "
        "failed gate; writes a deterministic report with the twin card, the transcript and a "
        "replayable work file. Needs the Anthropic SDK and an API key in the environment.",
    )
    p_cop.add_argument("request", help="what to do, in plain language")
    p_cop.add_argument("--ct", type=_path_arg, default=None,
                       help="the CT file (.nii.gz/.nrrd); appended to the request")
    p_cop.add_argument("--credit", default="public CC-BY scan",
                       help="the scan's source and licence, printed in the report")
    p_cop.add_argument("--server", default=None,
                       help="run the steps on a live Workbench (e.g. http://127.0.0.1:8765) so "
                       "its viewport follows along; default: in-process")
    p_cop.add_argument("--model", default="claude-opus-5-5")
    p_cop.add_argument("--effort", default="high", choices=("low", "medium", "high", "xhigh", "max"))
    p_cop.add_argument("--max-turns", type=int, default=24)
    p_cop.add_argument("--flow-metrics", type=_path_arg, default=None,
                       help="a pre-computed CFD metrics JSON (numbers only) to fill the twin "
                       "card's flow slot; journaled in the session, so the Workbench panel shows "
                       "it too")
    p_cop.add_argument("--out-dir", type=_path_arg, default=None,
                       help="run folder (default: <outputs>/copilot/<timestamp>)")
    p_cop.add_argument("--outputs", type=_path_arg, default=Path("outputs"))
    # D112: one command runs the whole demo from a terminal
    p_cop.add_argument("--launch", dest="launch", action="store_true", default=True,
                       help="when nothing answers at --server, start a Workbench there and open "
                       "its window in Demo mode (the default)")
    p_cop.add_argument("--no-launch", dest="launch", action="store_false",
                       help="never start a Workbench or open a window")
    p_cop.add_argument("--reset", action="store_true",
                       help="empty a Workbench that is already running at --server before the "
                       "run (a fresh take from grey tabs)")
    p_cop.add_argument("--flow-live", action="store_true",
                       help="after the strength index, build an OpenFOAM case from THIS crop's "
                       "blood lumen (the crop step's lumen, tet-filled; 1 L/min at the neck, "
                       "blood viscosity) and let the session's cfd step solve it (about 20 s), "
                       "so the flow sits on the same crop as the wall stress")
    p_cop.add_argument("--flow-case", type=_path_arg, default=None,
                       help="a complete OpenFOAM case of this scan's blood lumen (system/, "
                       "constant/polyMesh, 0/); the session re-solves a copy of it (the cfd "
                       "step) after the strength index — with --flow-live, only when the live "
                       "build fails")
    p_cop.add_argument("--open", dest="open_report", action="store_true", default=None,
                       help="open report.html when the run ends (default: when stdout is a "
                       "terminal)")
    p_cop.add_argument("--no-open", dest="open_report", action="store_false",
                       help="leave the report closed")

    p_aaa = sub.add_parser(
        "aaa-stress",
        help="AAA wall stress under blood pressure (INVESTIGATIONAL): stiff-linear "
        "CalculiX solves on the loaded geometry of a swept hex wall (pressure on the "
        "lumen side, both ends clamped) at a systolic and a what-if pressure; peak "
        "wall stress (99th percentile von Mises outside the end zones), max "
        "principal, wall tension. Refuses (exit 1) a wall with inverted cells, "
        "capped/floored wall rays or no mesh summary. Writes a results .vtu and a "
        "summary JSON.",
    )
    p_aaa.add_argument(
        "wall", type=_path_arg,
        help="swept wall .vtu from 'volmesh' (its <stem>.summary.json beside it), or a "
        ".mfwork session whose replay ends with that wall",
    )
    p_aaa.add_argument("--summary", type=_path_arg, default=None,
                       help="the wall's volmesh summary JSON (default: <wall stem>.summary.json)")
    p_aaa.add_argument("--crop-report", type=_path_arg, default=None,
                       help="crop report JSON (the neck diameter for the end zones, and "
                       "which cap is distal)")
    p_aaa.add_argument("--systolic", type=float, default=140.0,
                       help="systolic pressure, mmHg (default 140)")
    p_aaa.add_argument("--whatif", type=float, default=120.0,
                       help="what-if pressure, mmHg (default 120)")
    p_aaa.add_argument("--E", type=float, default=100.0,
                       help="wall Young's modulus, MPa (default 100)")
    p_aaa.add_argument("--nu", type=float, default=0.49,
                       help="wall Poisson's ratio (default 0.49)")
    p_aaa.add_argument(
        "--end-zone", default="neck",
        help="excluded next to each clamped end: neck (default: one neck diameter "
        "from the crop report, else each end's own diameter) | bending (3 shell "
        "bending lengths) | a length in mm",
    )
    p_aaa.add_argument(
        "--lumen", type=_path_arg, default=None,
        help="the blood-lumen surface cut with the same two planes (e.g. crop --lumen-out): "
        "models the intraluminal thrombus between it and the wall as a tied solid; needs "
        "--crop-report",
    )
    p_aaa.add_argument(
        "--sac", type=_path_arg, default=None,
        help="the surface the wall was built from (the cropped outer sac): the thrombus "
        "fills the space between it and --lumen; required with --lumen",
    )
    p_aaa.add_argument("--E-ilt", type=float, default=5.0,
                       help="thrombus Young's modulus, MPa (default 5, as in the Zenodo AAA "
                       "FE model)")
    p_aaa.add_argument("--ilt-size", type=float, default=3.0,
                       help="thrombus tet size, mm (default 3)")
    p_aaa.add_argument("--ccx", default=None, help="CalculiX executable")
    p_aaa.add_argument("--timeout", type=float, default=600.0,
                       help="solver limit per solve, seconds")
    p_aaa.add_argument("--out-dir", type=_path_arg, default=None,
                       help="results folder (default: <outputs>/aaa_stress/<wall stem>)")
    p_aaa.add_argument("--outputs", type=_path_arg, default=Path("outputs"))

    p_cfd = sub.add_parser(
        "cfd",
        help="Run OpenFOAM on a case directory and read U/p back: with "
        "--inlet/--outlet the dir needs only constant/polyMesh (see the "
        "foam command) and the solver files are written from the flags; "
        "without them the case must already be complete. Persists a "
        "results .vtu + summary JSON under outputs/cfd/<case>/.",
    )
    p_cfd.add_argument("case", type=_path_arg, help="OpenFOAM case directory")
    p_cfd.add_argument(
        "--solver", default="simpleFoam", help="simpleFoam (steady) | icoFoam (transient)"
    )
    p_cfd.add_argument("--nu", type=float, default=1e-3, help="kinematic viscosity")
    p_cfd.add_argument(
        "--inlet",
        default=None,
        metavar="NAME=VX,VY,VZ",
        help="velocity inlet patch, e.g. --inlet inlet=0,0,1",
    )
    p_cfd.add_argument(
        "--outlet",
        default=None,
        metavar="NAME=P",
        help="pressure outlet patch (kinematic p), e.g. --outlet outlet=0",
    )
    p_cfd.add_argument("--end-time", type=float, default=2000.0)
    p_cfd.add_argument(
        "--delta-t", type=float, default=None, help="time step (icoFoam only)"
    )
    p_cfd.add_argument("--write-interval", type=float, default=None)
    p_cfd.add_argument("--timeout", type=float, default=600.0)
    p_cfd.add_argument(
        "--turbulence",
        default="laminar",
        choices=("laminar", "kOmegaSST"),
        help="turbulence model for built cases (steady kOmegaSST RANS, V27)",
    )
    p_cfd.add_argument(
        "--intensity",
        type=float,
        default=0.05,
        help="inlet turbulence intensity I (kOmegaSST; default 0.05)",
    )
    p_cfd.add_argument(
        "--turb-length",
        type=float,
        default=None,
        help="inlet mixing length, e.g. 0.07*D for a pipe (REQUIRED with "
        "--turbulence kOmegaSST)",
    )
    p_cfd.add_argument(
        "--moving-wall",
        default=None,
        metavar="NAME=VX,VY,VZ",
        help="tangentially moving wall patch (closed cavities), "
        "e.g. --moving-wall lid=1,0,0",
    )
    p_cfd.add_argument(
        "--empty",
        default=None,
        metavar="NAMES",
        help="comma-separated patches to treat as the OpenFOAM 2-D "
        "'empty' direction, e.g. --empty front,back",
    )
    p_cfd.add_argument(
        "--closed-cavity",
        action="store_true",
        help="sealed domain: no pressure BC anywhere, pressure level "
        "pinned by pRefCell/pRefValue",
    )
    p_cfd.add_argument(
        "--p-ref-cell", type=int, default=0,
        help="reference cell index (--closed-cavity; default 0)",
    )
    p_cfd.add_argument(
        "--p-ref-value", type=float, default=0.0,
        help="reference kinematic pressure (--closed-cavity; default 0)",
    )
    p_cfd.add_argument(
        "--checkmesh",
        action="store_true",
        help="run checkMesh only (no solve); loud unless the mesh is OK",
    )
    p_cfd.add_argument(
        "--series",
        action="store_true",
        help="read ALL written time steps back and persist one .vtu per "
        "frame (combine with --write-interval below --end-time)",
    )
    p_cfd.add_argument("--outputs", type=_path_arg, default=Path("outputs"))

    p_web = sub.add_parser(
        "web",
        help="Serve the local web app (FastAPI over MeshingSession) on "
        "127.0.0.1:<free port> and print the URL. Deps: the 'web' "
        "extra (pip install .[web]).",
    )
    p_web.add_argument(
        "--port",
        type=int,
        default=None,
        help="port to bind (default: an OS-assigned free port)",
    )

    p_desktop = sub.add_parser(
        "desktop",
        help="Open the Workbench in a native window (QtWebEngine over the "
        "local 127.0.0.1 web server; V35b). Deps: the 'desktop' extra "
        "(conda-forge qt6-webengine, plus the 'web'/'viewport' extras).",
    )
    p_desktop.add_argument(
        "--port",
        type=int,
        default=None,
        help="port for the child web server (default: an OS-assigned free port)",
    )

    args = parser.parse_args(argv)
    if args.command == "web":
        return _cmd_web(args)
    if args.command == "desktop":
        return _cmd_desktop(args)
    if args.command == "cfd":
        return _cmd_cfd(args)
    if args.command == "blayers":
        return _cmd_blayers(args)
    if args.command == "import-image":
        return _cmd_import_image(args)
    if args.command == "import-cso":
        return _cmd_import_cso(args)
    if args.command == "segment":
        return _cmd_segment(args)
    if args.command == "crop":
        return _cmd_crop(args)
    if args.command == "sac":
        return _cmd_sac(args)
    if args.command == "tissue-index":
        return _cmd_tissue_index(args)
    if args.command == "import-cad3d":
        return _cmd_import_cad3d(args)
    if args.command == "foam":
        return _cmd_foam(args)
    if args.command == "fluent":
        return _cmd_fluent(args)
    if args.command == "solve":
        return _cmd_solve(args)
    if args.command == "copilot":
        return _cmd_copilot(args)
    if args.command == "aaa-stress":
        return _cmd_aaa_stress(args)
    if args.command == "replay":
        return _cmd_replay(args)
    if args.command == "repair":
        return _cmd_repair(args)
    if args.command == "run":
        return _cmd_run(args)
    if args.command == "blocks":
        return _cmd_blocks(args)
    if args.command == "deck":
        return _cmd_deck(args)
    if args.command == "import-cad":
        return _cmd_import_cad(args)
    if args.command == "volmesh":
        return _cmd_volmesh(args)
    if args.command == "lumen":
        return _cmd_lumen(args)
    if args.command == "inspect":
        return _cmd_inspect(args)
    if args.command == "export":
        return _cmd_export(args)
    if args.command == "check":
        return _cmd_check(args)
    if args.command == "synth":
        return _cmd_synth(args)
    parser.error(f"unknown command: {args.command}")
    return 2


def _cmd_web(args: argparse.Namespace) -> int:
    # Lazy: the web stack (FastAPI/uvicorn) is imported only for this command,
    # so importing meshforge.cli never pulls it (rule 5 / D50).
    from meshforge.web.server import serve

    serve(port=args.port)
    return 0


def _cmd_desktop(args: argparse.Namespace) -> int:
    # Lazy: the shell pulls PySide6/QtWebEngine only for this command, so
    # importing meshforge.cli never pulls Qt (rule 5 / D50; V35b).
    from meshforge.gui.shell import run

    return run(port=args.port)


def _cmd_inspect(args: argparse.Namespace) -> int:
    # Lazy imports: subcommands must not pull deps they don't use.
    from meshforge.io import file_sha256, load_surface
    from meshforge.quality import inspect_surface

    surface: Path = args.surface
    try:
        checksum_before = file_sha256(surface)
        mesh = load_surface(surface)
        report = inspect_surface(mesh, path=str(surface), sha256=checksum_before)
    except (FileNotFoundError, TypeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if file_sha256(surface) != checksum_before:
        print(f"error: input file changed on disk during inspection: {surface}", file=sys.stderr)
        return 1

    outputs: Path = args.outputs
    outputs.mkdir(parents=True, exist_ok=True)
    json_path = outputs / f"{surface.stem}_inspect.json"
    txt_path = outputs / f"{surface.stem}_inspect.txt"
    json_path.write_text(report.to_json() + "\n")
    txt_path.write_text(report.to_text())

    print(report.to_text(), end="")
    print(f"report      : {json_path}")
    print(f"report      : {txt_path}")
    return 0


def _cmd_export(args: argparse.Namespace) -> int:
    from meshforge.export import DEFAULT_FORMAT, SUPPORTED_FORMATS, export_surface
    from meshforge.io import file_sha256, load_surface

    surface: Path = args.surface
    out: Path | None = args.out
    if out is None:
        fmt = (args.fmt or DEFAULT_FORMAT).lower()
        suffix = SUPPORTED_FORMATS.get(fmt, f".{fmt}")
        out = args.outputs / f"{surface.stem}{suffix}"

    try:
        checksum_before = file_sha256(surface)
        mesh = load_surface(surface)
        written = export_surface(mesh, out, fmt=args.fmt, input_path=surface)
    except (FileNotFoundError, TypeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if file_sha256(surface) != checksum_before:
        print(f"error: input file changed on disk during export: {surface}", file=sys.stderr)
        return 1

    print(f"exported    : {surface} -> {written}")
    return 0


def _cmd_check(args: argparse.Namespace) -> int:
    from meshforge.io import file_sha256, load_surface
    from meshforge.quality import check_surface

    surface: Path = args.surface
    try:
        checksum_before = file_sha256(surface)
        mesh = load_surface(surface)
        report = check_surface(mesh, path=str(surface), sha256=checksum_before)
    except (FileNotFoundError, TypeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if file_sha256(surface) != checksum_before:
        print(f"error: input file changed on disk during check: {surface}", file=sys.stderr)
        return 1

    outputs: Path = args.outputs
    outputs.mkdir(parents=True, exist_ok=True)
    json_path = outputs / f"{surface.stem}_check.json"
    txt_path = outputs / f"{surface.stem}_check.txt"
    json_path.write_text(report.to_json() + "\n")
    txt_path.write_text(report.to_text())

    print(report.to_text(), end="")
    print(f"report             : {json_path}")
    print(f"report             : {txt_path}")
    return 0


def _parse_param(item: str) -> tuple[str, float | int]:
    """Parse KEY=VALUE into a numeric generator parameter (no eval)."""
    key, sep, raw = item.partition("=")
    if not sep or not key or not raw:
        raise ValueError(f"expected KEY=VALUE, got {item!r}")
    try:
        return key, int(raw)
    except ValueError:
        pass
    try:
        return key, float(raw)
    except ValueError:
        raise ValueError(f"parameter {key!r} must be numeric, got {raw!r}") from None


def _cmd_synth(args: argparse.Namespace) -> int:
    from meshforge.synth import GENERATORS, generate, generate_primitive

    try:
        params = dict(_parse_param(item) for item in args.params)
        from meshforge.synth import TREES, generate_tree

        if args.shape in GENERATORS:
            surface, truth = generate(args.shape, **params)
        elif args.shape in TREES:
            surface, truth = generate_tree(args.shape, **params)
        else:  # primitive registry; unknown names fail loudly listing all
            try:
                surface, truth = generate_primitive(args.shape, **params)
            except ValueError as exc:
                if "unknown primitive" in str(exc):
                    from meshforge.synth import PRIMITIVES

                    raise ValueError(
                        f"unknown shape {args.shape!r}; available: "
                        f"{', '.join(sorted(GENERATORS) + sorted(PRIMITIVES) + sorted(TREES))}"
                    ) from None
                raise
        if args.degrade_noise or args.degrade_holes or args.degrade_dup_faces:
            from meshforge.quality import degrade_surface

            surface = degrade_surface(
                surface,
                noise=args.degrade_noise,
                poke_holes=args.degrade_holes,
                duplicate_faces=args.degrade_dup_faces,
            )
            print(
                f"degraded    : noise={args.degrade_noise} "
                f"holes={args.degrade_holes} dup_faces={args.degrade_dup_faces}"
            )
    except (ValueError, TypeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    outputs: Path = args.outputs
    outputs.mkdir(parents=True, exist_ok=True)
    surface_path = outputs / f"{args.shape}.vtp"
    truth_path = outputs / f"{args.shape}_truth.json"
    surface.save(surface_path)
    truth_path.write_text(truth.to_json() + "\n")

    vol = "n/a" if truth.volume is None else f"{truth.volume:.6g}"
    area = "n/a" if truth.surface_area is None else f"{truth.surface_area:.6g}"
    print(f"shape       : {truth.shape}  ({surface.n_points} pts, {surface.n_cells} tris)")
    if hasattr(truth, "branches"):
        j = truth.junction
        print(
            f"branches    : {truth.n_branches}   junction: "
            f"({j[0]:.6g}, {j[1]:.6g}, {j[2]:.6g})"
        )
    elif hasattr(truth, "length"):
        print(f"length      : {truth.length:.6g}   closed loop: {truth.closed}")
    else:
        print(f"exact truth : {truth.exact}   expected faces: {truth.n_faces}")
    print(f"truth volume: {vol}   truth area: {area}")
    print(f"surface     : {surface_path}")
    print(f"truth       : {truth_path}")
    return 0


def _cmd_import_cad(args: argparse.Namespace) -> int:
    from meshforge.io import file_sha256
    from meshforge.io.cad import list_blocks, load_profile

    src: Path = args.file
    try:
        checksum_before = file_sha256(src)
        if args.list_blocks:
            entries = list_blocks(src)
            print(f"blocks in   : {src}")
            for e in entries:
                types = ", ".join(f"{k} x{v}" for k, v in sorted(e["types"].items()))
                print(
                    f"  {e['name']:<28} {e['n_entities']:>5} entities, "
                    f"{e['n_supported']:>5} supported  [{types}]"
                )
            if file_sha256(src) != checksum_before:
                print(f"error: input file changed on disk: {src}", file=sys.stderr)
                return 1
            return 0
        layers = None
        if args.layers:
            layers = [s.strip() for s in args.layers.split(",") if s.strip()]
        profile = load_profile(
            src,
            block=args.block,
            chain_tol=args.chain_tol,
            arc_tol=args.arc_tol,
            layers=layers,
            mode=args.mode,
        )
    except (FileNotFoundError, TypeError, ValueError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if file_sha256(src) != checksum_before:
        print(f"error: input file changed on disk during import: {src}", file=sys.stderr)
        return 1

    outputs: Path = args.outputs
    outputs.mkdir(parents=True, exist_ok=True)
    json_path = outputs / f"{src.stem}_profile.json"
    txt_path = outputs / f"{src.stem}_profile.txt"
    json_path.write_text(profile.to_json() + "\n")
    txt_path.write_text(profile.to_text())

    print(profile.to_text(), end="")
    print(f"profile     : {json_path}")
    print(f"report      : {txt_path}")
    return 0 if profile.mesh_ready else 1


def _cmd_replay(args: argparse.Namespace) -> int:
    from meshforge.pipeline import load_workfile, replay

    overrides: dict[str, dict] = {}
    try:
        for item in args.overrides:
            key, sep, raw = item.partition("=")
            step, dot, knob = key.partition(".")
            if not sep or not dot:
                raise ValueError(f"--set expects STEP.KNOB=VALUE, got {item!r}")
            _, value = _parse_param(f"{knob}={raw}")
            overrides.setdefault(step, {})[knob] = value
        session = load_workfile(args.workfile)
        results = replay(session, overrides)
    except (FileNotFoundError, TypeError, ValueError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    for result in results:
        print(result.text, end="")
    if args.save_workfile is not None:
        written = session.save_workfile(args.save_workfile)
        print(f"workfile    : {written}")
    print(f"replayed    : {len(results)} step(s) from {args.workfile}")
    return 0


def _cmd_repair(args: argparse.Namespace) -> int:
    from meshforge.export import guard_output_path
    from meshforge.io import file_sha256, load_surface
    from meshforge.quality import repair_surface

    src: Path = args.surface
    out: Path = args.out or (args.outputs / f"{src.stem}_repaired.vtp")
    try:
        checksum_before = file_sha256(src)
        mesh = load_surface(src)
        repaired, report = repair_surface(
            mesh, merge_tol=args.merge_tol, max_hole_edges=args.max_hole_edges
        )
        guard_output_path(out, input_paths=[src])
    except (FileNotFoundError, TypeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if file_sha256(src) != checksum_before:
        print(f"error: input changed on disk during repair: {src}", file=sys.stderr)
        return 1

    out.parent.mkdir(parents=True, exist_ok=True)
    repaired.save(out)
    json_path = args.outputs / f"{src.stem}_repair.json"
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(report.to_json() + "\n")

    print(report.to_text(), end="")
    print(f"repaired    : {out}")
    print(f"report      : {json_path}")
    return 0 if report.mesh_ready else 1


def _cmd_run(args: argparse.Namespace) -> int:
    from meshforge.recipes import run_recipe

    overrides: dict = {}
    try:
        import yaml

        for item in args.overrides:
            key, sep, raw = item.partition("=")
            if not sep or "." not in key:
                raise ValueError(f"--set expects STEP.PARAM=VALUE, got {item!r}")
            overrides[key] = yaml.safe_load(raw)
        manifest = run_recipe(args.recipe, outputs=args.outputs, overrides=overrides)
    except (FileNotFoundError, TypeError, ValueError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    for v in manifest["variants"]:
        mark = "ok    " if v["status"] == "ok" else "FAILED"
        extra = f"  ({v['error']})" if v["error"] else ""
        q = v["quality"]
        qtxt = (
            f"  inverted={q['n_inverted']} sj_min={q['sj_min']:.4g}" if q else ""
        )
        print(f"variant {mark}: {v['slug']}{qtxt}{extra}")
    print(
        f"recipe      : {manifest['recipe']}  "
        f"({manifest['n_ok']} ok, {manifest['n_failed']} failed)"
    )
    print(f"manifest    : {args.outputs / manifest['recipe'] / 'manifest.json'}")
    return 0 if manifest["n_failed"] == 0 else 1


def _cmd_blocks(args: argparse.Namespace) -> int:
    import json

    import numpy as np

    from meshforge.export import guard_output_path
    from meshforge.io import file_sha256
    from meshforge.meshing import build_block_mesh, extrude_blocks

    src: Path = args.profile
    out: Path = args.out or (args.outputs / f"{src.stem}_blocks.vtu")

    try:
        checksum_before = file_sha256(src)
        from meshforge.meshing.blocks import select_loops

        data = json.loads(src.read_text())
        selected, n_matched = select_loops(
            data["loops"],
            data["components"],
            outer=args.outer,
            component_holes=args.component_holes,
            holes_like=args.holes_like,
            holes_tol=args.holes_tol,
        )
        if n_matched:
            print(f"holes-like  : matched {n_matched} component(s)")

        target = args.target_size
        if target is None:
            allpts = np.vstack(selected)
            target = float(np.hypot(*(allpts.max(0) - allpts.min(0)))) / 60.0
        if args.ogrid and args.project:
            raise ValueError("--ogrid and --project are mutually exclusive")
        if args.ogrid and args.grade_ratio != 1.0:
            raise ValueError(
                "--grade-ratio applies to lattice meshes only, not --ogrid"
            )
        if args.ogrid:
            from meshforge.meshing import ogrid_disk

            bm = ogrid_disk(
                selected, target_size=target, core_fraction=args.core_fraction
            )
        elif args.project:
            from meshforge.meshing import project_profile

            bm = project_profile(
                selected, target_size=target, grade_ratio=args.grade_ratio
            )
        else:
            bm = build_block_mesh(
                selected, target_size=target, grade_ratio=args.grade_ratio
            )
        if args.smooth:
            from meshforge.meshing.smooth import smooth_lattice

            bm = smooth_lattice(bm)
            if args.grade_ratio > 1.0:
                bm.notes.append(
                    "WARNING: Winslow smoothing equidistributes — it partially "
                    "undoes the transition grading"
                )
        wall = extrude_blocks(bm, thickness=args.thickness, n_layers=args.n_layers)
        guard_output_path(out, input_paths=[src])
    except (FileNotFoundError, KeyError, TypeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if file_sha256(src) != checksum_before:
        print(f"error: input changed on disk: {src}", file=sys.stderr)
        return 1

    out.parent.mkdir(parents=True, exist_ok=True)
    wall.grid.save(out)
    summary = wall.summary()
    summary_path = out.with_suffix(".summary.json")
    summary_path.write_text(json.dumps(summary, indent=2) + "\n")

    q = summary["quality"]
    print(f"backend     : {summary['backend']}")
    print(f"cells       : {summary['n_cells']}  points: {summary['n_points']}")
    print(f"boundary    : {summary['boundary_faces']}")
    print(
        f"quality     : inverted={q['n_inverted']}  sj_min={q['sj_min']:.4g}  "
        f"volume={q['total_volume']:.6g}"
    )
    for note in summary["notes"]:
        print(f"note        : {note}")
    print(f"mesh        : {out}")
    print(f"summary     : {summary_path}")
    return 0 if q["n_inverted"] == 0 else 1


def _cmd_deck(args: argparse.Namespace) -> int:
    from meshforge.export.deck import (
        BoundaryCondition,
        DeckSpec,
        Load,
        Material,
        PhysicalConstants,
        Step,
        lateral_pins,
        write_deck,
    )
    from meshforge.io import file_sha256, load_surface
    from meshforge.meshing import get_backend

    surface_path: Path = args.surface
    out: Path = args.out or (args.outputs / f"{surface_path.stem}_{args.target}.inp")

    try:
        # Trap #5 precedence: exactly one thickness source, loudly.
        if args.thickness is not None and args.outer_surface is not None:
            raise ValueError(
                "--thickness and --outer-surface are mutually exclusive; "
                "drop --thickness to use the measured outer surface"
            )
        if args.thickness is None and args.outer_surface is None:
            raise ValueError("give either --thickness or --outer-surface")

        if args.material == "elastic":
            if args.E is None or args.nu is None:
                raise ValueError("--material elastic needs --E and --nu")
            material = Material.elastic(
                args.E, args.nu, density=args.density,
                expansion=args.expansion,
            )
        elif args.material == "thermal":
            if args.conductivity_table is not None:
                if args.conductivity is not None:
                    raise ValueError(
                        "give either --conductivity or --conductivity-table, "
                        "not both"
                    )
                rows = []
                for tok in filter(None, (t.strip() for t in
                                         args.conductivity_table.split(","))):
                    k_str, sep, t_str = tok.partition("@")
                    if not sep:
                        raise ValueError(
                            f"--conductivity-table entry {tok!r}: expected k@T"
                        )
                    rows.append((float(k_str), float(t_str)))
                material = Material.thermal(
                    rows, density=args.density, specific_heat=args.specific_heat,
                )
            elif args.conductivity is None:
                raise ValueError(
                    "--material thermal needs --conductivity or "
                    "--conductivity-table"
                )
            else:
                material = Material.thermal(
                    args.conductivity,
                    density=args.density,
                    specific_heat=args.specific_heat,
                )
        elif args.material == "neo_hooke":
            if args.c10 is None or args.d1 is None:
                raise ValueError("--material neo_hooke needs --c10 and --d1")
            material = Material.neo_hookean(args.c10, args.d1, density=args.density)
        else:
            if args.c10 is None or args.c01 is None or args.d1 is None:
                raise ValueError(
                    "--material mooney_rivlin needs --c10, --c01 and --d1"
                )
            material = Material.mooney_rivlin(
                args.c10, args.c01, args.d1, density=args.density
            )
        if args.expansion is not None and args.material != "elastic":
            raise ValueError(
                "--expansion applies to --material elastic only (thermal "
                "stress needs an elastic material)"
            )

        bcs: list[BoundaryCondition] = []
        for item in args.fix:
            parts = [p.strip() for p in item.split(",")]
            if len(parts) not in (2, 3):
                raise ValueError(f"--fix expects NAME,first[,last], got {item!r}")
            bcs.append(
                BoundaryCondition(
                    parts[0],
                    int(parts[1]),
                    int(parts[2]) if len(parts) == 3 else None,
                )
            )
        loads: list[Load] = []
        for item in args.pressure:
            parts = [p.strip() for p in item.split(",")]
            if len(parts) != 2:
                raise ValueError(f"--pressure expects NAME,value, got {item!r}")
            loads.append(Load.pressure(parts[0], float(parts[1])))
        for item in args.cload:
            parts = [p.strip() for p in item.split(",")]
            if len(parts) != 3:
                raise ValueError(f"--cload expects NAME,dof,value, got {item!r}")
            target: str | int = int(parts[0]) if parts[0].isdigit() else parts[0]
            loads.append(Load.concentrated(target, int(parts[1]), float(parts[2])))
        for item in args.temp:
            parts = [p.strip() for p in item.split(",")]
            if len(parts) != 2:
                raise ValueError(f"--temp expects NAME,value, got {item!r}")
            bcs.append(BoundaryCondition(parts[0], 11, 11, float(parts[1])))
        for item in args.flux:
            parts = [p.strip() for p in item.split(",")]
            if len(parts) != 2:
                raise ValueError(f"--flux expects NAME,value, got {item!r}")
            loads.append(Load.surface_flux(parts[0], float(parts[1])))
        for item in args.film:
            parts = [p.strip() for p in item.split(",")]
            if len(parts) != 3:
                raise ValueError(f"--film expects NAME,sink,h, got {item!r}")
            loads.append(Load.film(parts[0], float(parts[1]), float(parts[2])))
        for item in args.radiate:
            parts = [p.strip() for p in item.split(",")]
            if len(parts) != 3:
                raise ValueError(
                    f"--radiate expects NAME,sink,emiss, got {item!r}"
                )
            loads.append(Load.radiate(parts[0], float(parts[1]), float(parts[2])))

        physical_constants = None
        if args.radiate:
            if args.stefan_boltzmann is None or args.absolute_zero is None:
                raise ValueError(
                    "--radiate needs --stefan-boltzmann and --absolute-zero "
                    "(the *PHYSICAL CONSTANTS for the σT⁴ law)"
                )
            physical_constants = PhysicalConstants(
                stefan_boltzmann=args.stefan_boltzmann,
                absolute_zero=args.absolute_zero,
            )
        elif args.stefan_boltzmann is not None or args.absolute_zero is not None:
            raise ValueError(
                "--stefan-boltzmann/--absolute-zero only feed --radiate — add "
                "a --radiate load or drop them"
            )

        checksum_before = file_sha256(surface_path)
        mesh = load_surface(surface_path)
        if args.outer_surface is not None:
            from meshforge.geometry import ThicknessSpec

            t_arg = ThicknessSpec.from_outer_surface(load_surface(args.outer_surface))
        else:
            t_arg = args.thickness
        backend = get_backend(args.backend)
        kwargs: dict = {"n_theta": args.n_theta, "n_axial": args.n_axial}
        if args.backend == "swept":
            kwargs["n_layers"] = args.n_layers
            kwargs["layer_ratio"] = args.layer_ratio
        if args.backend == "tet" and args.mesh_size is not None:
            kwargs["mesh_size"] = args.mesh_size
        wall = backend.mesh_wall(mesh, t_arg, **kwargs)
        if wall.quality.n_inverted > 0:
            raise ValueError(
                f"refusing to write a deck from a mesh with "
                f"{wall.quality.n_inverted} inverted cell(s)"
            )
        if args.pin_lateral:
            bcs.extend(lateral_pins(wall))

        # V45 thermal load: uniform value, or a solved heat frd's NDTEMP
        # re-emitted per-node (the frd must BE this mesh — same 1..N ids and
        # coordinates). Trap-level refusals (missing --expansion/--t-ref)
        # are DeckSpec.validate()'s.
        if args.temperature is not None and args.temperature_from is not None:
            raise ValueError(
                "give either --temperature or --temperature-from, not both"
            )
        if args.temperature_time is not None and args.temperature_from is None:
            raise ValueError(
                "--temperature-time selects a snapshot from the "
                "--temperature-from frd's transient history — add "
                "--temperature-from, or drop it"
            )
        nodal_temps = None
        if args.temperature_from is not None:
            import numpy as np

            from meshforge.solve import nodal_temperatures, read_frd

            frd_path = Path(args.temperature_from)
            if not frd_path.is_file():
                raise ValueError(
                    f"--temperature-from frd not found: {frd_path}"
                )
            frd = read_frd(frd_path)
            n = wall.grid.n_points
            if len(frd.node_ids) != n or not np.array_equal(
                frd.node_ids, np.arange(1, n + 1)
            ):
                raise ValueError(
                    f"--temperature-from frd carries {len(frd.node_ids)} "
                    f"node(s) that are not this wall mesh's 1..{n} — the "
                    f"temperature field belongs to a different mesh"
                )
            pts = np.asarray(wall.grid.points)
            scale = max(1.0, float(np.abs(pts).max()))
            if not np.allclose(pts, frd.points, rtol=0.0, atol=2e-5 * scale):
                raise ValueError(
                    "--temperature-from frd node coordinates do not match "
                    "this wall mesh — the temperature field belongs to a "
                    "different mesh"
                )
            nodal_temps = nodal_temperatures(frd, time=args.temperature_time)

        # nlgeom, preloaded, the transient knobs and the riks knobs are passed
        # through for every kind so --nlgeom/--preloaded/--dt/--riks with the
        # wrong step fail loudly, never dropped.
        passthrough = dict(
            transient=args.transient, dt=args.dt, period=args.period,
            initial_temperature=args.t0, preloaded=args.preloaded,
            riks=args.riks, riks_max_lpf=args.riks_max_lpf,
            riks_dl_init=args.riks_dl_init, riks_dl_min=args.riks_dl_min,
            riks_dl_max=args.riks_dl_max,
        )
        step = (
            Step(
                kind="frequency", n_modes=args.n_modes, nlgeom=args.nlgeom,
                **passthrough,
            )
            if args.analysis == "frequency"
            else Step(
                kind="buckle", n_modes=args.n_modes, nlgeom=args.nlgeom,
                **passthrough,
            )
            if args.analysis == "buckle"
            else Step(kind="heat", nlgeom=args.nlgeom, **passthrough)
            if args.analysis == "heat"
            else Step(nlgeom=args.nlgeom, **passthrough)
        )
        spec = DeckSpec(
            material=material,
            step=step,
            boundary_conditions=tuple(bcs),
            loads=tuple(loads),
            target=args.target,
            formulation=args.formulation,
            physical_constants=physical_constants,
            uniform_temperature=args.temperature,
            nodal_temperatures=nodal_temps,
            reference_temperature=args.t_ref,
        )
        written = write_deck(
            wall, spec, out, input_path=surface_path
        )
    except (FileNotFoundError, TypeError, ValueError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if file_sha256(surface_path) != checksum_before:
        print(
            f"error: input changed on disk during deck build: {surface_path}",
            file=sys.stderr,
        )
        return 1

    q = wall.quality
    print(f"target      : {spec.target}  formulation: {spec.resolved_formulation()}")
    print(f"material    : {material.kind}  {material.params}")
    print(f"cells       : {wall.grid.n_cells}  points: {wall.grid.n_points}")
    print(f"quality     : inverted={q.n_inverted}  sj_min={q.sj_min:.4g}")
    print(f"bcs/loads   : {len(bcs)} boundary condition(s), {len(loads)} load(s)")
    print(f"deck        : {written}")
    return 0


def _cmd_blayers(args: argparse.Namespace) -> int:
    import json

    from meshforge.export import guard_output_path
    from meshforge.io import file_sha256, load_surface
    from meshforge.meshing.prism import mesh_boundary_layers

    surface_path: Path = args.surface
    out: Path = args.out or (args.outputs / f"{surface_path.stem}_blayers.vtu")

    try:
        checksum_before = file_sha256(surface_path)
        mesh = load_surface(surface_path)
        wall = mesh_boundary_layers(
            mesh,
            args.first,
            n_layers=args.n_layers,
            growth=args.growth,
            normal_smoothing=args.normal_smoothing,
        )
        guard_output_path(out, input_paths=[surface_path])
    except (FileNotFoundError, TypeError, ValueError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if file_sha256(surface_path) != checksum_before:
        print(f"error: input changed on disk during meshing: {surface_path}", file=sys.stderr)
        return 1

    out.parent.mkdir(parents=True, exist_ok=True)
    wall.grid.save(out)
    summary = wall.summary()
    summary_path = out.with_suffix(".summary.json")
    summary_path.write_text(json.dumps(summary, indent=2) + "\n")

    q = summary["quality"]
    print(f"cells       : {summary['n_cells']} wedges  points: {summary['n_points']}")
    print(f"boundary    : {summary['boundary_faces']}")
    print(
        f"quality     : inverted={q['n_inverted']}  sj_min={q['sj_min']:.4g}  "
        f"volume={q['total_volume']:.6g}"
    )
    for note in summary["notes"]:
        print(f"note        : {note}")
    print(f"mesh        : {out}")
    print(f"summary     : {summary_path}")
    return 0 if q["n_inverted"] == 0 else 1


def _cmd_import_image(args: argparse.Namespace) -> int:
    import json

    from meshforge.export import guard_output_path
    from meshforge.io.image import (
        load_volume_affine,
        override_spacing,
        volume_input_sha256,
        volume_to_surface,
    )
    from meshforge.quality.check import check_surface

    src: Path = args.volume
    out: Path = args.out or (args.outputs / f"{src.stem}_surface.vtp")

    try:
        checksum_before = volume_input_sha256(src)
        # Full affine: rotated / flipped NIfTI grids map exactly (D83).
        array, affine, vinfo = load_volume_affine(src)
        if args.spacing:
            affine = override_spacing(affine, args.spacing)
        surface, provenance = volume_to_surface(array, args.level, affine=affine)
        provenance["notes"] = list(vinfo["notes"]) + provenance["notes"]
        guard_output_path(out, input_paths=[src if src.is_file() else None])
    except (FileNotFoundError, TypeError, ValueError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if volume_input_sha256(src) != checksum_before:
        print(f"error: input changed on disk during import: {src}", file=sys.stderr)
        return 1

    out.parent.mkdir(parents=True, exist_ok=True)
    surface.save(out)
    report = check_surface(surface, path=str(src), sha256=checksum_before)
    provenance["check"] = report.to_dict()
    prov_path = out.parent / f"{src.stem}_image.json"
    prov_path.write_text(json.dumps(provenance, indent=2) + "\n")

    print(f"volume      : shape {provenance['shape']}  spacing {provenance['spacing']}")
    print(f"iso level   : {provenance['level']:g}  (data {provenance['data_range'][0]:.6g}"
          f"..{provenance['data_range'][1]:.6g})")
    print(f"mesh        : {provenance['n_points']} pts, {provenance['n_triangles']} tris")
    print(f"check       : watertight={report.watertight}  mesh_ready={report.mesh_ready}")
    for note in provenance["notes"]:
        print(f"note        : {note}")
    print(f"surface     : {out}")
    print(f"provenance  : {prov_path}")
    if not report.mesh_ready:
        print("error: iso-surface is not mesh_ready — see check report",
              file=sys.stderr)
        return 1
    return 0


def _cmd_segment(args: argparse.Namespace) -> int:
    import json

    from meshforge.export import guard_output_path
    from meshforge.io.segment import DEFAULT_DEVICE, DEFAULT_FAST, segment_ct
    from meshforge.quality.check import check_surface

    src: Path = args.ct
    stem = src.name
    for suffix in (".nii.gz", ".nrrd", ".nii"):
        if stem.lower().endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    out: Path = args.out or (args.outputs / "segment" / f"{stem}_aorta.vtp")
    if out.suffix.lower() != ".vtp":
        out = out.with_suffix(".vtp")
    base = out.with_suffix("")
    mask_path = base.parent / f"{base.name}_mask.nii.gz"
    log_path = base.parent / f"{base.name}_totalseg.log"
    json_path = base.parent / f"{base.name}_segment.json"
    device = args.device or DEFAULT_DEVICE
    fast = bool(args.fast or DEFAULT_FAST)

    try:
        for target in (out, mask_path, log_path, json_path, args.lumen_out):
            if target is not None:
                guard_output_path(target, input_paths=[src])
        if args.reference_zenodo is not None and not args.reference_zenodo.is_dir():
            raise FileNotFoundError(
                f"Zenodo case directory not found: {args.reference_zenodo}"
            )
        print(
            f"segment     : {src.name} -> TotalSegmentator (task total, roi aorta, "
            f"{'fast' if fast else 'full'}, device {device}) ...",
            flush=True,
        )
        seg = segment_ct(
            src,
            device=device,
            fast=fast,
            smoothing=args.smoothing,
            timeout=args.timeout,
            keep_mask=mask_path,
            lumen_hu=args.lumen_hu if args.lumen_out is not None else None,
            context_rois=(),  # the CLI writes the vessel; the heart ghost is the Workbench's
        )
    except (FileNotFoundError, TypeError, ValueError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    out.parent.mkdir(parents=True, exist_ok=True)
    seg.surface.save(out)
    if args.lumen_out is not None:
        args.lumen_out.parent.mkdir(parents=True, exist_ok=True)
        seg.lumen_surface.save(args.lumen_out)
    log_path.write_text(seg.log)
    summary = dict(seg.summary)
    report = check_surface(seg.surface, path=str(src), sha256=seg.input_sha256)
    summary["check"] = report.to_dict()
    ts, mask, surf, ct = (
        summary["totalseg"], summary["mask"], summary["surface"], summary["ct"]
    )
    print(
        f"ct          : {ct['format']} {ct['shape']} voxels, spacing "
        f"{[round(v, 4) for v in ct['spacing_mm']]} mm"
    )
    print(
        f"totalseg    : {ts['version'] or '(version unknown)'}  task total, roi "
        f"{'+'.join(ts['rois'])}, {'fast' if ts['fast'] else 'full'}, device "
        f"{ts['device']}, wall {ts['wall_time_s']:.1f} s"
    )
    print(
        f"mask        : {mask['kept_voxels']} voxels kept ({summary['mask_volume_ml']:.2f} mL), "
        f"{mask['components']} component(s), dropped {mask['dropped_components']} / "
        f"{mask['dropped_voxels']} voxels, smoothing sigma {mask['sigma']:g} vox"
    )
    print(
        f"surface     : {surf['n_points']} pts, {surf['n_triangles']} tris, "
        f"enclosed {surf['enclosed_volume_ml']} mL, frame {surf['frame']}"
    )
    print(f"check       : watertight={report.watertight}  mesh_ready={report.mesh_ready}")
    for note in summary["notes"]:
        print(f"note        : {note}")
    if args.lumen_out is not None:
        lum = summary["lumen"]
        print(
            f"lumen       : {lum['kept_voxels']} voxels >= {lum['lumen_hu']:g} HU kept "
            f"({lum['lumen_volume_ml']:.2f} mL, {100 * lum['fraction_of_mask']:.1f}% of the AI "
            f"mask, Dice vs mask {lum['dice_vs_ai_mask']:.4f}); dropped "
            f"{lum['dropped_components']} piece(s) / {lum['dropped_voxels']} voxels"
        )
        print(f"lumen       : {lum['method']}")
        for note in lum["notes"][1:]:
            print(f"lumen note  : {note}")

    rc = 0
    if args.reference_zenodo is not None:
        from meshforge.io.zenodo_aaa import validate_against_reference

        try:
            val = validate_against_reference(
                seg.surface, args.reference_zenodo, seg.ct_shape, seg.ct_affine
            )
        except (FileNotFoundError, ValueError) as exc:
            print(f"error: reference validation failed: {exc}", file=sys.stderr)
            rc = 1
        else:
            summary["validation"] = val
            z0, z1 = val["slab_z_mm"]
            print(f"reference   : {val['reference']}, case {val['case']}")
            print(
                f"slab        : z {z0:.2f} .. {z1:.2f} mm (reference end planes), "
                f"{val['slab_voxels']} CT voxels; segmented volume "
                f"{val['segmented_volume_ml']:.2f} mL"
            )
            for name, reg in val["regions"].items():
                d = reg["distance"]
                ratio = reg["volume_ratio"]
                ratio_txt = "n/a" if ratio is None else f"{ratio:.3f}"
                print(
                    f"dice {name:<9}: {reg['dice']:.4f}  ref volume "
                    f"{reg['volume_ml']:.2f} mL (seg/ref {ratio_txt})  "
                    f"ASSD {d['assd']:.2f} mm  HD95 {d['hd95']:.2f} mm  "
                    f"[{val['region_labels'][name]}]"
                )
            best = val["regions"][val["best"]]
            print(
                f"best match  : {val['best']} ({val['region_labels'][val['best']]}) "
                f"Dice {best['dice']:.4f}, ASSD {best['distance']['assd']:.2f} mm, "
                f"HD95 {best['distance']['hd95']:.2f} mm"
            )

    json_path.write_text(json.dumps(summary, indent=2, default=str) + "\n")
    print(f"surface     : {out}")
    print(f"mask        : {mask_path}")
    print(f"log         : {log_path}")
    print(f"summary     : {json_path}")
    if args.lumen_out is not None:
        print(f"lumen       : {args.lumen_out}")
    if not report.mesh_ready:
        print("error: segmented surface is not mesh_ready — see check report",
              file=sys.stderr)
        return 1
    return rc


def _cmd_crop(args: argparse.Namespace) -> int:
    import json

    from meshforge.export import guard_output_path
    from meshforge.geometry.crop import crop_aaa, crop_with_planes, planes_from_report
    from meshforge.io import file_sha256, load_surface
    from meshforge.pipeline import crop_text
    from meshforge.quality.check import check_surface

    src: Path = args.surface
    out: Path = args.out or (args.outputs / "crop" / f"{src.stem}_crop.vtp")
    if out.suffix.lower() != ".vtp":
        out = out.with_suffix(".vtp")
    json_path = out.with_name(f"{out.stem}_crop.json")
    lumen_out = None
    if args.lumen is not None:
        lumen_out = args.lumen_out or out.with_name(f"{out.stem}_lumen.vtp")
    try:
        if args.lumen_out is not None and args.lumen is None:
            raise ValueError("--lumen-out needs --lumen (the surface to cut)")
        for target in (out, json_path, lumen_out):
            if target is not None:
                guard_output_path(target, input_paths=[src, args.lumen, args.planes_from])
        sha = {p: file_sha256(p) for p in (src, args.lumen) if p is not None}
        surface = load_surface(src)
        lumen = load_surface(args.lumen) if args.lumen is not None else None
        print(f"crop        : {src.name}"
              + (f" + {args.lumen.name} (same planes)" if lumen is not None else ""), flush=True)
        if args.planes_from is not None:
            if args.distal_height is not None or args.proximal_height is not None:
                raise ValueError("--planes-from and manual heights are mutually exclusive")
            planes = planes_from_report(json.loads(args.planes_from.read_text()))
            cut, caps, note = crop_with_planes(surface, planes, what="surface")
            rep = {
                "planes": {pl.name: pl.to_dict() for pl in planes},
                "caps": {k: v.to_dict() for k, v in caps.items()},
                "surface": {"n_points": int(cut.n_points), "n_triangles": int(cut.n_cells),
                            "watertight": int(cut.n_open_edges) == 0,
                            "volume_ml": round(float(cut.volume) / 1000.0, 4)},
                "landmarks": None,
                "notes": [f"planes reused from {args.planes_from.name}", note],
            }
            lumen_cut = None
            if lumen is not None:
                lumen_cut, lcaps, lnote = crop_with_planes(lumen, planes, what="lumen")
                rep["lumen"] = {"n_points": int(lumen_cut.n_points),
                                "n_triangles": int(lumen_cut.n_cells),
                                "watertight": int(lumen_cut.n_open_edges) == 0,
                                "volume_ml": round(float(lumen_cut.volume) / 1000.0, 4),
                                "caps": {k: v.to_dict() for k, v in lcaps.items()}}
                rep["notes"].append(lnote)
        else:
            res = crop_aaa(
                surface,
                neck_extension_mm=args.neck_extension,
                distal_margin_mm=args.distal_margin,
                distal_height_mm=args.distal_height,
                proximal_height_mm=args.proximal_height,
                lumen=lumen,
            )
            cut, lumen_cut, rep = res.surface, res.lumen, res.report()
    except (FileNotFoundError, TypeError, ValueError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    for p, h in sha.items():
        if file_sha256(p) != h:
            print(f"error: input changed on disk during the crop: {p}", file=sys.stderr)
            return 1
    out.parent.mkdir(parents=True, exist_ok=True)
    cut.save(out)
    if lumen_cut is not None:
        lumen_cut.save(lumen_out)
    report = check_surface(cut, path=str(src), sha256=sha[src])
    rep["check"] = report.to_dict()
    json_path.write_text(json.dumps(rep, indent=1, default=str) + "\n")
    print(crop_text(rep), end="")
    print(f"check       : watertight={report.watertight}  mesh_ready={report.mesh_ready}")
    print(f"surface     : {out}")
    if lumen_cut is not None:
        print(f"lumen       : {lumen_out}")
    print(f"report      : {json_path}")
    if not report.mesh_ready:
        print("error: the cropped surface is not mesh_ready", file=sys.stderr)
        return 1
    return 0


def _cmd_sac(args: argparse.Namespace) -> int:
    import json

    from meshforge.export import guard_output_path
    from meshforge.geometry.sac import recover_sac_from_files, write_sac_mask
    from meshforge.pipeline import sac_text
    from meshforge.quality.check import check_surface

    src: Path = args.ct
    stem = src.name
    for suffix in (".nii.gz", ".nrrd", ".nii"):
        if stem.lower().endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    out: Path = args.out or (args.outputs / "sac" / f"{stem}_sac.vtp")
    if out.suffix.lower() != ".vtp":
        out = out.with_suffix(".vtp")
    base = out.with_suffix("")
    mask_path = base.parent / f"{base.name}_mask.nii.gz"
    json_path = base.parent / f"{base.name}.json"
    try:
        for target in (out, mask_path, json_path):
            guard_output_path(target, input_paths=[src, args.lumen_mask])
        if args.reference_zenodo is not None and not args.reference_zenodo.is_dir():
            raise FileNotFoundError(
                f"Zenodo case directory not found: {args.reference_zenodo}"
            )
        print(f"sac         : {src.name} + lumen {args.lumen_mask.name} -> outer sac ...",
              flush=True)
        run = recover_sac_from_files(
            src,
            args.lumen_mask,
            lumen_hu=args.lumen_hu,
            fat_hu=args.fat_hu,
            max_thickness_mm=args.max_thickness,
            n_rays=args.n_rays,
            roi_mm=args.roi_mm,
            min_resolved=args.min_resolved,
            smoothing=args.smoothing,
        )
    except (FileNotFoundError, TypeError, ValueError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    out.parent.mkdir(parents=True, exist_ok=True)
    run.surface.save(out)
    write_sac_mask(run.result, mask_path)
    summary = dict(run.summary)
    report = check_surface(run.surface, path=str(src), sha256=run.input_sha256["ct"])
    summary["check"] = report.to_dict()
    surf = summary["surface"]
    print(sac_text(summary), end="")
    print(
        f"surface     : {surf['n_points']} pts, {surf['n_triangles']} tris, "
        f"enclosed {surf['enclosed_volume_ml']} mL, point data "
        f"{'+'.join(surf['point_data'])}, frame {surf['frame']}"
    )
    print(f"check       : watertight={report.watertight}  mesh_ready={report.mesh_ready}")

    rc = 0
    if args.reference_zenodo is not None:
        from meshforge.io.zenodo_aaa import validate_against_reference

        try:
            val = validate_against_reference(
                run.surface, args.reference_zenodo, run.grid_shape, run.grid_affine
            )
        except (FileNotFoundError, ValueError) as exc:
            print(f"error: reference validation failed: {exc}", file=sys.stderr)
            rc = 1
        else:
            summary["validation"] = val
            z0, z1 = val["slab_z_mm"]
            print(f"reference   : {val['reference']}, case {val['case']}")
            print(
                f"slab        : z {z0:.2f} .. {z1:.2f} mm (reference end planes); "
                f"sac volume in slab {val['segmented_volume_ml']:.2f} mL"
            )
            for name, reg in val["regions"].items():
                d = reg["distance"]
                ratio = reg["volume_ratio"]
                ratio_txt = "n/a" if ratio is None else f"{ratio:.3f}"
                print(
                    f"dice {name:<9}: {reg['dice']:.4f}  ref volume "
                    f"{reg['volume_ml']:.2f} mL (sac/ref {ratio_txt})  "
                    f"ASSD {d['assd']:.2f} mm  HD95 {d['hd95']:.2f} mm  "
                    f"[{val['region_labels'][name]}]"
                )
            print("agreement   : with a reference segmentation, not accuracy")

    json_path.write_text(json.dumps(summary, indent=2, default=str) + "\n")
    print(f"surface     : {out}")
    print(f"sac mask    : {mask_path}")
    print(f"summary     : {json_path}")
    if not report.mesh_ready:
        print("error: sac surface is not mesh_ready — see check report", file=sys.stderr)
        return 1
    return rc


def _cmd_tissue_index(args: argparse.Namespace) -> int:
    import json

    from meshforge.tissue.strength import (
        StrengthLibraryError,
        index_band_report,
        index_band_text,
        pws_from_summary,
    )

    try:
        if args.from_summary is not None:
            pws, where, digest = pws_from_summary(args.from_summary)
            pws_source = f"{args.from_summary.name}: {where}"
        else:
            pws, pws_source, digest = args.pws, "entered", None
        if args.out is not None and args.from_summary is not None and (
            args.out.resolve() == args.from_summary.resolve()
        ):
            raise ValueError(f"refusing to overwrite input file: {args.from_summary}")
        report = index_band_report(pws, selection=args.selection, method=args.method)
    except (ValueError, FileNotFoundError, StrengthLibraryError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    report["pws_source"] = pws_source
    if digest is not None:
        report["pws_summary_sha256"] = digest
    print(index_band_text(report, pws_source=pws_source), end="")
    if args.out is not None:
        out = args.out if args.out.suffix.lower() == ".json" else args.out.with_suffix(".json")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, indent=2) + "\n")
        print(f"summary            : {out}")
    return 0


def _cmd_import_cso(args: argparse.Namespace) -> int:
    import json

    from meshforge.export import guard_output_path
    from meshforge.io.cso import cso_input_sha256, cso_to_surface, load_cso
    from meshforge.quality.check import check_surface

    src: Path = args.file
    out: Path = args.out or (args.outputs / f"{src.stem}_surface.vtp")

    method = args.method
    dicom_before = None
    try:
        checksum_before = cso_input_sha256(src)
        data = load_cso(src, on_open=args.on_open)
        grid = None
        if args.dicom is not None:
            from meshforge.io.image import dicom_grid, volume_input_sha256

            dicom_before = volume_input_sha256(args.dicom)  # rule 1
            grid = dicom_grid(args.dicom)
            method = "voxel"
        if method == "auto":
            from meshforge.pipeline import auto_cso_surface

            surface, provenance, _cl, strategy = auto_cso_surface(
                data,
                n_ring=args.n_ring,
                voxel_size=args.voxel_size,
                cap_ends=not args.no_cap,
            )
        else:
            surface, provenance = cso_to_surface(
                data,
                method=method,
                n_ring=args.n_ring,
                voxel_size=args.voxel_size,
                cap_ends=not args.no_cap,
                grid=grid,
            )
            strategy = provenance["method"]
        provenance["strategy"] = strategy
        guard_output_path(out, input_paths=[src])
    except (FileNotFoundError, TypeError, ValueError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if cso_input_sha256(src) != checksum_before:
        print(f"error: input changed on disk during import: {src}", file=sys.stderr)
        return 1
    if args.dicom is not None and volume_input_sha256(args.dicom) != dicom_before:
        print(f"error: DICOM changed on disk during import: {args.dicom}", file=sys.stderr)
        return 1

    provenance.update(
        {
            "version": list(data.version),
            "n_contours": len(data.contours),
            "n_guides": len(data.guides),
            "on_open": args.on_open,
            "frameOfReferenceUID": data.frame_of_reference_uid,
            "seriesInstanceUID": data.series_instance_uid,
        }
    )
    if args.dicom is not None:
        provenance["dicom"] = str(args.dicom)
        provenance["dicom_sha256"] = dicom_before
    provenance["notes"] = list(provenance.get("notes", [])) + list(data.notes)

    out.parent.mkdir(parents=True, exist_ok=True)
    surface.save(out)
    report = check_surface(surface, path=str(src), sha256=checksum_before)
    provenance["check"] = report.to_dict()
    prov_path = out.parent / f"{src.stem}_cso.json"
    prov_path.write_text(json.dumps(provenance, indent=2) + "\n")

    print(f"contours    : {provenance['n_contours']}  guides {provenance['n_guides']}"
          f"  version {tuple(data.version)}")
    print(f"method      : {provenance['method']}")
    print(f"strategy    : {provenance.get('strategy', provenance['method'])}")
    print(f"mesh        : {provenance['n_points']} pts, {provenance['n_triangles']} tris")
    print(f"check       : watertight={report.watertight}  mesh_ready={report.mesh_ready}")
    for note in provenance["notes"]:
        print(f"note        : {note}")
    print(f"surface     : {out}")
    print(f"provenance  : {prov_path}")
    if not report.mesh_ready:
        print("error: CSO surface is not mesh_ready — see check report",
              file=sys.stderr)
        return 1
    return 0


def _cmd_import_cad3d(args: argparse.Namespace) -> int:
    import json

    from meshforge.export import guard_output_path
    from meshforge.io import file_sha256
    from meshforge.io.cad3d import load_solid
    from meshforge.quality.check import check_surface

    src: Path = args.file
    out: Path = args.out or (args.outputs / f"{src.stem}_surface.vtp")

    try:
        checksum_before = file_sha256(src)
        surface, provenance = load_solid(
            src, mesh_size=args.mesh_size, mesh_size_factor=args.mesh_size_factor
        )
        guard_output_path(out, input_paths=[src])
    except (FileNotFoundError, TypeError, ValueError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if file_sha256(src) != checksum_before:
        print(f"error: input file changed on disk during import: {src}", file=sys.stderr)
        return 1

    out.parent.mkdir(parents=True, exist_ok=True)
    surface.save(out)
    report = check_surface(surface, path=str(src), sha256=checksum_before)
    provenance["check"] = report.to_dict()
    prov_path = out.parent / f"{src.stem}_cad3d.json"
    prov_path.write_text(json.dumps(provenance, indent=2) + "\n")

    print(f"format      : {provenance['format']}  volumes: {provenance['n_volumes']}"
          f"  surfaces: {provenance['n_surfaces']}")
    print(f"mesh        : {provenance['n_points']} pts, "
          f"{provenance['n_triangles']} tris (size {provenance['mesh_size']:.6g})")
    print(f"check       : watertight={report.watertight}  mesh_ready={report.mesh_ready}")
    print(f"surface     : {out}")
    print(f"provenance  : {prov_path}")
    if not report.mesh_ready:
        print("error: imported surface is not mesh_ready — see check report",
              file=sys.stderr)
        return 1
    return 0


def _cmd_foam(args: argparse.Namespace) -> int:
    from meshforge.export.foam import write_polymesh
    from meshforge.io import file_sha256, load_surface
    from meshforge.meshing import get_backend, valid_group_name

    surface_path: Path = args.surface
    case: Path = args.case or (args.outputs / f"{surface_path.stem}_foam")

    try:
        if args.thickness is not None and args.outer_surface is not None:
            raise ValueError(
                "--thickness and --outer-surface are mutually exclusive; "
                "drop --thickness to use the measured outer surface"
            )
        if args.thickness is None and args.outer_surface is None:
            raise ValueError("give either --thickness or --outer-surface")
        if args.default_patch is not None:
            valid_group_name(args.default_patch)  # F1: minted patch name
        patch_types: dict[str, str] = {}
        for item in args.patch_type:
            parts = [p.strip() for p in item.split(",")]
            if len(parts) != 2 or not all(parts):
                raise ValueError(f"--patch-type expects NAME,TYPE, got {item!r}")
            patch_types[valid_group_name(parts[0])] = parts[1]  # F1

        checksum_before = file_sha256(surface_path)
        mesh = load_surface(surface_path)
        if args.outer_surface is not None:
            from meshforge.geometry import ThicknessSpec

            t_arg = ThicknessSpec.from_outer_surface(load_surface(args.outer_surface))
        else:
            t_arg = args.thickness
        backend = get_backend(args.backend)
        kwargs: dict = {"n_theta": args.n_theta, "n_axial": args.n_axial}
        if args.backend == "swept":
            kwargs["n_layers"] = args.n_layers
            kwargs["layer_ratio"] = args.layer_ratio
        if args.backend == "tet" and args.mesh_size is not None:
            kwargs["mesh_size"] = args.mesh_size
        wall = backend.mesh_wall(mesh, t_arg, **kwargs)
        if wall.quality.n_inverted > 0:
            raise ValueError(
                f"refusing to export a mesh with "
                f"{wall.quality.n_inverted} inverted cell(s)"
            )
        written = write_polymesh(
            wall, case, patch_types=patch_types,
            default_patch=args.default_patch, input_path=surface_path,
        )
    except (FileNotFoundError, TypeError, ValueError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if file_sha256(surface_path) != checksum_before:
        print(
            f"error: input changed on disk during export: {surface_path}",
            file=sys.stderr,
        )
        return 1

    q = wall.quality
    print(f"cells       : {wall.grid.n_cells}  points: {wall.grid.n_points}")
    print(f"quality     : inverted={q.n_inverted}  sj_min={q.sj_min:.4g}")
    print(
        "patches     : "
        + "  ".join(f"{k}({g.n_faces})" for k, g in wall.boundary.items())
    )
    print(f"polyMesh    : {written}")
    return 0


def _cmd_fluent(args: argparse.Namespace) -> int:
    from meshforge.export.fluent import write_fluent
    from meshforge.io import file_sha256, load_surface
    from meshforge.meshing import get_backend, valid_group_name

    surface_path: Path = args.surface
    out: Path = args.out or (args.outputs / f"{surface_path.stem}.msh")

    try:
        if args.thickness is not None and args.outer_surface is not None:
            raise ValueError(
                "--thickness and --outer-surface are mutually exclusive; "
                "drop --thickness to use the measured outer surface"
            )
        if args.thickness is None and args.outer_surface is None:
            raise ValueError("give either --thickness or --outer-surface")
        if args.default_patch is not None:
            valid_group_name(args.default_patch)  # F1: minted zone name
        bc_types: dict[str, str] = {}
        for item in args.bc_type:
            parts = [p.strip() for p in item.split(",")]
            if len(parts) != 2 or not all(parts):
                raise ValueError(f"--bc-type expects NAME,TYPE, got {item!r}")
            bc_types[valid_group_name(parts[0])] = parts[1]  # F1

        checksum_before = file_sha256(surface_path)
        mesh = load_surface(surface_path)
        if args.outer_surface is not None:
            from meshforge.geometry import ThicknessSpec

            t_arg = ThicknessSpec.from_outer_surface(load_surface(args.outer_surface))
        else:
            t_arg = args.thickness
        backend = get_backend(args.backend)
        kwargs: dict = {"n_theta": args.n_theta, "n_axial": args.n_axial}
        if args.backend == "swept":
            kwargs["n_layers"] = args.n_layers
            kwargs["layer_ratio"] = args.layer_ratio
        if args.backend == "tet" and args.mesh_size is not None:
            kwargs["mesh_size"] = args.mesh_size
        wall = backend.mesh_wall(mesh, t_arg, **kwargs)
        if wall.quality.n_inverted > 0:
            raise ValueError(
                f"refusing to export a mesh with "
                f"{wall.quality.n_inverted} inverted cell(s)"
            )
        written = write_fluent(
            wall, out, bc_types=bc_types,
            default_patch=args.default_patch, input_path=surface_path,
        )
    except (FileNotFoundError, TypeError, ValueError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if file_sha256(surface_path) != checksum_before:
        print(
            f"error: input changed on disk during export: {surface_path}",
            file=sys.stderr,
        )
        return 1

    q = wall.quality
    print(f"cells       : {wall.grid.n_cells}  points: {wall.grid.n_points}")
    print(f"quality     : inverted={q.n_inverted}  sj_min={q.sj_min:.4g}")
    print(
        "zones       : "
        + "  ".join(f"{k}({g.n_faces})" for k, g in wall.boundary.items())
    )
    print(f"fluent mesh : {written}")
    return 0


def _cmd_cfd(args: argparse.Namespace) -> int:
    import json

    from meshforge.export import guard_output_path
    from meshforge.pipeline import _parse_inlet_spec, _parse_outlet_spec
    from meshforge.solve import CfdBC, CfdSpec, check_mesh, solve_case, write_case_files

    case: Path = args.case
    try:
        if args.checkmesh:
            report = check_mesh(case, timeout=args.timeout)
            print(f"checkMesh   : Mesh OK  {report}")
            return 0

        builds_case = bool(
            args.inlet or args.outlet or args.moving_wall or args.empty
        )
        if not builds_case and args.turbulence != "laminar":
            raise ValueError(
                "--turbulence applies only when the solver files are built "
                "here (--inlet/--outlet/--moving-wall); a complete case "
                "already carries its turbulence setup"
            )
        if not builds_case and args.closed_cavity:
            raise ValueError(
                "--closed-cavity applies only when the solver files are "
                "built here; a complete case already carries its fvSolution"
            )
        if builds_case:
            bcs: dict[str, CfdBC] = {}
            if args.inlet:
                _, sep, raw = args.inlet.partition("=")
                if sep and len(raw.split(",")) == 1:
                    raise ValueError(
                        "--inlet NAME=SPEED needs the mesh in memory to find "
                        "the patch's inward normal, and this command only "
                        "has a case directory: give NAME=VX,VY,VZ here, or "
                        "use the speed form in the Workbench or a work file "
                        "run with 'meshforge replay'"
                    )
                name, velocity = _parse_inlet_spec(args.inlet)
                bcs[name] = CfdBC("velocity", velocity)
            if args.outlet:
                name, p_value = _parse_outlet_spec(args.outlet)
                bcs[name] = CfdBC("pressure", p_value)
            if args.moving_wall:
                name, velocity = _parse_inlet_spec(args.moving_wall)
                bcs[name] = CfdBC("moving_wall", velocity)
            for patch in filter(
                None, (p.strip() for p in (args.empty or "").split(","))
            ):
                bcs[patch] = CfdBC("empty")
            spec = CfdSpec(
                solver=args.solver,
                nu=args.nu,
                bcs=bcs,
                end_time=args.end_time,
                delta_t=args.delta_t,
                write_interval=args.write_interval,
                turbulence=args.turbulence,
                turbulence_intensity=args.intensity,
                turbulence_length=args.turb_length,
                closed_cavity=args.closed_cavity,
                p_ref_cell=args.p_ref_cell,
                p_ref_value=args.p_ref_value,
            )
            patch_kinds = write_case_files(case, spec)
            print(f"case        : {case}  patches {patch_kinds}")
        result = solve_case(case, timeout=args.timeout, series=args.series)

        out_dir: Path = args.outputs / "cfd" / case.name
        vtu = out_dir / f"{case.name}_results.vtu"
        guard_output_path(vtu, input_paths=[])
        frame_paths = []
        if result.frames is not None:
            # Index-named (time-named files would depend on float
            # formatting); the JSON summary maps index -> time.
            frame_paths = [
                out_dir / f"{case.name}_t{i:04d}.vtu"
                for i in range(len(result.frames))
            ]
            for path in frame_paths:
                guard_output_path(path, input_paths=[])
    except (FileNotFoundError, TypeError, ValueError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    out_dir.mkdir(parents=True, exist_ok=True)
    result.grid.save(vtu)
    for path, frame in zip(frame_paths, result.frames or []):
        frame.save(path)
    summary = result.summary()
    summary["results_vtu"] = str(vtu)
    if frame_paths:
        summary["frame_vtus"] = [str(p) for p in frame_paths]
    summary_path = out_dir / f"{case.name}_cfd.json"
    summary_path.write_text(json.dumps(summary, indent=2) + "\n")

    print(result.to_text(), end="")
    print(f"results     : {vtu}")
    if frame_paths:
        print(f"frames      : {frame_paths[0].name} .. {frame_paths[-1].name}")
    print(f"summary     : {summary_path}")
    return 0


def _cmd_solve(args: argparse.Namespace) -> int:
    if args.deck.suffix.lower() == ".bdf":
        return _cmd_solve_mystran(args)
    if args.deck.suffix.lower() == ".k":
        print(
            "error: no LS-DYNA solver is wired — .k decks are format-level "
            "exports only (D29 pattern; validate them in your LS-DYNA "
            "environment)",
            file=sys.stderr,
        )
        return 1
    if args.deck.suffix.lower() == ".cdb":
        print(
            "error: no ANSYS solver is wired — .cdb decks are format-level "
            "exports only (D29 pattern; validate them in your ANSYS "
            "environment)",
            file=sys.stderr,
        )
        return 1
    import json

    from meshforge.export import guard_output_path
    from meshforge.io import file_sha256
    from meshforge.solve import frd_to_grid, solve_deck

    deck: Path = args.deck
    out_dir: Path = args.outputs / "solve" / deck.stem

    try:
        checksum_before = file_sha256(deck)
        result = solve_deck(deck, out_dir, ccx=args.ccx, timeout=args.timeout)
        grid = frd_to_grid(result.frd)
        vtu = out_dir / f"{deck.stem}_results.vtu"
        guard_output_path(vtu, input_paths=[deck])
    except (FileNotFoundError, TypeError, ValueError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if file_sha256(deck) != checksum_before:
        print(f"error: deck changed on disk during the solve: {deck}", file=sys.stderr)
        return 1

    grid.save(vtu)
    summary = result.summary()
    summary["results_vtu"] = str(vtu)
    summary_path = out_dir / f"{deck.stem}_solve.json"
    summary_path.write_text(json.dumps(summary, indent=2) + "\n")

    print(result.to_text(), end="")
    print(f"results     : {vtu}")
    print(f"summary     : {summary_path}")
    return 0


def _cmd_copilot(args: argparse.Namespace) -> int:
    import json
    import time as _time

    from meshforge.copilot import HttpBackend, SessionBackend, render_report, run_copilot
    from meshforge.copilot.report import shown_path, shown_paths
    from meshforge.copilot.tools import PipelineTools
    from meshforge.pipeline import check_flow_metrics

    flow = None
    if args.flow_metrics is not None:  # checked before any API call is spent
        try:
            flow = check_flow_metrics(json.loads(args.flow_metrics.read_text()))
        except (OSError, ValueError) as exc:
            # an OSError's own text repeats the full path; its reason does not
            why = (exc.strerror or type(exc).__name__) if isinstance(exc, OSError) else exc
            print(f"error: --flow-metrics {args.flow_metrics.name}: {why}", file=sys.stderr)
            return 1
    flow_case: Path | None = args.flow_case
    if flow_case is not None and not (flow_case / "system" / "controlDict").is_file():
        print(f"error: --flow-case {flow_case.name}: not an OpenFOAM case (no system/controlDict)",
              file=sys.stderr)
        return 1
    out_dir: Path = args.out_dir or (args.outputs / "copilot" / _time.strftime("%Y%m%d-%H%M%S"))
    out_dir.mkdir(parents=True, exist_ok=True)
    request = args.request + (f"\nCT: {args.ct}" if args.ct is not None else "")
    # D112: the Workbench this run drives — already up (emptied on --reset), or
    # started here with its window, so one terminal command runs the demo.
    workbench = None
    if args.server:
        from meshforge.copilot import launch

        if launch.health(args.server):
            if args.reset:
                try:
                    launch.reset_workbench(args.server)
                except RuntimeError as exc:
                    print(f"error: {exc}", file=sys.stderr)
                    return 1
                print(f"workbench   : running at {args.server}; session emptied for this run",
                      flush=True)
            else:
                print(f"workbench   : running at {args.server}", flush=True)
        elif args.launch:
            try:
                workbench, secs = launch.start_workbench(args.server, out_dir / "workbench.log")
            except (RuntimeError, ValueError) as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 1
            where = launch.open_window(args.server)
            print(f"workbench   : started at {args.server} in {secs:.1f} s (pid {workbench.pid}, "
                  f"log {shown_path(out_dir / 'workbench.log')}); window opened in the {where}",
                  flush=True)
    backend = HttpBackend(args.server) if args.server else SessionBackend()
    tools = PipelineTools(backend, out_dir / "work")
    print(f"copilot     : {args.model} (effort {args.effort}) on the {backend.name}", flush=True)
    t0 = _time.perf_counter()
    try:
        run = run_copilot(request, tools=tools, model=args.model, effort=args.effort,
                          max_turns=args.max_turns,
                          log=lambda m: print(m, flush=True))
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        if workbench is not None:
            print(f"workbench   : still running at {args.server} (pid {workbench.pid}; stop with: "
                  f"kill {workbench.pid})")
        return 1
    flow_error = ""
    flow_note = ""
    if run.complete and (args.flow_live or flow_case is not None):
        # D112: the blood flow in the SAME session as the stress, journaled as
        # `cfd` before the metrics below so the twin reads both. --flow-live
        # builds the case from this crop's own lumen; --flow-case re-solves a
        # given case (the fallback when the live build fails, or alone).
        from meshforge.copilot import launch
        from meshforge.copilot.backend import StepOutcome

        def solve(case_dir: Path, what: str) -> tuple[StepOutcome, float]:
            t1 = _time.perf_counter()
            try:
                cfd = backend.run("cfd", {"case": str(case_dir)})
            except RuntimeError as exc:  # the Workbench is gone
                cfd = StepOutcome(False, {}, "", str(exc))
            secs = _time.perf_counter() - t1
            if cfd.ok:
                solver = cfd.summary.get("solver") or "simpleFoam"
                cells = cfd.summary.get("n_cells")
                return cfd, secs, (f"{what}: OpenFOAM {solver}"
                                   + (f", {cells} cells" if cells else "") + f", {secs:.0f} s")
            return cfd, secs, ""

        cfd = None
        if args.flow_live:
            from meshforge.copilot import flow as liveflow

            try:
                built = liveflow.build_lumen_case(tools.crop_lumen, out_dir / "work" / "flow_case_live")
                print(f"flow        : this crop's blood channel filled: {built['n_cells']} cells, "
                      f"inflow at the neck {built['inflow_speed_mm_s']} mm/s for {built['q_lmin']:g} L/min",
                      flush=True)
                cfd, _secs, flow_note = solve(Path(built["case"]), "this crop's blood channel solved live")
                if not cfd.ok:
                    flow_error = f"the live flow case did not run: {cfd.error}"
            except (OSError, ValueError, RuntimeError) as exc:
                flow_error = f"the live flow case could not be built: {exc}"
            if flow_error:
                print(f"error: {flow_error}", file=sys.stderr)
        if (cfd is None or not cfd.ok) and flow_case is not None:
            try:
                staged = launch.stage_case(flow_case, out_dir / "work" / "flow_case")
                cfd, _secs, flow_note = solve(staged, "the scan's pre-built lumen case re-solved live")
                if cfd.ok:
                    flow_error = ""  # the fallback carried the flow
                else:
                    flow_error = f"the flow case did not run: {cfd.error}"
            except (OSError, ValueError, RuntimeError) as exc:
                flow_error = f"the flow case could not be staged: {exc}"
            if flow_error:
                print(f"error: {flow_error}", file=sys.stderr)
        if flow_note:
            print(f"flow        : {flow_note}", flush=True)
    if flow is not None:
        # the session journals the metrics as its own flow_metrics record
        # BEFORE the report is written (and before session.mfwork is saved),
        # so the Workbench panel and the report print the same flow lines
        try:
            attached = backend.attach_flow_metrics(flow)
            attach_error = "" if attached.ok else attached.error
        except RuntimeError as exc:  # the Workbench is gone
            attach_error = str(exc)
        if attach_error:
            flow_error = "; ".join(filter(None, (flow_error, attach_error)))
            print(f"error: the flow metrics are not in the session (its twin panel will not "
                  f"show them): {attach_error}", file=sys.stderr)
        else:
            print(f"flow        : {len(flow)} pre-computed CFD metrics journaled in the session",
                  flush=True)
    replay = ""
    if isinstance(backend, SessionBackend):
        try:
            wf = backend.session.save_workfile(out_dir / "session.mfwork")
            replay = f"meshforge replay {shown_path(wf)}"  # no local folder on screen
        except ValueError as exc:  # an empty journal (no step landed): nothing to replay
            print(f"replay      : none ({exc})", flush=True)
    md, page = render_report(
        request=args.request, ct_name=args.ct.name if args.ct else "(in the request)",
        credit=args.credit, results=run.results, calls=run.calls,
        summary_text=run.summary_text, guard_note=run.guard_note, model=args.model,
        flow=flow, replay_hint=replay, flow_note=flow_note,
    )
    (out_dir / "report.md").write_text(md)
    (out_dir / "report.html").write_text(page)
    # the transcript names the CT (and the run folder) without the local folder
    # around them; replay reads session.mfwork, which keeps the real paths
    record = json.loads(json.dumps(
        {"request": request, "model": args.model, "stop": run.stop, "usage": run.usage,
         "guard": run.guard_note, "calls": run.calls, "transcript": run.transcript}, default=str))
    (out_dir / "transcript.json").write_text(
        json.dumps(shown_paths(record, [args.ct, out_dir]), indent=1) + "\n")
    print(f"stop        : {run.stop}; {run.guard_note}")
    print(f"tokens      : {run.usage.get('input_tokens', 0)} in, "
          f"{run.usage.get('output_tokens', 0)} out; {_time.perf_counter() - t0:.0f} s")
    if run.summary_text:
        print("summary     :\n" + run.summary_text)
    print(f"report      : {shown_path(out_dir / 'report.html')}")
    print(f"transcript  : {shown_path(out_dir / 'transcript.json')}")
    if replay:
        print(f"replay      : {replay}")
    open_report = args.open_report if args.open_report is not None else sys.stdout.isatty()
    if open_report:
        from meshforge.copilot import launch

        where = launch.open_file(out_dir / "report.html")
        print(f"opened      : report.html in the {where}")
    if workbench is not None:
        print(f"workbench   : still running at {args.server} (pid {workbench.pid}; stop with: "
              f"kill {workbench.pid})")
    return 0 if run.complete and not flow_error else 1


def _cmd_aaa_stress(args: argparse.Namespace) -> int:
    import json

    from meshforge.export import guard_output_path
    from meshforge.io import file_sha256
    from meshforge.solve.aaa_metrics import (
        crop_report_from_journal,
        read_wall,
        run_aaa_stress,
    )

    src: Path = args.wall
    stem = src.stem
    out_dir: Path = args.out_dir or (args.outputs / "aaa_stress" / stem)
    vtu = out_dir / f"{stem}_aaa_stress.vtu"
    json_path = out_dir / f"{stem}_aaa_stress.json"
    try:
        if not src.is_file():
            raise FileNotFoundError(f"wall input not found: {src}")
        inputs = [src, args.crop_report, args.lumen, args.sac]
        if src.suffix.lower() == ".mfwork":
            if args.summary is not None:
                raise ValueError("--summary applies to a wall .vtu, not a .mfwork session")
            from meshforge.pipeline import load_workfile, replay

            session = load_workfile(src)
            print(f"replay      : {src.name} ({len(session.journal)} steps)", flush=True)
            replay(session)
            wall = session._require(session.volmesh, "volmesh", "a volmesh step in the session")
            summary = wall.summary()
            crop = crop_report_from_journal(session.journal)
        else:
            summary_path = args.summary or src.with_suffix(".summary.json")
            inputs.append(summary_path)
            wall, summary = read_wall(src, summary_path)
            crop = None
        if args.crop_report is not None:
            crop = json.loads(args.crop_report.read_text())
        for target in (vtu, json_path):
            guard_output_path(target, input_paths=inputs)
        sha = {p: file_sha256(p) for p in inputs if p is not None and Path(p).is_file()}
        lumen = sac = None
        if args.lumen is not None or args.sac is not None:
            from meshforge.io import load_surface

            lumen = load_surface(args.lumen) if args.lumen is not None else None
            sac = load_surface(args.sac) if args.sac is not None else None
        print(f"aaa-stress  : {src.name}, {args.systolic:g} and {args.whatif:g} mmHg"
              + (f", thrombus between {args.lumen.name} and the wall" if lumen is not None else ""),
              flush=True)
        result = run_aaa_stress(
            wall, summary, out_dir=out_dir, stem=stem, crop_report=crop,
            systolic_mmhg=args.systolic, whatif_mmhg=args.whatif, E=args.E, nu=args.nu,
            end_zone=args.end_zone, ccx=args.ccx, timeout=args.timeout, input_path=src,
            lumen=lumen, sac=sac, E_ilt=args.E_ilt, ilt_size=args.ilt_size,
        )
    except (FileNotFoundError, TypeError, ValueError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    for p, h in sha.items():
        if file_sha256(p) != h:
            print(f"error: input changed on disk during the run: {p}", file=sys.stderr)
            return 1
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(result.summary, indent=2) + "\n")
    print(result.to_text(), end="")
    if result.grid is not None:
        result.grid.save(vtu)
        print(f"results     : {vtu}")
    print(f"summary     : {json_path}")
    if result.refused:
        print("error: no wall-stress metric reported (REFUSED, see above)", file=sys.stderr)
        return 1
    return 0


def _cmd_solve_mystran(args: argparse.Namespace) -> int:
    import json

    from meshforge.export import guard_output_path
    from meshforge.io import file_sha256
    from meshforge.solve import solve_bdf

    deck: Path = args.deck
    out_dir: Path = args.outputs / "solve" / deck.stem

    try:
        checksum_before = file_sha256(deck)
        result = solve_bdf(
            deck, out_dir, mystran=args.mystran, timeout=args.timeout
        )
        # Rebuild the mesh from the bdf (meshio read-back was V15's exactness
        # check) and attach the F06 displacements as point data.
        import meshio
        import numpy as np
        import pyvista as pv

        mesh = meshio.read(deck, file_format="nastran")
        grid = pv.from_meshio(mesh)
        if grid.n_points != len(result.grid_ids):
            raise RuntimeError(
                f"F06 grid count {len(result.grid_ids)} != bdf point count "
                f"{grid.n_points} — deck and results disagree"
            )
        grid.point_data["U"] = result.displacements
        grid.point_data["U_mag"] = np.linalg.norm(result.displacements, axis=1)
        grid.point_data.active_scalars_name = "U_mag"
        vtu = out_dir / f"{deck.stem}_results.vtu"
        guard_output_path(vtu, input_paths=[deck])
    except (FileNotFoundError, TypeError, ValueError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if file_sha256(deck) != checksum_before:
        print(f"error: deck changed on disk during the solve: {deck}", file=sys.stderr)
        return 1

    grid.save(vtu)
    summary = result.summary()
    summary["results_vtu"] = str(vtu)
    summary_path = out_dir / f"{deck.stem}_solve.json"
    summary_path.write_text(json.dumps(summary, indent=2) + "\n")

    print(result.to_text(), end="")
    print(f"results     : {vtu}")
    print(f"summary     : {summary_path}")
    return 0


def _cmd_volmesh(args: argparse.Namespace) -> int:
    import json

    from meshforge.export import guard_output_path
    from meshforge.io import file_sha256, load_surface
    from meshforge.meshing import get_backend

    surface_path: Path = args.surface
    out: Path = args.out or (args.outputs / f"{surface_path.stem}_wall_{args.backend}.vtu")

    try:
        # Trap #5 precedence: exactly one thickness source, loudly.
        if args.thickness is not None and args.outer_surface is not None:
            raise ValueError(
                "--thickness and --outer-surface are mutually exclusive; "
                "drop --thickness to use the measured outer surface"
            )
        if args.thickness is None and args.outer_surface is None:
            raise ValueError("give either --thickness or --outer-surface")

        checksum_before = file_sha256(surface_path)
        mesh = load_surface(surface_path)
        if args.outer_surface is not None:
            from meshforge.geometry import ThicknessSpec

            t_arg = ThicknessSpec.from_outer_surface(load_surface(args.outer_surface))
        else:
            t_arg = args.thickness
        backend = get_backend(args.backend)
        kwargs: dict = {
            "n_theta": args.n_theta,
            "n_axial": args.n_axial,
        }
        if args.backend == "swept":
            kwargs["n_layers"] = args.n_layers
            kwargs["layer_ratio"] = args.layer_ratio
            kwargs["curvature_cap"] = args.curvature_cap
            kwargs["sweep"] = args.sweep
            kwargs["side"] = args.side
            kwargs["max_tilt"] = args.max_tilt
            kwargs["section_smoothing"] = args.section_smoothing
        elif (args.sweep != "centreline" or args.side != "outward"
              or args.max_tilt is not None or args.section_smoothing):
            raise ValueError("--sweep/--side/--max-tilt/--section-smoothing are swept-only")
        if args.backend == "tet" and args.mesh_size is not None:
            kwargs["mesh_size"] = args.mesh_size
        if args.backend == "tet" and args.no_optimize:
            kwargs["optimize"] = False
        wall = backend.mesh_wall(mesh, t_arg, **kwargs)
        guard_output_path(out, input_paths=[surface_path, args.outer_surface])
    except (FileNotFoundError, TypeError, ValueError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if file_sha256(surface_path) != checksum_before:
        print(f"error: input changed on disk during meshing: {surface_path}", file=sys.stderr)
        return 1

    from meshforge.solve.aaa_metrics import annotate_wall_thickness

    annotate_wall_thickness(wall)  # D93: the thickness along the surface normal
    out.parent.mkdir(parents=True, exist_ok=True)
    wall.grid.save(out)
    summary = wall.summary()
    summary_path = out.with_suffix(".summary.json")
    summary_path.write_text(json.dumps(summary, indent=2) + "\n")

    q = summary["quality"]
    print(f"backend     : {summary['backend']}")
    print(f"cells       : {summary['n_cells']}  points: {summary['n_points']}")
    print(f"boundary    : {summary['boundary_faces']}")
    print(
        f"quality     : inverted={q['n_inverted']}  "
        f"sj_min={q['sj_min']:.4g}  sj_mean={q['sj_mean']:.4g}  "
        f"volume={q['total_volume']:.6g}"
    )
    if "lattice" in summary:
        lat = summary["lattice"]
        print(
            f"wall rays   : capped={lat['n_capped']}  floored={lat['n_floored']}  of "
            f"{lat['n_rays']} (curvature_cap {lat['curvature_cap']:g}); thickness "
            f"{lat['thickness_min']:.4g}..{lat['thickness_max']:.4g}"
        )
        if lat.get("thickness_normal_median") is not None:
            print(f"wall normal : median thickness {lat['thickness_normal_median']:.4g} along the "
                  f"surface normal")
        if lat.get("sweep") == "planes":
            print(
                f"plane sweep : side={lat['side']}  capped_tilt={lat['n_capped_tilt']}  "
                f"capped_concave={lat['n_capped_concave']}  tilt_max={lat['tilt_max_deg']:.1f} "
                f"deg (bound {lat['max_tilt_deg']:g})  section_smoothing="
                f"{lat['section_smoothing']:g}  nodes_off_surface max="
                f"{lat['surface_offset_max']:.3g} p95={lat['surface_offset_p95']:.3g}"
            )
    for note in summary["notes"]:
        print(f"note        : {note}")
    print(f"mesh        : {out}")
    print(f"summary     : {summary_path}")
    return 0 if q["n_inverted"] == 0 else 1


def _cmd_lumen(args: argparse.Namespace) -> int:
    import json

    from meshforge.export import guard_output_path
    from meshforge.io import file_sha256, load_surface
    from meshforge.meshing.lumen import fill_lumen

    surface_path: Path = args.surface
    out: Path = args.out or (args.outputs / f"{surface_path.stem}_lumen.vtu")

    try:
        checksum_before = file_sha256(surface_path)
        mesh = load_surface(surface_path)
        lumen = fill_lumen(
            mesh,
            method=args.method,
            n_theta=args.n_theta,
            n_axial=args.n_axial,
            core_fraction=args.core_fraction,
            smooth=args.smooth,
            mesh_size=args.mesh_size,
        )
        guard_output_path(out, input_paths=[surface_path])
    except (FileNotFoundError, TypeError, ValueError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if file_sha256(surface_path) != checksum_before:
        print(f"error: input changed on disk during meshing: {surface_path}", file=sys.stderr)
        return 1

    out.parent.mkdir(parents=True, exist_ok=True)
    lumen.grid.save(out)
    summary = lumen.summary()
    summary_path = out.with_suffix(".summary.json")
    summary_path.write_text(json.dumps(summary, indent=2) + "\n")

    q = summary["quality"]
    print(f"backend     : {summary['backend']}")
    print(f"cells       : {summary['n_cells']}  points: {summary['n_points']}")
    print(f"boundary    : {summary['boundary_faces']}")
    print(
        f"quality     : inverted={q['n_inverted']}  "
        f"sj_min={q['sj_min']:.4g}  sj_mean={q['sj_mean']:.4g}  "
        f"volume={q['total_volume']:.6g}"
    )
    for note in summary["notes"]:
        print(f"note        : {note}")
    print(f"mesh        : {out}")
    print(f"summary     : {summary_path}")
    return 0 if q["n_inverted"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

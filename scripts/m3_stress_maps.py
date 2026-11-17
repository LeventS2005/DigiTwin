"""M3 (D88) video stills: wall-stress maps from an ``aaa-stress`` result.

Reads only digitwin's own outputs (the results .vtu and summary JSON that
``digitwin aaa-stress`` wrote) and renders off-screen with pyvista:

* ``<out>_140.png`` — the outer wall coloured by von Mises stress at the
  systolic pressure, the limits of the (unscored) clamped end zones drawn as
  black lines, the peak marked;
* ``<out>_140_vs_120.png`` — systolic and what-if side by side on one scale.

Run from the repository root:

    python -I scripts/m3_stress_maps.py outputs/m3/anev02/<stem>_aaa_stress.vtu \
        outputs/m3/anev02_stress --label "AortaSeg-60 Anev_02 (CC BY 4.0)"
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np

CAPTION = ("investigational, not a diagnosis - wall thickness assumed 1.5 mm - "
           "no thrombus modelled")


def _surface(grid, field: str):
    surf = grid.extract_surface(algorithm="dataset_surface")
    surf.point_data["stress_kpa"] = np.asarray(surf.point_data[field], dtype=float) * 1000.0
    return surf


def _zone_lines(surf, summ):
    """Where each end zone stops: the surface cut by the plane at the zone's
    length from its cap."""
    lines = []
    for cap in summ["end_zones"]["caps"].values():
        n = np.asarray(cap["normal_in"], float)
        o = np.asarray(cap["centroid_mm"], float) + cap["end_zone_mm"] * n
        cut = surf.slice(normal=n, origin=o)
        if cut.n_points:
            lines.append(cut)
    return lines


def _camera(plotter, grid, summ):
    c = np.asarray(grid.center)
    d = 3.2 * float(np.ptp(np.asarray(grid.points), axis=0).max())
    up = np.asarray(summ["end_zones"]["caps"]["distal"]["normal_in"], float)  # distal -> proximal
    view = np.array([0.0, 1.0, 0.0]) - up[1] * up  # RAS anterior, square to the axis
    view /= np.linalg.norm(view)
    plotter.camera_position = [tuple(c + d * view), tuple(c), tuple(up)]


def render(vtu: Path, out_stem: Path, label: str) -> list[Path]:
    import pyvista as pv

    summ = json.loads(vtu.with_suffix(".json").read_text())
    grid = pv.read(str(vtu))
    s, w = summ["systolic"], summ["whatif"]
    vmax = float(np.percentile(_surface(grid, "von_mises").point_data["stress_kpa"], 99.5))
    clim = [0.0, round(vmax, -1)]
    bar = dict(title="von Mises stress (kPa)", vertical=True, fmt="%.0f", n_labels=5,
               position_x=0.86, position_y=0.2, height=0.6)
    written = []

    p = pv.Plotter(off_screen=True, window_size=(1200, 1400))
    p.set_background("white")
    surf = _surface(grid, "von_mises")
    p.add_mesh(surf, scalars="stress_kpa", cmap="inferno", clim=clim,
               scalar_bar_args=bar, smooth_shading=True)
    for line in _zone_lines(surf, summ):
        p.add_mesh(line, color="black", line_width=4)
    if s.get("peak"):
        p.add_mesh(pv.Sphere(radius=2.5, center=s["peak"]["location_mm"]), color="cyan")
    p.add_text(f"Wall stress at {s['pressure_mmhg']:g} mmHg - {label}", font_size=14,
               color="black", position="upper_left")
    if s.get("von_mises_p99_mpa") is not None:
        p.add_text(f"peak wall stress (99th pct) {s['pws_kpa']:.0f} kPa\n"
                   f"black lines: limits of the clamped end zones (not scored)\n"
                   f"cyan: highest stress outside them", font_size=11,
                   color="black", position="upper_right")
    p.add_text(CAPTION, font_size=9, color="dimgray", position="lower_left")
    _camera(p, grid, summ)
    out1 = out_stem.with_name(f"{out_stem.name}_{s['pressure_mmhg']:g}.png")
    p.screenshot(str(out1))
    p.close()
    written.append(out1)

    p = pv.Plotter(off_screen=True, window_size=(2000, 1300), shape=(1, 2))
    p.set_background("white")
    for col, (blk, field) in enumerate(((s, "von_mises"), (w, "von_mises_whatif"))):
        p.subplot(0, col)
        surf = _surface(grid, field)
        p.add_mesh(surf, scalars="stress_kpa", cmap="inferno", clim=clim, smooth_shading=True,
                   show_scalar_bar=col == 1, scalar_bar_args=bar)
        for line in _zone_lines(surf, summ):
            p.add_mesh(line, color="black", line_width=4)
        pws = "" if blk.get("pws_kpa") is None else f": PWS {blk['pws_kpa']:.0f} kPa"
        p.add_text(f"{blk['pressure_mmhg']:g} mmHg{pws}", font_size=16, color="black",
                   position="upper_left")
        _camera(p, grid, summ)
    p.link_views()
    p.subplot(0, 0)
    ratio = (summ.get("whatif_ratio") or {}).get("pws")
    if ratio is not None:
        p.add_text(f"what-if: {w['pressure_mmhg']:g} instead of {s['pressure_mmhg']:g} mmHg "
                   f"-> PWS x{ratio:.3f}", font_size=11, color="black", position="upper_right")
    p.add_text(CAPTION + f"  |  {label}", font_size=9, color="dimgray", position="lower_left")
    out2 = out_stem.with_name(
        f"{out_stem.name}_{s['pressure_mmhg']:g}_vs_{w['pressure_mmhg']:g}.png")
    p.screenshot(str(out2))
    p.close()
    written.append(out2)
    return written


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("vtu", type=Path, help="<stem>_aaa_stress.vtu from 'digitwin aaa-stress'")
    ap.add_argument("out_stem", type=Path, help="output path stem, e.g. outputs/m3/anev02_stress")
    ap.add_argument("--label", default="", help="data credit shown on the image")
    args = ap.parse_args()
    if not str(args.out_stem).startswith("outputs"):
        raise SystemExit("write under outputs/ only")
    args.out_stem.parent.mkdir(parents=True, exist_ok=True)
    for path in render(args.vtu, args.out_stem, args.label):
        print(path)


if __name__ == "__main__":
    main()

"""M6 video still: wall shear stress (WSS) on the s1364 CFD lumen.

Reads an OpenFOAM case that already holds a ``wallShearStress`` field (run
``simpleFoam -postProcess -func wallShearStress -latestTime`` on a COPY of
the case first) and writes, next to ``<out>``:

* ``<out>.png`` — 16:9 still: the lumen surface coloured by |WSS| in Pa
  (log scale; anterior and left-lateral views) beside the area-weighted WSS
  distribution;
* ``<out>_render.png`` — the two 3-D views alone;
* ``<out>.json`` — the summary numbers with units, and how they were made;
* ``<out>_wall.vtp`` — the wall with ``wss_pa`` per face, for other tools.

Units: the case is in mm and mm/s with the kinematic viscosity nu in mm^2/s,
so OpenFOAM's (kinematic) wallShearStress is in mm^2/s^2. Times the blood
density rho (kg/m^3) and 1e-6 (mm^2 -> m^2) it is a stress in Pa. The
default rho = 1060 kg/m^3 with the case's nu = 3.3 mm^2/s is a Newtonian
blood viscosity of 3.5 mPa*s.

The surface is the blood-flow lumen: inside the sac that is the thrombus's
luminal face, not the aortic wall. "Dilated" faces: the lumen is cut into
2 mm slabs along the axis between the two cap centroids; a slab whose
equivalent diameter (twice the mean distance of its faces to the slab
centre) exceeds ``--dilated-mm`` counts as the dilated segment.
The low-WSS line (0.4 Pa) is the threshold of Malek, Alper & Izumo,
JAMA 282:2035 (1999). Descriptive numbers only: no clinical meaning is
claimed for them here.

Run from the repository root:

    python -I scripts/m6_wss_map.py outputs/m6/wss/s1364_case outputs/m6/wss/s1364_wss \
        --dilated-mm 28.15 --label "TotalSegmentator dataset s1364 (Wasserthal et al.), CC BY 4.0"
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np

LOW_WSS_PA = 0.4
CAPTION = "steady resting flow (1 L/min), rigid wall, Newtonian blood - descriptive, not a diagnosis"


def _wquantile(x: np.ndarray, w: np.ndarray, q: float) -> float:
    """Area-weighted quantile (q in 0..1)."""
    order = np.argsort(x)
    cw = np.cumsum(w[order])
    return float(np.interp(q * cw[-1], cw, x[order]))


def _stats(wss: np.ndarray, area: np.ndarray) -> dict:
    return {
        "area_mm2": round(float(area.sum()), 1),
        "mean_pa": round(float(np.average(wss, weights=area)), 3),
        "p5_pa": round(_wquantile(wss, area, 0.05), 3),
        "p50_pa": round(_wquantile(wss, area, 0.50), 3),
        "p95_pa": round(_wquantile(wss, area, 0.95), 3),
        "max_pa": round(float(wss.max()), 3),
        f"area_below_{LOW_WSS_PA}_pa_percent": round(100.0 * float(area[wss < LOW_WSS_PA].sum() / area.sum()), 1),
    }


def case_nu(case: Path) -> float:
    """The case's kinematic viscosity (constant/transportProperties), mm^2/s."""
    import re

    text = (case / "constant" / "transportProperties").read_text()
    m = re.search(r"^\s*nu\s+(?:\[[^\]]*\]\s*)?([0-9.eE+-]+)\s*;", text, re.M)
    if not m:
        raise SystemExit("no 'nu' in constant/transportProperties")
    return float(m.group(1))


def load(case: Path, rho: float, inflow_patch: str):
    import pyvista as pv

    foam = case / "case.foam"
    if not foam.is_file():
        foam.touch()  # the reader opens the case through an (empty) .foam file
    reader = pv.OpenFOAMReader(str(foam))
    t = reader.time_values[-1]
    reader.set_active_time_value(t)
    reader.enable_all_patch_arrays()
    blocks = reader.read()["boundary"]
    names = list(blocks.keys())
    if "wall" not in names or inflow_patch not in names:
        raise SystemExit(f"patches {names}: need 'wall' and the inflow patch {inflow_patch!r}")
    wall = blocks["wall"]
    if "wallShearStress" not in wall.cell_data:
        raise SystemExit("no wallShearStress on the wall: run the wallShearStress function object first")
    if not isinstance(wall, pv.PolyData):
        wall = wall.extract_surface(algorithm="dataset_surface")
    tau = np.asarray(wall.cell_data["wallShearStress"], float)  # mm^2/s^2 (kinematic)
    wall.cell_data["wss_pa"] = rho * 1e-6 * np.linalg.norm(tau, axis=1)
    caps = {n: np.asarray(blocks[n].center, float) for n in names if n != "wall"}
    return wall, caps, float(t)


def regions(wall, caps: dict, inflow_patch: str, dilated_mm: float | None, slab_mm: float = 2.0):
    """(dilated mask, per-face slab diameter) along the cap-to-cap axis."""
    neck = caps[inflow_patch]
    distal = next(c for n, c in caps.items() if n != inflow_patch)
    axis = (neck - distal) / np.linalg.norm(neck - distal)
    cen = np.asarray(wall.cell_centers().points, float)
    area = np.asarray(wall.compute_cell_sizes(length=False, volume=False).cell_data["Area"], float)
    s = (cen - distal) @ axis
    slab = np.floor(s / slab_mm).astype(int)
    diam = np.zeros(len(cen))
    for k in np.unique(slab):
        m = slab == k
        c = np.average(cen[m], axis=0, weights=area[m])
        r = cen[m] - c
        r -= np.outer(r @ axis, axis)  # in-plane distance only
        diam[m] = 2.0 * np.average(np.linalg.norm(r, axis=1), weights=area[m])
    dilated = diam > dilated_mm if dilated_mm else np.zeros(len(cen), bool)
    return dilated, diam, area, axis


def _trim(img: np.ndarray, pad: int = 12) -> np.ndarray:
    """Crop the white margin around a render."""
    ink = np.where((img[..., :3] < 250).any(axis=2))
    if not ink[0].size:
        return img
    y0, y1 = max(ink[0].min() - pad, 0), min(ink[0].max() + pad, img.shape[0])
    x0, x1 = max(ink[1].min() - pad, 0), min(ink[1].max() + pad, img.shape[1])
    return img[y0:y1, x0:x1]


def render(wall, axis: np.ndarray, clim: tuple[float, float], path: Path, size=(1100, 1500)) -> list:
    """Anterior and left-lateral views (RAS: +y anterior, +x left), square to
    the vessel axis, each trimmed to its ink."""
    import pyvista as pv

    surf = wall.cell_data_to_point_data()
    c = np.asarray(surf.center)
    d = 2.6 * float(np.ptp(np.asarray(surf.points), axis=0).max())
    views = []
    for look in (np.array([0.0, 1.0, 0.0]), np.array([1.0, 0.0, 0.0])):
        p = pv.Plotter(off_screen=True, window_size=list(size))
        p.set_background("white")
        p.add_mesh(surf, scalars="wss_pa", cmap="plasma", clim=clim, log_scale=True,
                   smooth_shading=True, show_scalar_bar=False)
        view = look - (look @ axis) * axis
        view /= np.linalg.norm(view)
        p.camera_position = [tuple(c + d * view), tuple(c), tuple(axis)]
        views.append(_trim(p.screenshot(return_img=True)))
        p.close()
    h = max(v.shape[0] for v in views)
    padded = [np.pad(v, ((0, h - v.shape[0]), (0, 0), (0, 0)), constant_values=255) for v in views]
    gap = np.full((h, 40, padded[0].shape[2]), 255, dtype=padded[0].dtype)
    both = np.concatenate([padded[0], gap, padded[1]], axis=1)
    import matplotlib.image as mpimg

    mpimg.imsave(str(path), both)
    return views


def figure(views, wss, area, dilated, summary, clim, label: str, path: Path) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.colors import LogNorm

    plt.rcParams.update({"font.family": "sans-serif", "font.size": 13})
    fig = plt.figure(figsize=(16, 9), dpi=120, facecolor="white")
    fig.text(0.04, 0.93, "Wall shear stress where the blood flows", fontsize=24, weight="bold", color="#1a1f26")
    fig.text(0.04, 0.895, f"s1364 lumen surface (the thrombus face inside the sac) · {CAPTION}",
             fontsize=13, color="#5d6672")
    for i, (img, name) in enumerate(zip(views, ("anterior", "left lateral"))):
        ax0 = fig.add_axes([0.03 + i * 0.215, 0.11, 0.20, 0.74])
        ax0.imshow(img)
        ax0.set_title(name, fontsize=12, color="#5d6672")
        ax0.axis("off")
    cax = fig.add_axes([0.465, 0.20, 0.011, 0.52])
    sm = plt.cm.ScalarMappable(norm=LogNorm(*clim), cmap="plasma")
    cb = fig.colorbar(sm, cax=cax)
    cb.set_label("|WSS| (Pa)")
    ax = fig.add_axes([0.55, 0.42, 0.41, 0.42])
    bins = np.logspace(np.log10(clim[0]), np.log10(max(clim[1], wss.max())), 40)
    tot = area.sum()
    ax.hist([wss[dilated], wss[~dilated]], bins=bins, weights=[100 * area[dilated] / tot, 100 * area[~dilated] / tot],
            stacked=True, color=["#c8312b", "#9aa3ad"],
            label=[f"dilated lumen (> {summary['dilated_mm']:g} mm across)", "rest of the lumen (neck, distal end)"])
    ax.set_xscale("log")
    ax.set_ylim(0, ax.get_ylim()[1] * 1.45)  # headroom: the legend sits above the bars
    ax.axvline(LOW_WSS_PA, color="#1a1f26", lw=1.4, ls="--")
    ax.text(LOW_WSS_PA * 1.06, ax.get_ylim()[1] * 0.92, f"{LOW_WSS_PA} Pa", fontsize=12)
    ax.set_xlabel("|WSS| (Pa), log scale")
    ax.set_ylabel("% of lumen surface")
    ax.spines[["top", "right"]].set_visible(False)
    ax.legend(frameon=False, loc="upper left")
    sac, allw = summary["dilated_wall"], summary["whole_wall"]
    lines = [
        ("", "dilated lumen", "whole lumen"),
        ("median |WSS|", f"{sac['p50_pa']:.2f} Pa", f"{allw['p50_pa']:.2f} Pa"),
        ("5th–95th pct", f"{sac['p5_pa']:.2f}–{sac['p95_pa']:.2f} Pa", f"{allw['p5_pa']:.2f}–{allw['p95_pa']:.2f} Pa"),
        (f"area < {LOW_WSS_PA} Pa", f"{sac[f'area_below_{LOW_WSS_PA}_pa_percent']:.0f} %",
         f"{allw[f'area_below_{LOW_WSS_PA}_pa_percent']:.0f} %"),
        ("surface area", f"{sac['area_mm2'] / 100:.0f} cm²", f"{allw['area_mm2'] / 100:.0f} cm²"),
    ]
    for i, (a, b, c) in enumerate(lines):
        y = 0.31 - i * 0.045
        w = "bold" if i == 0 else "normal"
        fig.text(0.55, y, a, fontsize=14, color="#5d6672")
        fig.text(0.73, y, b, fontsize=14, weight=w, color="#c8312b" if i else "#1a1f26")
        fig.text(0.86, y, c, fontsize=14, weight=w, color="#1a1f26")
    fig.text(0.04, 0.045, label, fontsize=10.5, color="#8a939e")
    fig.text(0.04, 0.022, f"OpenFOAM simpleFoam + wallShearStress · rho {summary['rho_kg_m3']:g} kg/m³, "
             f"nu {summary['nu_mm2_s']:g} mm²/s ({summary['rho_kg_m3'] * summary['nu_mm2_s'] / 1000:.1f} mPa·s) · low-WSS line 0.4 Pa: Malek, Alper & Izumo, JAMA 1999",
             fontsize=10.5, color="#8a939e")
    fig.savefig(path, dpi=120, facecolor="white")
    plt.close(fig)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("case", type=Path)
    ap.add_argument("out", type=Path, help="output stem")
    ap.add_argument("--rho", type=float, default=1060.0, help="blood density, kg/m^3")
    ap.add_argument("--inflow-patch", default="outlet", help="the neck cap the flow enters by")
    ap.add_argument("--dilated-mm", type=float, default=None,
                    help="slab equivalent diameter above which the wall counts as sac")
    ap.add_argument("--label", default="")
    a = ap.parse_args(argv)

    wall, caps, t = load(a.case, a.rho, a.inflow_patch)
    dilated, diam, area, axis = regions(wall, caps, a.inflow_patch, a.dilated_mm)
    wss = np.asarray(wall.cell_data["wss_pa"], float)
    clim = (max(_wquantile(wss, area, 0.01), 1e-3), _wquantile(wss, area, 0.99))
    summary = {
        "what": "wall shear stress magnitude on the CFD lumen wall, area-weighted over wall faces",
        "units": {"wss": "Pa", "area": "mm^2"},
        "case": a.case.name, "time": t, "n_wall_faces": int(len(wss)),
        "rho_kg_m3": a.rho, "nu_mm2_s": case_nu(a.case),
        "conversion": "Pa = rho [kg/m^3] x 1e-6 x OpenFOAM kinematic wallShearStress [mm^2/s^2]",
        "inflow_patch": a.inflow_patch,
        "surface": "the CFD lumen wall (blood contact surface: the thrombus face inside the sac)",
        "dilated_mm": a.dilated_mm,
        "dilated_rule": (f"2 mm slabs along the cap-to-cap axis; slab equivalent diameter > {a.dilated_mm} mm"
                         if a.dilated_mm else None),
        "low_wss_threshold_pa": LOW_WSS_PA,
        "low_wss_source": "Malek AM, Alper SL, Izumo S. JAMA 1999;282(21):2035-42",
        "whole_wall": _stats(wss, area),
        "dilated_wall": _stats(wss[dilated], area[dilated]) if dilated.any() else None,
        "other_wall": _stats(wss[~dilated], area[~dilated]) if dilated.any() else None,
        "colour_range_pa": [round(clim[0], 4), round(clim[1], 3)],
    }
    a.out.parent.mkdir(parents=True, exist_ok=True)
    wall.cell_data["slab_diameter_mm"] = diam
    wall.cell_data["dilated"] = dilated.astype(np.uint8)
    wall.save(str(a.out.parent / f"{a.out.name}_wall.vtp"))
    views = render(wall, axis, clim, a.out.parent / f"{a.out.name}_render.png")
    if dilated.any():
        figure(views, wss, area, dilated, summary, clim, a.label, a.out.parent / f"{a.out.name}.png")
    (a.out.parent / f"{a.out.name}.json").write_text(json.dumps(summary, indent=1) + "\n")
    print(json.dumps({k: summary[k] for k in ("whole_wall", "dilated_wall", "other_wall")}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""M3 (D88) validation: our wall tension vs the dataset's published FE result.

Public data only: Zenodo AAA (doi 10.5281/zenodo.19182977, CC BY 4.0). Its
surfaces are a REFERENCE segmentation (not ground truth) and its
``tension.vtp`` maps are THE DATASET'S PUBLISHED FE RESULT (not ground truth):
linear elastic, wall E 1e5 kPa / ILT E 5e3 kPa, nu 0.49, C3D10H, nlgeom off,
caps encastre, 16 kPa (120 mmHg) on the thrombus' lumen surface; tension on
the outer wall, trimmed 3 mm at each end, N/mm.

Ours, per case, on the same wall geometry: the M2 gate wall — R_lumen_ilt
(the wall / thrombus interface, WallSurface patch 4) cropped by the D85
landmarks and given a 1.5 mm swept hex wall outward (sweep auto, cap 0.9);
rebuilt here with the gate's own calls and checked point-identical to
``outputs/m2/gate/<case>/wall.vtu``. Then the D88 solve at 0.016 MPa on the
wall's inner surface, both caps encastre, NO thrombus — so ours should read
higher where their thrombus carries load.

Comparison set: their tension vertices inside our crop and outside our end
zones (one neck diameter from each of our caps, the PWS rule; the CSV also
has the 3-bending-length and no-exclusion variants). Each vertex takes the
tension of our nearest outer-surface node. Per case: Spearman rho, the offset
between the centroids of their and our top-1 % (>= p99) vertex regions, and
our p99 / their p99 (and p50).

Run from the repository root (reads downloaded data: isolated mode):

    python -I scripts/m3_tension_validation.py [CASE ...]

Writes outputs/m3/tension_validation.{csv,md} and per-case logs/VTUs under
outputs/m3/tension_validation/.
"""

from __future__ import annotations

import csv
import json
import os
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path.cwd()
ZEN = ROOT / "outputs/data/zenodo_19182977/extracted/Dataset_root"
GATE = ROOT / "outputs/m2/gate"
OUT = ROOT / "outputs/m3"
#: MESHFORGE_TENSION_ILT=1: our model includes the thrombus as a tied solid
#: (D91), like theirs; results go to the *_ilt files beside the wall-only ones
WITH_ILT = os.environ.get("MESHFORGE_TENSION_ILT") == "1"
TAG = "_ilt" if WITH_ILT else ""
WORK = OUT / f"tension_validation{TAG}"
P_THEIRS_MPA = 0.016  # their *Dsload 16e-03 (MPa) = 120.0 mmHg
THICKNESS = 1.5
#: every Zenodo case whose M2 crop used the automatic landmarks and whose gate
#: wall passed (0 inverted / 0 capped / 0 floored) — chosen by rule, not by result
CASES = ("P01", "P04", "P06", "P07", "P09", "P10", "P11", "P12", "P13", "P14", "P15", "P17")
VARIANTS = ("neck", "bending", "none")


def gate_wall(case: str):
    """The M2 gate wall, rebuilt with the gate script's calls."""
    from meshforge.geometry import centerline_from_surface
    from meshforge.geometry.crop import crop_aaa
    from meshforge.io.zenodo_aaa import load_reference
    from meshforge.meshing import get_backend

    ref = load_reference(ZEN / case)
    outer = ref.regions["lumen_ilt"].triangulate()
    lumen = ref.regions["lumen"].triangulate()
    res = crop_aaa(outer, lumen=lumen)
    cl = centerline_from_surface(res.surface)
    wall = get_backend("swept").mesh_wall(res.surface, THICKNESS, centerline=cl,
                                          curvature_cap=0.9, sweep="auto",
                                          section_smoothing=0.0)
    return wall, res.report(), ref.regions["lumen"], res


def compare(case: str, log) -> list[dict]:
    import pyvista as pv
    from scipy.spatial import cKDTree
    from scipy.stats import spearmanr

    import meshforge.solve.aaa_metrics as am

    t0 = time.time()
    wall, rep, lumen_ref, crop_res = gate_wall(case)
    saved = pv.read(str(GATE / case / "wall.vtu"))
    same = (saved.n_points == wall.grid.n_points
            and float(np.abs(np.asarray(saved.points) - np.asarray(wall.grid.points)).max())
            < 1e-9)
    summ = json.loads(json.dumps(wall.summary()))
    refused = am.wall_refusals(wall, summ)
    log(f"{case}: wall {wall.grid.n_cells} hex, identical to the gate wall: {same}, "
        f"refusals {refused or 'none'} ({time.time() - t0:.1f} s)")
    if refused or not same:
        return [{"case": case, "variant": v, "status": "wall refused or not the gate wall"}
                for v in VARIANTS]

    cols = am._columns(wall)
    ilt = None
    if WITH_ILT:
        from meshforge.geometry.crop import planes_from_report
        from meshforge.solve.ilt import build_ilt_mesh

        ilt = build_ilt_mesh(crop_res.lumen, crop_res.surface, planes_from_report(rep))
        log(f"{case}: thrombus {len(ilt.tets)} C3D10, {ilt.volume_ml:.1f} mL")
    ip, _, res = am.solve_pressure(wall, P_THEIRS_MPA, WORK / case, f"{case}_16kPa",
                                   E=am.DEFAULT_E_MPA, nu=am.DEFAULT_NU, ilt=ilt)
    f = am.stress_fields(ip, cols, THICKNESS)
    t_pts = am._tension_points(cols, f.tension_col, wall.grid.n_points)
    log(f"{case}: solve {res.elapsed:.1f} s")

    outer_nodes = np.unique(wall.boundary["outer"].face_nodes)
    opts = np.asarray(wall.grid.points)[outer_nodes]
    theirs_mesh = pv.read(str(ZEN / case / "tension.vtp"))
    tv = np.asarray(theirs_mesh.points, dtype=float)
    tt = np.asarray(theirs_mesh.point_data["tension"], dtype=float)
    dist, idx = cKDTree(opts).query(tv)
    ours_at = t_pts[outer_nodes][idx]
    # their thrombus under each vertex: outer wall -> lumen distance minus the wall
    lum = lumen_ref.triangulate().subdivide_adaptive(max_edge_len=1.0)
    ilt = cKDTree(np.asarray(lum.points)).query(tv)[0] - THICKNESS

    rows = []
    for variant in VARIANTS:
        ez = am.end_zones(wall, end_zone="0" if variant == "none" else variant,
                          crop_report=rep, thickness=THICKNESS, volume=cols.volume)
        inside = np.ones(len(tv), dtype=bool)
        for cap in ez.caps:
            d = (tv - cap.centroid) @ cap.normal_in
            inside &= d >= cap.end_zone_mm
        # on our modelled wall: inside the crop and near our outer surface
        keep = inside & (dist < 3.0)
        n = int(keep.sum())
        row = {"case": case, "variant": variant, "n_vertices": n,
               "n_their_vertices": len(tv),
               "end_zone_mm": "/".join(f"{c.end_zone_mm:.1f}" for c in ez.caps),
               "nearest_node_mm_median": round(float(np.median(dist[keep])), 3) if n else None}
        if n < 200:
            row["status"] = f"too few vertices outside the end zones ({n})"
            rows.append(row)
            continue
        a, b = ours_at[keep], tt[keep]
        rho = float(spearmanr(a, b).statistic)
        rho_ilt = float(spearmanr(a / b, ilt[keep]).statistic)
        pa, pb = np.percentile(a, 99), np.percentile(b, 99)
        ca, cb = tv[keep][a >= pa].mean(axis=0), tv[keep][b >= pb].mean(axis=0)
        row.update(
            status="ok",
            spearman_rho=round(rho, 3),
            peak_offset_mm=round(float(np.linalg.norm(ca - cb)), 1),
            p99_ratio=round(float(pa / pb), 3),
            p50_ratio=round(float(np.median(a) / np.median(b)), 3),
            rho_ratio_vs_ilt=round(rho_ilt, 3),
            ilt_median_mm=round(float(np.median(ilt[keep])), 2),
            ours_p99=round(float(pa), 4), theirs_p99=round(float(pb), 4),
            ours_p50=round(float(np.median(a)), 4), theirs_p50=round(float(np.median(b)), 4),
        )
        rows.append(row)
        log(f"{case} [{variant}]: n {n}, rho {rho:.3f}, peak offset {row['peak_offset_mm']} mm, "
            f"p99 ratio {row['p99_ratio']}, p50 ratio {row['p50_ratio']}, rho(ratio, ILT) "
            f"{rho_ilt:.3f}")
        if variant == "neck":
            out = theirs_mesh.copy()
            out.point_data["tension_ours"] = ours_at
            out.point_data["compared"] = keep.astype(np.uint8)
            out.save(str(WORK / case / f"{case}_tension_compare.vtp"))
    return rows


def write_tables(rows: list[dict]) -> None:
    cols = ["case", "variant", "status", "n_vertices", "n_their_vertices", "end_zone_mm",
            "spearman_rho", "peak_offset_mm", "p99_ratio", "p50_ratio", "rho_ratio_vs_ilt",
            "ilt_median_mm", "ours_p99",
            "theirs_p99", "ours_p50", "theirs_p50", "nearest_node_mm_median"]
    with open(OUT / f"tension_validation{TAG}.csv", "w", newline="") as fh:
        wr = csv.DictWriter(fh, fieldnames=cols)
        wr.writeheader()
        for r in rows:
            wr.writerow({k: r.get(k) for k in cols})

    def med(variant, key):
        vals = [r[key] for r in rows if r["variant"] == variant and r.get("status") == "ok"]
        return (float(np.median(vals)), len(vals)) if vals else (None, 0)

    md = [
        "# M3 tension validation (D88): ours vs the dataset's published FE result",
        "",
        "Data: Zenodo AAA 4D-CTA dataset, doi 10.5281/zenodo.19182977, CC BY 4.0. Its "
        "surfaces are a reference segmentation and its `tension.vtp` maps are the "
        "dataset's published FE result - neither is ground truth.",
        "",
        "Theirs: wall (1.5 mm, E 100 MPa) + intraluminal thrombus (E 5 MPa), nu 0.49, "
        "C3D10H, linear, caps encastre, 16 kPa on the thrombus' lumen surface. Ours: the "
        "M2 gate wall (R_lumen_ilt cropped by the D85 landmarks, 1.5 mm hex wall outward = "
        "their wall), C3D20R, E 100 MPa, nu 0.49, linear, our crop caps encastre, 16 kPa "
        "on the wall's inner surface, NO thrombus. Each of their outer-wall vertices inside "
        "our crop takes the tension of our nearest outer node.",
        "",
        "Cases: every Zenodo case with automatic crop landmarks and a passing gate wall "
        f"({len(CASES)}), chosen by that rule before any result.",
        "",
        "| case | n vertices | end zones (mm) | Spearman rho | peak offset (mm) | "
        "p99 ours/theirs | p50 ours/theirs | thrombus median (mm) | rho(ratio, thrombus) |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        if r["variant"] != "neck":
            continue
        if r.get("status") != "ok":
            md.append(f"| {r['case']} | {r.get('n_vertices', '')} | {r.get('end_zone_mm', '')} "
                      f"| n/a: {r['status']} | | | | | |")
            continue
        md.append(f"| {r['case']} | {r['n_vertices']} | {r['end_zone_mm']} | "
                  f"{r['spearman_rho']:.3f} | {r['peak_offset_mm']:.1f} | {r['p99_ratio']:.3f} | "
                  f"{r['p50_ratio']:.3f} | {r['ilt_median_mm']:.1f} | {r['rho_ratio_vs_ilt']:.3f} |")
    md += ["", "Medians over the compared cases, per end-zone variant:", "",
           "| end zones | cases | median rho | median peak offset (mm) | median p99 ratio | "
           "median p50 ratio | median rho(ratio, thrombus) |", "|---|---|---|---|---|---|---|"]
    for v in VARIANTS:
        rho, k = med(v, "spearman_rho")
        if not k:
            md.append(f"| {v} | 0 | | | | | |")
            continue
        md.append(f"| {v} | {k} | {rho:.3f} | {med(v, 'peak_offset_mm')[0]:.1f} | "
                  f"{med(v, 'p99_ratio')[0]:.3f} | {med(v, 'p50_ratio')[0]:.3f} | "
                  f"{med(v, 'rho_ratio_vs_ilt')[0]:.3f} |")
    md += [
        "",
        "End zones: neck = one neck diameter from each of our caps (the PWS default); "
        "bending = 3 shell bending lengths; none = every vertex inside our crop. Their maps "
        "are clamped at THEIR ends, ours at our crop planes, so vertices near our caps are "
        "not like for like.",
        "",
        "Expected direction: ours reads higher where their thrombus carries part of the "
        "pressure load (we model none); the ratio is not a calibration. Their pressure acts "
        "on the lumen, so their wall carries about P x the lumen radius; ours carries P x "
        "the wall's inner radius. rho(ratio, thrombus) is the Spearman correlation, over "
        "the compared vertices, between our/their tension and the thrombus thickness under "
        "the vertex (outer wall to their lumen surface, minus the 1.5 mm wall).",
    ]
    (OUT / f"tension_validation{TAG}.md").write_text("\n".join(md) + "\n")


def main(argv: list[str]) -> None:
    cases = argv or list(CASES)
    WORK.mkdir(parents=True, exist_ok=True)
    rows_path = WORK / "rows.json"
    rows = json.loads(rows_path.read_text()) if rows_path.exists() and argv else []
    rows = [r for r in rows if r["case"] not in cases]
    with open(WORK / "log.txt", "a") as fh:
        def log(msg):
            print(msg, flush=True)
            fh.write(msg + "\n")
            fh.flush()

        for case in cases:
            (WORK / case).mkdir(parents=True, exist_ok=True)
            try:
                rows += compare(case, log)
            except Exception as exc:  # recorded per case, never fail-open
                log(f"{case}: FAILED {type(exc).__name__}: {exc}")
                rows += [{"case": case, "variant": v, "status": f"failed: {exc}"[:200]}
                         for v in VARIANTS]
            rows_path.write_text(json.dumps(rows, indent=1))
    order = {c: i for i, c in enumerate(CASES)}
    rows.sort(key=lambda r: (order.get(r["case"], 99), VARIANTS.index(r["variant"])))
    write_tables(rows)


if __name__ == "__main__":
    main(sys.argv[1:])

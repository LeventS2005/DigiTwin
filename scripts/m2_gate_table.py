"""M2 gate table (D85): crop every public AAA case and mesh wall + lumen.

Public data only:
* Zenodo AAA P01-P20 (doi 10.5281/zenodo.19182977, CC BY 4.0): the outer
  surface is the reference lumen + thrombus region (R_lumen_ilt), the blood
  lumen is R_lumen — a reference segmentation, not ground truth.
* AortaSeg-60 (doi 10.5281/zenodo.18147026, CC BY 4.0), header-corrected
  CTs: Anev_04 (POST-EVAR — stent graft in the sac; never a demo case) with
  its M1 TotalSegmentator mask, and Anev_02 (native AAA) with the saved
  TotalSegmentator v2.18 mask (lumen only on this case).

For every case: the gate variant (crop -> swept wall 1.5 mm outward ->
lumen O-grid) and, for Zenodo, the "as given" baseline (their own axial
end planes, default centreline, curvature cap 0.5). The gate wall (cap 0.9)
uses sweep="auto" (D87): the centreline sweep unless its wall inverts or is
capped/floored, then the parallel-plane sweep; the cap-0.5 counts stay the
centreline sweep's, for comparison. Anev_02's wall is built on its recovered
outer sac (outputs/m2/sac/Anev_02_sac.vtp, D86) cut by the same two planes,
with 4 mm section smoothing (the sac surface carries mm-scale axial
terraces); its unsmoothed result is kept in the row too. Writes
outputs/m2/gate_table.{csv,md}, outputs/m2/gate_rows.json and a log per
case under outputs/m2/gate/. Run from the repository root:

    python -I scripts/m2_gate_table.py [CASE ...]
"""

from __future__ import annotations

import csv
import json
import sys
import time
import traceback
from pathlib import Path

import numpy as np

ROOT = Path.cwd()
ZEN = ROOT / "outputs/data/zenodo_19182977/extracted/Dataset_root"
OUT = ROOT / "outputs/m2"
GATE = OUT / "gate"
THICKNESS = 1.5
CAPS = (0.5, 0.9)  # default curvature cap, and the D85-tuned one
GATE_CAP = 0.9


def log(fh, *parts):
    line = " ".join(str(p) for p in parts)
    print(line, flush=True)
    fh.write(line + "\n")
    fh.flush()


def wall_metrics(surface, cap, centerline=None, sweep="centreline", smoothing=0.0):
    from meshforge.meshing import get_backend

    kw = {} if sweep == "centreline" else {"sweep": sweep, "section_smoothing": smoothing}
    w = get_backend("swept").mesh_wall(surface, THICKNESS, centerline=centerline,
                                       curvature_cap=cap, **kw)
    q, lat = w.quality, w.lattice
    out = {
        "cells": int(w.grid.n_cells), "inverted": int(q.n_inverted),
        "sj_min": round(float(q.sj_min), 4), "sj_mean": round(float(q.sj_mean), 4),
        "capped": lat["n_capped"], "floored": lat["n_floored"],
        "t_min": round(lat["thickness_min"], 3), "sweep": lat.get("sweep", "centreline"),
    }
    if lat.get("sweep") == "planes":
        out.update(smoothing=lat["section_smoothing"], tilt_max=lat["tilt_max_deg"],
                   offset_max=lat["surface_offset_max"], offset_p95=lat["surface_offset_p95"])
    if "auto_centreline" in lat:
        out["auto_centreline"] = lat["auto_centreline"]
    return out, w


def lumen_metrics(surface):
    from meshforge.meshing.lumen import fill_lumen

    m = fill_lumen(surface)
    q = m.quality
    anchored = sum(1 for n in m.notes if "anchored on its flat end cap" in n
                   and "not anchored" not in n)
    return {
        "backend": m.backend, "cells": int(m.grid.n_cells),
        "inverted": int(q.n_inverted), "sj_min": round(float(q.sj_min), 4),
        "ends_anchored": anchored,
    }, m


def guarded(fh, label, fn):
    t = time.time()
    try:
        out = fn()
        log(fh, f"  {label}: ok ({time.time() - t:.1f} s)")
        return out, None
    except Exception as exc:  # recorded per case, never fail-open: the row says why
        log(fh, f"  {label}: FAILED ({type(exc).__name__}: {exc})")
        fh.write(traceback.format_exc())
        return None, f"{type(exc).__name__}: {str(exc)[:300]}"


def crop_case(fh, outer, lumen, case_dir, **kw):
    """Auto landmarks; on refusal (short Zenodo crops), a manual trim just
    inside the given ends, moved inward until the square cut clears the
    original tilted end caps (2 mm steps)."""
    from meshforge.geometry.crop import CropError, crop_aaa

    try:
        res = crop_aaa(outer, lumen=lumen, **kw)
        return res, "auto"
    except CropError as exc:
        log(fh, f"  crop auto refused: {str(exc)[:400]}")
        auto_err = str(exc).split(";")[0]
    from meshforge.geometry.crop import aaa_centerline

    L = aaa_centerline(outer.triangulate()).length
    last = None
    for inset in np.arange(4.0, 0.3 * L, 2.0):
        try:
            res = crop_aaa(outer, lumen=lumen, distal_height_mm=float(inset),
                           proximal_height_mm=float(L - inset))
            res.notes.append(f"auto landmarks refused ({auto_err}); manual trim {inset:g} mm "
                             f"inside each end")
            return res, f"manual trim {inset:g} mm"
        except (CropError, ValueError) as exc:
            last = exc
    raise CropError(f"auto refused ({auto_err}); no manual trim worked: {last}")


def save(mesh, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    mesh.save(str(path))


def run_case(case, spec):
    from meshforge.geometry import centerline_from_surface

    GATE.mkdir(parents=True, exist_ok=True)
    cdir = GATE / case
    cdir.mkdir(parents=True, exist_ok=True)
    row = {"case": case, "source": spec["source"], "status": spec["status"]}
    with open(cdir / "log.txt", "w") as fh:
        log(fh, f"== {case} ({spec['source']}; {spec['status']})")
        outer, lumen = spec["load"](fh)
        row["outer_label"], row["lumen_label"] = spec["outer_label"], spec["lumen_label"]

        if spec.get("baseline"):
            for cap in (0.5,):
                m, err = guarded(fh, "baseline wall (as given, default centreline, cap 0.5)",
                                 lambda: wall_metrics(outer, cap)[0])
                row["base_wall"] = m or {"error": err}
            m, err = guarded(fh, "baseline lumen (as given)", lambda: lumen_metrics(lumen)[0])
            row["base_lumen"] = m or {"error": err}
            log(fh, "  baseline:", json.dumps({k: row[k] for k in ("base_wall", "base_lumen")}))

        res, err = guarded(fh, "crop", lambda: crop_case(fh, outer, lumen, None,
                                                       **spec.get("crop_kw", {})))
        if res is None:
            row["crop"] = {"error": err}
            return row
        res, variant = res
        rep = res.report()
        (cdir / "crop_report.json").write_text(json.dumps(rep, indent=1, default=str))
        save(res.surface, cdir / "outer_crop.vtp")
        if res.lumen is not None:
            save(res.lumen, cdir / "lumen_crop.vtp")
        lm = rep["landmarks"] or {}
        row["crop"] = {
            "variant": variant,
            "sac_d_equiv": lm.get("sac_d_equiv_mm"), "sac_d_max": lm.get("sac_max_diameter_mm"),
            "neck_d": lm.get("neck_d_equiv_mm"), "sac_neck": lm.get("sac_neck_ratio"),
            "neck_len": lm.get("neck_length_mm"), "neck_open": lm.get("neck_open_ended"),
            "h_d": rep["planes"]["distal"]["height_mm"],
            "h_p": rep["planes"]["proximal"]["height_mm"],
            "cap_d": rep["caps"]["distal"]["d_equiv_mm"],
            "cap_p": rep["caps"]["proximal"]["d_equiv_mm"],
            "planarity": max(rep["caps"][k]["planarity_mm"] for k in ("distal", "proximal")),
            "outer_ml": rep["surface"]["volume_ml"],
        }
        for n in rep["notes"]:
            log(fh, "  note:", n)
        log(fh, "  crop:", json.dumps(row["crop"]))

        if spec.get("wall", True):
            wall_surface = res.surface
            if spec.get("sac"):
                # D86/D87: the wall belongs on the recovered outer sac, cut by
                # this crop's own two planes
                import pyvista as pv

                from meshforge.geometry.crop import crop_with_planes, planes_from_report

                sac, _, note = crop_with_planes(pv.read(str(ROOT / spec["sac"])),
                                                planes_from_report(rep), what="sac")
                save(sac, cdir / "sac_crop.vtp")
                log(fh, f"  sac crop: {sac.volume / 1000:.2f} mL; {note}")
                wall_surface = sac
            cl = centerline_from_surface(wall_surface)
            smoothing = spec.get("section_smoothing", 0.0)
            if smoothing:
                m, err = guarded(fh, "wall cap 0.9, auto, unsmoothed",
                                 lambda: wall_metrics(wall_surface, GATE_CAP, centerline=cl,
                                                      sweep="auto")[0])
                row["wall_0.9_unsmoothed"] = m or {"error": err}
                log(fh, "  wall cap 0.9 unsmoothed:", json.dumps(row["wall_0.9_unsmoothed"]))
            for cap in CAPS:
                sweep = "auto" if cap == GATE_CAP else "centreline"
                m, err = guarded(fh, f"wall cap {cap} ({sweep})",
                                 lambda: wall_metrics(wall_surface, cap, centerline=cl,
                                                      sweep=sweep,
                                                      smoothing=smoothing if sweep != "centreline"
                                                      else 0.0))
                if m is not None:
                    metrics, wmesh = m
                    row[f"wall_{cap}"] = metrics
                    if cap == GATE_CAP:
                        save(wmesh.grid, cdir / "wall.vtu")
                else:
                    row[f"wall_{cap}"] = {"error": err}
                log(fh, f"  wall cap {cap}:", json.dumps(row[f"wall_{cap}"]))
        else:
            row["wall_pending"] = spec["wall_pending"]
            log(fh, "  wall:", spec["wall_pending"])

        target = res.lumen if spec.get("lumen_is_second", True) else res.surface
        m, err = guarded(fh, "lumen", lambda: lumen_metrics(target))
        if m is not None:
            metrics, lmesh = m
            row["lumen"] = metrics
            save(lmesh.grid, cdir / "lumen.vtu")
        else:
            row["lumen"] = {"error": err}
        log(fh, "  lumen:", json.dumps(row["lumen"]))
    return row


def zenodo_spec(case):
    def load(fh):
        from meshforge.io.zenodo_aaa import load_reference

        ref = load_reference(ZEN / case)
        log(fh, f"  reference z {ref.z0:.2f}..{ref.z1:.2f} mm")
        return ref.regions["lumen_ilt"].triangulate(), ref.regions["lumen"].triangulate()

    return {
        "source": "Zenodo 19182977", "status": "reference segmentation",
        "outer_label": "R_lumen_ilt", "lumen_label": "R_lumen", "load": load,
        "baseline": True,
    }


def aortaseg_spec(case, mask, status, *, ts_is_lumen):
    ct = ROOT / f"outputs/data/aortaseg60/ct_fixed/{case}_image.nii.gz"

    def load(fh):
        import nibabel as nib

        from meshforge.io.image import mask_to_surface, nifti_affine_mm
        from meshforge.io.segment import ct_lumen_surface, load_mask

        m, ma = load_mask(ROOT / mask)
        img = nib.load(str(ct))
        aff, _ = nifti_affine_mm(img)
        shape = tuple(int(v) for v in img.header.get_data_shape())
        surf, prov = mask_to_surface(m, ma, sigma=1.0)
        log(fh, f"  TotalSegmentator surface: {prov['n_points']} pts, "
                f"{prov['mask_volume'] / 1000:.1f} mL, watertight {prov['watertight']}")
        lum, summ = ct_lumen_surface(ct, m, ma, shape, aff, lumen_hu=150.0, smoothing=1.0)
        log(fh, "  CT-threshold lumen:", json.dumps({k: v for k, v in summ.items()
                                                    if k not in ("notes", "surface")}))
        save(surf, GATE / case / "ts_surface.vtp")
        save(lum, GATE / case / "threshold_lumen.vtp")
        return surf.triangulate(), lum.triangulate()

    spec = {"source": "AortaSeg-60", "status": status, "load": load,
            "outer_label": "TotalSegmentator aorta (incl. thrombus)" if not ts_is_lumen
            else "TotalSegmentator aorta (= lumen on this case)",
            "lumen_label": "CT >= 150 HU inside the AI mask"}
    if ts_is_lumen:
        # the AI label is the lumen only (D84): the wall goes on the recovered
        # outer sac (D86), cut by the same planes, sections smoothed 4 mm (D87)
        spec.update(lumen_is_second=False, sac="outputs/m2/sac/Anev_02_sac.vtp",
                    section_smoothing=4.0,
                    outer_label="recovered outer sac (D86); crop on the lumen-only AI label",
                    lumen_label="TotalSegmentator aorta (lumen-only on this case)")
    return spec


def specs():
    out = {f"P{i:02d}": zenodo_spec(f"P{i:02d}") for i in range(1, 21)}
    out["Anev_04"] = aortaseg_spec(
        "Anev_04", "outputs/m1/aortaseg/Anev_04_fixed_aorta_mask.nii.gz",
        "POST-EVAR (stent graft) - not a demo case", ts_is_lumen=False)
    out["Anev_02"] = aortaseg_spec(
        "Anev_02", "outputs/m2/anev02_v218/Anev_02_ts218_aorta_mask.nii.gz",
        "native AAA", ts_is_lumen=True)
    return out


def gate(row):
    w = row.get(f"wall_{GATE_CAP}") or {}
    lum = row.get("lumen") or {}
    wall_ok = ("inverted" in w and w["inverted"] == 0 and w["capped"] == 0
               and w["floored"] == 0) if "wall_pending" not in row else None
    lumen_ok = "inverted" in lum and lum["inverted"] == 0
    return wall_ok, lumen_ok


def write_tables(rows):
    cols = ["case", "source", "status", "crop", "sac_d_equiv", "sac_d_max", "neck_d",
            "sac_neck", "neck_len", "h_d", "h_p", "cap_d", "cap_p",
            "wall_sweep", "wall_cells", "wall_inv", "wall_sj_min", "wall_sj_mean",
            "capped_0.5", "floored_0.5", "capped_0.9", "floored_0.9",
            "lumen_backend", "lumen_cells", "lumen_inv", "lumen_sj_min", "lumen_ends_anchored",
            "base_wall_inv", "base_wall_capped", "base_wall_floored", "base_lumen",
            "wall_gate", "lumen_gate", "demo_gate"]
    flat = []
    for r in rows:
        c = r.get("crop") or {}
        w5, w9 = r.get("wall_0.5") or {}, r.get("wall_0.9") or {}
        lum, bw, bl = r.get("lumen") or {}, r.get("base_wall") or {}, r.get("base_lumen") or {}
        wall_ok, lumen_ok = gate(r)
        flat.append({
            "case": r["case"], "source": r["source"], "status": r["status"],
            "crop": c.get("variant", c.get("error", "")),
            **{k: c.get(k) for k in ("sac_d_equiv", "sac_d_max", "neck_d", "sac_neck",
                                     "neck_len", "h_d", "h_p", "cap_d", "cap_p")},
            "wall_sweep": _sweep_label(w9),
            "wall_cells": w9.get("cells", r.get("wall_pending", w9.get("error"))),
            "wall_inv": w9.get("inverted"), "wall_sj_min": w9.get("sj_min"),
            "wall_sj_mean": w9.get("sj_mean"),
            "capped_0.5": w5.get("capped"), "floored_0.5": w5.get("floored"),
            "capped_0.9": w9.get("capped"), "floored_0.9": w9.get("floored"),
            "lumen_backend": lum.get("backend", lum.get("error")),
            "lumen_cells": lum.get("cells"), "lumen_inv": lum.get("inverted"),
            "lumen_sj_min": lum.get("sj_min"), "lumen_ends_anchored": lum.get("ends_anchored"),
            "base_wall_inv": bw.get("inverted", bw.get("error")),
            "base_wall_capped": bw.get("capped"), "base_wall_floored": bw.get("floored"),
            "base_lumen": (f"{bl['backend']} inv {bl['inverted']}" if "backend" in bl
                           else bl.get("error")),
            "wall_gate": {True: "PASS", False: "fail", None: "pending"}[wall_ok],
            "lumen_gate": "PASS" if lumen_ok else "fail",
            "demo_gate": "PASS" if (wall_ok and lumen_ok) else
            ("lumen PASS, wall pending" if wall_ok is None and lumen_ok else "fail"),
        })
    with open(OUT / "gate_table.csv", "w", newline="") as fh:
        wr = csv.DictWriter(fh, fieldnames=cols)
        wr.writeheader()
        wr.writerows(flat)
    md = [
        "# M2 gate table (D85)",
        "",
        "Wall: swept hexahedra, 1.5 mm outward from the outer surface; gate at curvature_cap "
        f"{GATE_CAP} with sweep=auto (D87): the crop's embedded segment centreline unless that "
        "wall inverts or is capped/floored, then stations on planes parallel to the two "
        "parallel caps (\"planes\"; for planes, capped = tilt + concavity caps). The 0.5 "
        "counts are the centreline sweep's, for comparison. Lumen: O-grid with tet fallback "
        "on the blood lumen. Demo gate = wall 0 inverted, 0 capped, 0 floored AND lumen 0 "
        "inverted. Baseline = the Zenodo surfaces as given (axial end planes), default "
        "centreline, cap 0.5.",
        "",
        "Data: Zenodo AAA, doi 10.5281/zenodo.19182977, CC BY 4.0 (reference segmentation, "
        "not ground truth); AortaSeg-60, doi 10.5281/zenodo.18147026, CC BY 4.0.",
        "",
        "| case | status | crop | sac d_eq / d_max | neck d | sac/neck | wall sweep | wall cells "
        "| wall inv | sj_min | capped/floored @0.5 | @0.9 | lumen | lumen inv | lumen sj_min | "
        "baseline wall inv (cap/floor) | baseline lumen | demo gate |",
        "|" + "---|" * 18,
    ]
    for f in flat:
        md.append(
            f"| {f['case']} | {f['status']} | {f['crop']} | {f['sac_d_equiv']} / {f['sac_d_max']} | "
            f"{f['neck_d']} | {f['sac_neck']} | {f['wall_sweep']} | {f['wall_cells']} | "
            f"{f['wall_inv']} | "
            f"{f['wall_sj_min']} | {f['capped_0.5']}/{f['floored_0.5']} | "
            f"{f['capped_0.9']}/{f['floored_0.9']} | {f['lumen_backend']} ({f['lumen_cells']}) | "
            f"{f['lumen_inv']} | {f['lumen_sj_min']} | {f['base_wall_inv']} "
            f"({f['base_wall_capped']}/{f['base_wall_floored']}) | {f['base_lumen']} | "
            f"**{f['demo_gate']}** |"
        )
    notes = []
    for r in rows:
        before, now = r.get("before_d87"), r.get("wall_0.9") or {}
        if before and before.get("wall_0.9"):
            b = before["wall_0.9"]
            notes.append(
                f"- {r['case']}: before D87 (centreline sweep) wall inverted {b['inverted']}, "
                f"sj_min {b['sj_min']}, capped/floored {b['capped']}/{b['floored']} -> now "
                f"{_sweep_label(now)}: inverted {now.get('inverted')}, sj_min "
                f"{now.get('sj_min')}, capped/floored {now.get('capped')}/{now.get('floored')}."
            )
        elif before and before.get("wall_pending"):
            notes.append(f"- {r['case']}: before D87 the wall was \"{before['wall_pending']}\".")
        raw = r.get("wall_0.9_unsmoothed")
        if raw and "inverted" in raw:
            notes.append(
                f"- {r['case']}: without section smoothing ({_sweep_label(raw)}) the wall has "
                f"inverted {raw['inverted']}, sj_min {raw['sj_min']}, capped/floored "
                f"{raw['capped']}/{raw['floored']} (thickness down to {raw['t_min']} mm at "
                f"mm-scale notches of the recovered sac surface). The gate row uses "
                f"{now.get('smoothing')} mm smoothing: nodes up to {now.get('offset_max')} mm "
                f"(p95 {now.get('offset_p95')}) off the sac surface."
            )
    if notes:
        md += ["", "Notes (D87):", *notes]
    (OUT / "gate_table.md").write_text("\n".join(md) + "\n")


def _sweep_label(w):
    if not w or "cells" not in w:
        return None
    if w.get("sweep") != "planes":
        return "centreline"
    sm = w.get("smoothing") or 0
    return "planes" + (f", smoothed {sm:g} mm" if sm else "")


def main(argv):
    all_specs = specs()
    names = argv or list(all_specs)
    rows_path = OUT / "gate_rows.json"
    rows = {r["case"]: r for r in json.loads(rows_path.read_text())} if rows_path.exists() else {}
    for name in names:
        t = time.time()
        old = rows.get(name) or {}
        before = old.get("before_d87") or {k: old[k] for k in ("wall_0.9", "wall_pending")
                                            if k in old}
        try:
            rows[name] = run_case(name, all_specs[name])
            if before:
                rows[name]["before_d87"] = before
        except Exception as exc:
            rows[name] = {"case": name, "source": all_specs[name]["source"],
                          "status": all_specs[name]["status"],
                          "crop": {"error": f"{type(exc).__name__}: {exc}"}}
            traceback.print_exc()
        rows[name]["seconds"] = round(time.time() - t, 1)
        rows_path.write_text(json.dumps(list(rows.values()), indent=1, default=str))
    order = list(all_specs)
    write_tables([rows[k] for k in order if k in rows])


if __name__ == "__main__":
    main(sys.argv[1:])

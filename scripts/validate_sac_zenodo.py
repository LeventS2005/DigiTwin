"""Validate sac recovery (D86) on the 20 Zenodo AAA cases.

Dataset: Zenodo DOI 10.5281/zenodo.19182977, CC BY 4.0. Its surfaces are a
REFERENCE segmentation (AI-assisted + Slicer), not ground truth; results are
agreement with it, not accuracy.

Per case:
* phase: of the case's 4D-CTA frames, the one whose >= 150 HU voxels have the
  highest Dice with the reference lumen R_lumen inside the reference slab
  (the frame the reference most likely belongs to; the frames differ by
  < 0.01 Dice). The rule never looks at the thrombus or outer wall.
* input lumen: voxels >= 150 HU within 5 mm (in-plane) of R_lumen, the
  6-connected component overlapping R_lumen most (bone is bright too, so it
  must be kept out), written as a NIfTI mask on the CT grid;
* ``recover_sac_from_files`` with the step's default knobs, then Dice / ASSD
  / HD95 against R_lumen_ilt (primary) and R_full in the reference slab.
A case refused for too few resolved rays is re-run with min_resolved 0.25 as
a DIAGNOSTIC row, kept out of the summary statistics.

    python -I scripts/validate_sac_zenodo.py DATASET_ROOT OUT_DIR [P01 ...]
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import csv  # noqa: E402
import json  # noqa: E402
import time  # noqa: E402

import numpy as np  # noqa: E402
from scipy import ndimage  # noqa: E402

import digitwin  # noqa: E402
from digitwin.geometry.sac import recover_sac_from_files  # noqa: E402
from digitwin.io.nrrd import read_nrrd  # noqa: E402
from digitwin.io.segment import MAX_CT_VOXELS, write_ct_nifti  # noqa: E402
from digitwin.io.zenodo_aaa import load_reference, validate_against_reference  # noqa: E402
from digitwin.quality.overlap import slab_mask, voxelize_surface  # noqa: E402

LUMEN_HU = 150.0
NEAR_MM = 5.0


def pick_phase(case_dir, ref):
    best = None
    for ph in sorted((case_dir / "4D_CTA").glob("*.nrrd")):
        vol = read_nrrd(ph, max_voxels=MAX_CT_VOXELS)
        slab = slab_mask(vol.array.shape, vol.affine, ref.z0, ref.z1)
        rl = voxelize_surface(ref.regions["lumen"], vol.array.shape, vol.affine) & slab
        bright = (vol.array >= LUMEN_HU) & slab
        d = 2 * np.count_nonzero(bright & rl) / (np.count_nonzero(bright) + np.count_nonzero(rl))
        if best is None or d > best[1]:
            best = (ph, d, vol)
    return best


def input_lumen(ct, affine, r_lumen):
    sp = np.linalg.norm(affine[:3, :3], axis=0)
    near = ndimage.distance_transform_edt(~r_lumen, sampling=(sp[0], sp[1], 1e6)) <= NEAR_MM
    lab, n = ndimage.label((ct >= LUMEN_HU) & near)
    overlap = np.bincount(lab[r_lumen].ravel(), minlength=n + 1)
    overlap[0] = 0
    if overlap.max() == 0:
        raise ValueError("no >= 150 HU voxels overlap the reference lumen")
    return lab == int(np.argmax(overlap))


def run_case(case_dir, out_dir):
    t0 = time.time()
    ref = load_reference(case_dir)
    ph, ph_dice, vol = pick_phase(case_dir, ref)
    lum = input_lumen(vol.array, vol.affine, voxelize_surface(ref.regions["lumen"], vol.array.shape, vol.affine))
    mask_path = out_dir / "zenodo_inputs" / f"{case_dir.name}_lumen_input.nii.gz"
    mask_path.parent.mkdir(parents=True, exist_ok=True)
    write_ct_nifti(lum.astype(np.uint8), vol.affine, mask_path)
    voxel_ml = abs(float(np.linalg.det(vol.affine[:3, :3]))) / 1000
    row = {
        "case": case_dir.name,
        "phase": ph.name,
        "phase_lumen_dice": round(ph_dice, 4),
        "grid": "x".join(str(s) for s in vol.array.shape),
        "spacing_mm": "x".join(f"{s:.3g}" for s in np.linalg.norm(vol.affine[:3, :3], axis=0)),
        "lumen_input_ml": round(float(np.count_nonzero(lum)) * voxel_ml, 2),
    }
    rows = []
    for mode, min_res in (("default", 0.5), ("diagnostic", 0.25)):
        r = dict(row, run=mode, min_resolved=min_res)
        try:
            run = recover_sac_from_files(ph, mask_path, min_resolved=min_res)
        except ValueError as exc:
            r["refused"] = str(exc)
            rows.append(r)
            if mode == "default" and "found a fat plane" in str(exc):
                continue  # re-run as a diagnostic
            break
        s = run.summary
        val = validate_against_reference(run.surface, case_dir, run.grid_shape, run.grid_affine)
        c = s["ray_status_counts"]
        r.update(
            refused="",
            resolved_fraction=s["resolved_fraction"],
            rays=sum(c.values()),
            **{f"rays_{k}": v for k, v in c.items()},
            slab_sac_ml=round(val["segmented_volume_ml"], 2),
        )
        for name in ("lumen_ilt", "full", "lumen"):
            reg = val["regions"][name]
            r[f"ref_{name}_ml"] = round(reg["volume_ml"], 2)
            r[f"dice_{name}"] = round(reg["dice"], 4)
            r[f"assd_{name}_mm"] = round(reg["distance"]["assd"], 2)
            r[f"hd95_{name}_mm"] = round(reg["distance"]["hd95"], 2)
        r["sac_max_caliper_mm"] = s["max_diameter"]["max_caliper"]["value_mm"]
        r["thickness_median_mm"] = s["thickness_mm"]["all_rays"]["median"]
        rows.append(r)
        break
    for r in rows:
        r["seconds"] = round(time.time() - t0, 1)
    return rows


def main(root, out_dir, cases):
    root, out_dir = Path(root), Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"digitwin from {digitwin.__file__}", flush=True)
    cases = cases or sorted(p.name for p in root.iterdir() if p.is_dir())
    rows = []
    for case in cases:
        got = run_case(root / case, out_dir)
        for r in got:
            print(json.dumps({k: r.get(k) for k in ("case", "run", "phase", "resolved_fraction",
                                                   "dice_lumen_ilt", "dice_full", "assd_lumen_ilt_mm",
                                                   "refused")}), flush=True)
        rows += got
    keys = []
    for r in rows:
        keys += [k for k in r if k not in keys]
    with open(out_dir / "zenodo_validation.csv", "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=keys)
        w.writeheader()
        w.writerows(rows)
    ok = [r for r in rows if r["run"] == "default" and not r["refused"]]
    stats = {}
    for key in ("dice_lumen_ilt", "dice_full", "assd_lumen_ilt_mm", "assd_full_mm",
                "hd95_lumen_ilt_mm", "hd95_full_mm", "resolved_fraction"):
        v = np.array([r[key] for r in ok], dtype=float)
        stats[key] = {"n": int(v.size), "median": round(float(np.median(v)), 4),
                      "q1": round(float(np.percentile(v, 25)), 4),
                      "q3": round(float(np.percentile(v, 75)), 4)}
    (out_dir / "zenodo_validation_stats.json").write_text(json.dumps(stats, indent=1) + "\n")
    print(json.dumps(stats, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2], sys.argv[3:]))

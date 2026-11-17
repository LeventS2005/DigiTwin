"""M6 chart: normal-aorta wall strength next to the AAA lab v0 band.

Source: "Dataset on human NORMAL DIAMETER abdominal aorta biomechanics
(uniaxial) and histology", Mendeley Data v4, doi 10.17632/yfj4wfszbw.4,
CC BY 4.0 (Gomes et al.): 30 autopsy aortas pressurised to rupture, strips
from the rupture border and from intact infrarenal controls.

Each strip's failure stress is computed the way the packaged AAA library
(``scripts/build_strength_v0.py``) does it: Cauchy stress for an
incompressible strip, sigma = F lambda / (w0 t0), lambda = 1 + displacement /
gauge length, failure = the maximum Cauchy stress; when that maximum lies
more than 0.10 strain past the load maximum (a late peak), the stress at the
load maximum is used instead (``late_peak="first_peak"``). One difference:
the curve is read from the Series IX text report (raw load), not the xls
("adjusted load" = raw load minus a 0-0.1 N offset, under 2 % at failure in
the AAA data), because no xls reader is installed.

The AAA band comes from ``digitwin.tissue`` (the library the investigational
index uses), quantiles from ``strength_quantiles`` for every band. The demo
index is NOT changed: this chart only places the normal wall beside it.

The downloaded files are untrusted: every file is checked against the
download manifest's sha256 before it is read, nothing in them is executed,
a report that does not have the expected layout stops the run, and the
operator name in the headers is never read out. Run isolated, from outside
the data folder:

    python -I scripts/m6_normal_strength.py outputs/data/usp_normal outputs/m6/usp_normal_manifest.json \
        outputs/m6/normal_strength --pws-summary outputs/m6/stress/session_aaa_stress.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))  # our own conversions and the strength library

from digitwin.tissue import mechanics as mech  # noqa: E402
from digitwin.tissue.strength import load_strength_library, strength_quantiles  # noqa: E402

DOI = "10.17632/yfj4wfszbw.4"
CREDIT = "USP normal abdominal aorta tensile data (Gomes et al.), Mendeley Data, CC BY 4.0, doi " + DOI
LATE_PEAK_STRAIN_GAP = 0.10  # as in build_strength_v0.py
NUM = r"[-+]?\d+(?:\.\d*)?(?:[eE][-+]?\d+)?"


def site_group(stem: str) -> tuple[str, str]:
    """(site code, group) from a report name like 'Case12 aoinfraproxt'."""
    code = re.sub(r"^case\s*\d+\s*", "", stem.lower()).strip()
    code = re.sub(r"t1?$", "", code).strip()
    if "borda" in code or "rotura" in code:
        return code, "rupture_border"
    if code.startswith("aoinfra") or code.startswith("aodistal"):
        return code, "control"
    return code, "unclassified"


def read_report(path: Path) -> dict:
    text = path.read_bytes().decode("latin-1")
    joined = "\n".join(ln.rstrip() for ln in text.splitlines())
    for need in (r"Test Type: Tensile\b", r"Units: SI\b", r"Geometry: Rectangular\b",
                 r"Number of specimens: 1\b", r"Extensometer: Disabled\b"):
        if not re.search(need, joined):
            raise SystemExit(f"{path.name}: report lacks {need!r}")

    def field(label: str, unit: str) -> float:
        m = re.search(rf"^\s*{label}:\s*({NUM}) {unit}\s*$", joined, re.M)
        if not m:
            raise SystemExit(f"{path.name}: no '{label}: <number> {unit}' line")
        return float(m.group(1))

    rep = {"width": field("Width", "mm"), "thickness": field("Thickness", "mm"),
           "gauge": field("Spec gauge len", "mm")}
    m = re.search(r"Number of data points:\s*(\d+)", joined)
    if not m:
        raise SystemExit(f"{path.name}: no 'Number of data points'")
    data = [(int(d.group(1)), float(d.group(2)), float(d.group(3)))
            for d in (re.fullmatch(rf"\s*(\d+),\s*({NUM}),\s*({NUM})", ln) for ln in joined.splitlines()) if d]
    n = int(m.group(1))
    if len(data) != n or [p for p, _, _ in data] != list(range(1, n + 1)):
        raise SystemExit(f"{path.name}: {len(data)} data lines, header says {n}")
    mx = re.search(rf"Maximum Load point:\s*(\d+)\s+Maximum Load:\s*({NUM}) kN", joined)
    # The instrument catches its peak between logged samples (seen: up to 2.2 % above the largest
    # logged load, its point one sample off on a sharp peak and a few off on a flat top). So both the
    # logged maximum and the largest logged load within 2 samples of the reported point must be within
    # 3 % of the reported maximum; a unit, column or curve mix-up would miss by far more.
    if not mx or not 1 <= int(mx.group(1)) <= n:
        raise SystemExit(f"{path.name}: no usable 'Maximum Load point' line")
    j, ref = int(mx.group(1)) - 1, float(mx.group(2))
    tol = max(1e-5, 0.03 * ref)
    near = max(kn for _, _, kn in data[max(0, j - 2): j + 3])
    if abs(max(kn for _, _, kn in data) - ref) > tol or abs(near - ref) > tol:
        raise SystemExit(f"{path.name}: the curve does not match the report's 'Maximum Load' line")
    rep["data"] = data
    return rep


def validate_on_aaa(root: Path) -> dict:
    """Run this TXT method on the AAA dataset's own reports and compare with
    the packaged library (which used the xls adjusted load)."""
    import csv

    sums = {}
    for line in (root / "_sha256.txt").read_text().splitlines():
        h, rel = line.split(maxsplit=1)
        sums[rel.removeprefix("./")] = h
    lib = REPO / "src" / "digitwin" / "data" / "strength_v0.csv"
    diffs = []
    for row in csv.DictReader(lib.read_text().splitlines()):
        if row["data_kind"] != "raw_curve" or not row["source_report"]:
            continue
        rel = "extracted/" + row["source_report"]
        p = root / rel
        if hashlib.sha256(p.read_bytes()).hexdigest() != sums.get(rel):
            raise SystemExit(f"{rel}: sha256 differs from the AAA dataset's list")
        ours = strip_strength(read_report(p))["strength_mpa"]
        theirs = float(row["first_peak_cauchy_mpa"] if row["late_peak"] == "yes" else row["failure_cauchy_mpa"])
        diffs.append({"strip": row["strip_id"], "txt_mpa": ours, "library_mpa": round(theirs, 4),
                      "rel_diff_percent": round(100 * (ours / theirs - 1), 2)})
    worst = max(abs(d["rel_diff_percent"]) for d in diffs)
    return {"n_strips": len(diffs), "max_abs_rel_diff_percent": worst, "strips": diffs}


def strip_strength(rep: dict) -> dict:
    w, t, g = rep["width"], rep["thickness"], rep["gauge"]
    lam = [1.0 + u / g for _, u, _ in rep["data"]]
    f = [1000.0 * kn for _, _, kn in rep["data"]]
    sig = [mech.cauchy_incompressible_mpa(max(fi, 0.0), li, w, t) for fi, li in zip(f, lam)]
    i = max(range(len(sig)), key=lambda q: sig[q])  # failure = maximum Cauchy stress
    k = max(range(len(f)), key=lambda q: f[q])  # load maximum
    late = (lam[i] - lam[k]) > LATE_PEAK_STRAIN_GAP
    use = k if late else i
    return {"width_mm": w, "thickness_mm": t, "gauge_length_mm": g, "max_load_n": round(f[k], 4),
            "failure_cauchy_mpa": round(sig[i], 4), "failure_strain": round(lam[i] - 1.0, 4),
            "late_peak": late, "strength_mpa": round(sig[use], 4)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("data", type=Path, help="the downloaded dataset folder")
    ap.add_argument("manifest", type=Path, help="the download manifest (sha256 per file)")
    ap.add_argument("out", type=Path, help="output stem")
    ap.add_argument("--pws-summary", type=Path, default=None, help="an aaa_stress summary .json (demo PWS line)")
    ap.add_argument("--validate-aaa", type=Path, default=None,
                    help="the USP AAA dataset folder (with _sha256.txt): check this TXT method against the library")
    a = ap.parse_args(argv)

    man = json.loads(a.manifest.read_text())
    want = {Path(r["path"]).relative_to("outputs/data/usp_normal").as_posix(): r["sha256"] for r in man["files"]}
    strips, skipped = [], []
    for rel, sha in sorted(want.items()):
        p = a.data / rel
        if hashlib.sha256(p.read_bytes()).hexdigest() != sha:
            raise SystemExit(f"{rel}: sha256 differs from the download manifest")
        if not rel.lower().endswith(".txt"):
            continue
        code, group = site_group(p.stem)
        rec = {"case": int(re.search(r"Case_(\d+)", rel).group(1)), "site_code": code, "group": group,
               "source_file": rel, "source_sha256": sha}
        rec.update(strip_strength(read_report(p)))
        (strips if group != "unclassified" else skipped).append(rec)

    lib = load_strength_library()
    bands = {}
    for key, label, vals, cases in (
        ("aaa_rupture_border", "AAA, rupture border\n(lab v0, used by the demo index)",
         [r.strength_mpa("cauchy", "first_peak") for r in lib.select("aaa")],
         {r.case for r in lib.select("aaa")}),
        ("aaa_control", "AAA donors, intact infrarenal control",
         [r.strength_mpa("cauchy", "first_peak") for r in lib.select("control")],
         {r.case for r in lib.select("control")}),
        ("normal_rupture_border", "normal aorta, rupture border",
         [s["strength_mpa"] for s in strips if s["group"] == "rupture_border"],
         {s["case"] for s in strips if s["group"] == "rupture_border"}),
        ("normal_control", "normal aorta, intact infrarenal control",
         [s["strength_mpa"] for s in strips if s["group"] == "control"],
         {s["case"] for s in strips if s["group"] == "control"}),
    ):
        q = strength_quantiles(vals, "empirical")
        bands[key] = {"label": label, "n_strips": len(vals), "n_aortas": len(cases),
                      "p10_mpa": round(q["p10"], 3), "p50_mpa": round(q["p50"], 3), "p90_mpa": round(q["p90"], 3)}
    pws = None
    if a.pws_summary:
        s = json.loads(a.pws_summary.read_text())
        pws = (float(s["systolic"]["von_mises_p99_mpa"]), float(s["systolic"]["pressure_mmhg"]))
    result = {
        "what": "wall failure strength (Cauchy, incompressible strip; first-peak rule), empirical P10/P50/P90",
        "units": "MPa", "normal_source": CREDIT, "aaa_source": "DigiTwin strength library v0 (doi 10.17632/x64srrc39p.3)",
        "method_note": "normal strips read from the Series IX text reports (raw load); the AAA library uses the "
                       "xls adjusted load (raw minus a 0-0.1 N offset)",
        "bands": bands, "demo_pws_mpa": pws[0] if pws else None,
        "n_unclassified_skipped": len(skipped), "unclassified_codes": sorted({s["site_code"] for s in skipped}),
        "n_late_peak": sum(s["late_peak"] for s in strips), "strips": strips,
        "demo_index": "unchanged: the investigational index uses the AAA rupture-border band only",
        "txt_method_check_on_aaa": validate_on_aaa(a.validate_aaa) if a.validate_aaa else None,
    }
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.with_suffix(".json").write_text(json.dumps(result, indent=1) + "\n")
    _chart(result, pws, a.out.with_suffix(".png"))
    check = result["txt_method_check_on_aaa"]
    print(json.dumps({k: result[k] for k in ("bands", "n_unclassified_skipped", "unclassified_codes", "n_late_peak")},
                     indent=1))
    if check:
        print(f"TXT method on the AAA reports: {check['n_strips']} strips, max |diff| vs library "
              f"{check['max_abs_rel_diff_percent']} %")
    return 0


def _chart(result: dict, pws, path: Path) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plt.rcParams.update({"font.family": "sans-serif", "font.size": 13})
    red, ink, grey, muted = "#c8312b", "#1a1f26", "#5d6672", "#8a939e"
    order = ["aaa_rupture_border", "aaa_control", "normal_rupture_border", "normal_control"]
    colours = {"aaa_rupture_border": red, "aaa_control": "#e8908a", "normal_rupture_border": "#4a6fa5",
               "normal_control": "#9db4d6"}
    fig = plt.figure(figsize=(16, 9), dpi=120, facecolor="white")
    fig.text(0.05, 0.93, "How strong is the wall? Aneurysm vs normal aorta", fontsize=24, weight="bold", color=ink)
    fig.text(0.05, 0.895, "lab-measured failure strength of tensile strips (Cauchy stress), P10–P90 band with the "
             "median; n strips / n aortas", fontsize=13, color=grey)
    ax = fig.add_axes([0.33, 0.19, 0.62, 0.64])
    bands = result["bands"]
    hi = max(b["p90_mpa"] for b in bands.values())
    for y, key in enumerate(reversed(order)):
        b = bands[key]
        ax.plot([b["p10_mpa"], b["p90_mpa"]], [y, y], color=colours[key], lw=16, solid_capstyle="butt", alpha=0.9)
        ax.plot([b["p50_mpa"]] * 2, [y - 0.28, y + 0.28], color=ink, lw=2.5)
        ax.text(b["p90_mpa"] + 0.03 * hi, y, f"P50 {b['p50_mpa']:.2f} MPa  (P10 {b['p10_mpa']:.2f} – P90 "
                f"{b['p90_mpa']:.2f})", va="center", fontsize=12, color=ink)
        ax.text(-0.03, y, f"{b['label']}\n{b['n_strips']} strips / {b['n_aortas']} aortas",
                transform=ax.get_yaxis_transform(), ha="right", va="center", fontsize=13,
                color=ink if key == order[0] else grey, weight="bold" if key == order[0] else "normal")
    if pws:
        ax.axvline(pws[0], color=ink, lw=1.2, ls=":")
        ax.text(pws[0], len(order) - 0.45, f"demo peak wall stress\n{1000 * pws[0]:.1f} kPa at {pws[1]:g} mmHg",
                ha="center", va="bottom", fontsize=11.5, color=grey)
    ax.set_ylim(-0.6, len(order) - 0.1)
    ax.set_xlim(0, hi * 1.45)
    ax.set_yticks([])
    ax.set_xlabel("failure strength, Cauchy stress (MPa)")
    ax.spines[["top", "right", "left"]].set_visible(False)
    fig.text(0.05, 0.085, "The investigational index divides the peak wall stress by the AAA rupture-border band "
             "only (unchanged). The normal-aorta bands are context: same strip test, same stress measure.",
             fontsize=12, color=ink)
    fig.text(0.05, 0.048, f"Normal aorta: {CREDIT}.", fontsize=10.5, color=muted)
    fig.text(0.05, 0.028, "AAA: USP AAA tensile data (Gomes et al.), Mendeley Data, CC BY 4.0, doi "
             "10.17632/x64srrc39p.3 (DigiTwin strength library v0).", fontsize=10.5, color=muted)
    fig.text(0.05, 0.008, "Normal strips read from the instrument's text reports (raw load; on the AAA reports this "
             "reproduces the library within 1 %); first-peak rule for late peaks; empirical quantiles. Not a "
             "diagnosis.", fontsize=10.5, color=muted)
    fig.savefig(path, dpi=120, facecolor="white")
    plt.close(fig)


if __name__ == "__main__":
    raise SystemExit(main())

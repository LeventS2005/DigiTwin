"""M6 chart: our wall tension vs the dataset's published FE model, one row per case.

Source: the per-case rows of the with-thrombus tension validation
(``scripts/m3_tension_validation.py`` with ``DIGITWIN_TENSION_ILT=1``, run
over the 12 public AAA cases of Zenodo 19182977, CC BY 4.0, doi
10.5281/zenodo.19182977). The dataset's surfaces are a reference segmentation
and its tension maps are its published finite-element result; the chart
compares tension (ours vs theirs), it does not measure accuracy.

Each case has three rows in ``rows.json`` (end-zone variants ``neck``,
``bending``, ``none``). The chart uses ``--variant neck`` (the default), the
product's own end-zone rule: vertices outside one neck diameter from each cap,
as in the peak-wall-stress rule.

* left panel: Spearman rank correlation of our tension with theirs, per case;
* right panel: our p99 tension / their p99 tension, per case, with the band
  that the "within 8 %" claim names (the label is built from the data: if a
  case falls outside the band it says "n of N within 8 %").

Every plotted number is read from the rows file; the medians, the claim checks
and the rows' sha256 go to a JSON beside the PNG. A row that is missing,
duplicated, not ``ok``, or outside the axis range stops the run.

Run from the repository root:

    python scripts/m6_validation_chart.py outputs/m3/tension_validation_ilt/rows.json \
        outputs/m6/figures/10_validation_12cases
"""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import sys
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))  # kept for symmetry with the sibling scripts; no digitwin import needed

DOI = "10.5281/zenodo.19182977"
CAPTION = ("12 public AAA cases (Zenodo 19182977, reference segmentation), thrombus modelled; "
           "ours vs the dataset's published FE")
BAND = 0.08  # the "within 8 %" claim
RHO_XLIM = (0.5, 1.0)
RATIO_XLIM = (0.8, 1.2)
NEED = ("case", "variant", "status", "spearman_rho", "p99_ratio", "p50_ratio", "peak_offset_mm", "n_vertices")


def load_rows(path: Path, variant: str) -> list[dict]:
    """The ``ok`` rows of one end-zone variant, in the run's order. Stops on anything unexpected."""
    rows = json.loads(path.read_text())
    if not isinstance(rows, list) or not rows:
        raise SystemExit(f"{path.name}: expected a non-empty list of per-case rows")
    mine = [r for r in rows if r.get("variant") == variant]
    if not mine:
        raise SystemExit(f"{path.name}: no rows for variant {variant!r} (has {sorted({r.get('variant') for r in rows})})")
    bad = [r.get("case") for r in mine if r.get("status") != "ok"]
    if bad:
        raise SystemExit(f"{path.name}: variant {variant!r} has rows that are not 'ok': {bad}")
    for r in mine:
        missing = [k for k in NEED if r.get(k) is None]
        if missing:
            raise SystemExit(f"{path.name}: case {r.get('case')} lacks {missing}")
    cases = [r["case"] for r in mine]
    if len(set(cases)) != len(cases):
        raise SystemExit(f"{path.name}: duplicate cases in variant {variant!r}: {cases}")
    return mine


def provenance_path(path: Path) -> str:
    """The rows path from ``outputs/`` on (never an absolute path); the bare name if it is elsewhere."""
    parts = path.resolve().parts
    return "/".join(parts[parts.index("outputs"):]) if "outputs" in parts else path.name


def fmt3(x: float) -> str:
    """Three decimals, half up (the medians of 12 three-decimal values end in 5 at the fourth place)."""
    return str(Decimal(repr(round(x, 4))).quantize(Decimal("0.001"), rounding=ROUND_HALF_UP))


def pct_off(med: float) -> str:
    n = round(abs(1.0 - med) * 100)
    return "equal" if n == 0 else f"{n}% {'low' if med < 1 else 'high'}"


def summarise(rows: list[dict], rows_path: Path, variant: str) -> dict:
    rho = [r["spearman_rho"] for r in rows]
    p99 = [r["p99_ratio"] for r in rows]
    p50 = [r["p50_ratio"] for r in rows]
    peak = [r["peak_offset_mm"] for r in rows]
    for name, vals, (lo, hi) in (("spearman_rho", rho, RHO_XLIM), ("p99_ratio", p99, RATIO_XLIM)):
        out = [(r["case"], v) for r, v in zip(rows, vals) if not lo <= v <= hi]
        if out:
            raise SystemExit(f"{name} outside the chart's x range {lo}-{hi}: {out}")
    inside = [r["case"] for r in rows if abs(r["p99_ratio"] - 1.0) <= BAND + 1e-9]
    worst_rho = min(rows, key=lambda r: r["spearman_rho"])
    worst_p99 = max(rows, key=lambda r: abs(r["p99_ratio"] - 1.0))
    med = {"spearman_rho": statistics.median(rho), "p99_ratio": statistics.median(p99),
           "p50_ratio": statistics.median(p50), "peak_offset_mm": statistics.median(peak)}
    n = len(rows)
    return {
        "what": "our wall tension vs the dataset's published FE model, per case (with the thrombus modelled)",
        "source_dataset": f"Zenodo AAA dataset, doi {DOI}, CC BY 4.0 (reference segmentation; published FE tension)",
        "rows_file": provenance_path(rows_path),
        "rows_sha256": hashlib.sha256(rows_path.read_bytes()).hexdigest(),
        "variant": variant,
        "variant_meaning": "vertices within one neck diameter of each cap are excluded (the PWS end-zone rule)",
        "n_cases": n,
        "caption": CAPTION,
        "cases": [{"case": r["case"], "spearman_rho": r["spearman_rho"], "p99_ratio": r["p99_ratio"],
                   "p50_ratio": r["p50_ratio"], "peak_offset_mm": r["peak_offset_mm"], "n_vertices": r["n_vertices"],
                   "end_zone_mm": r.get("end_zone_mm")} for r in rows],
        "medians": {k: round(v, 4) for k, v in med.items()},
        "medians_3dp": {k: fmt3(v) for k, v in med.items() if k != "peak_offset_mm"},
        "median_p99_off_percent": pct_off(med["p99_ratio"]),
        "median_p50_off_percent": pct_off(med["p50_ratio"]),
        "band": {"half_width": BAND, "lo": round(1 - BAND, 4), "hi": round(1 + BAND, 4)},
        "p99_cases_within_band": len(inside), "p99_all_within_band": len(inside) == n,
        "p99_max_abs_deviation_percent": round(100 * abs(worst_p99["p99_ratio"] - 1.0), 1),
        "worst_rho": {"case": worst_rho["case"], "value": worst_rho["spearman_rho"]},
        "worst_p99": {"case": worst_p99["case"], "value": worst_p99["p99_ratio"]},
        "axes": {"rho_x": list(RHO_XLIM), "ratio_x": list(RATIO_XLIM)},
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("rows", type=Path, help="the validation run's rows.json (tension_validation_ilt/rows.json)")
    ap.add_argument("out", type=Path, help="output stem: <out>.png and <out>.json")
    ap.add_argument("--variant", default="neck", help="end-zone variant to plot (default: neck, the product rule)")
    a = ap.parse_args(argv)
    if not a.rows.is_file():
        raise SystemExit(f"no rows file at {a.rows}")
    result = summarise(load_rows(a.rows, a.variant), a.rows, a.variant)
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.with_suffix(".json").write_text(json.dumps(result, indent=1) + "\n")
    _chart(result, a.out.with_suffix(".png"))
    m = result["medians"]
    d = result["medians_3dp"]
    print(f"{result['n_cases']} cases ({result['variant']}): median rho {d['spearman_rho']}, p99 {d['p99_ratio']}, "
          f"p50 {d['p50_ratio']}, peak offset {m['peak_offset_mm']:.2f} mm; "
          f"p99 within 8%: {result['p99_cases_within_band']}/{result['n_cases']}")
    return 0


def _chart(result: dict, path: Path) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plt.rcParams.update({"font.family": "sans-serif", "font.sans-serif": ["Arial", "Helvetica", "DejaVu Sans"],
                         "font.size": 11})
    ink, grey, grid = "#1a1f26", "#5d6672", "#e6e9ed"
    dot, band_c = "#2f6aa8", "#dce7f3"
    cases = result["cases"]
    n = len(cases)
    m, d = result["medians"], result["medians_3dp"]
    labels = [c["case"] for c in cases]
    rows = range(n)
    top, bottom, label_y = -0.45, n - 0.4, -0.78  # the reference lines span the rows; labels sit in the headroom above

    fig = plt.figure(figsize=(10, 5.5), dpi=200, facecolor="white")
    fig.text(0.04, 0.945, "Wall tension: ours vs the dataset's published FE model", fontsize=17, weight="bold",
             color=ink, va="center")
    axes = [fig.add_axes([0.085, 0.215, 0.385, 0.605]), fig.add_axes([0.585, 0.215, 0.385, 0.605])]

    def frame(ax, xlim, ticks, title, xlabel):
        ax.set_xlim(*xlim)
        ax.set_ylim(bottom, -1.15)  # first case on top; headroom for the in-panel labels
        ax.set_xticks(ticks)
        ax.set_xticklabels([f"{t:.1f}" for t in ticks])
        ax.set_yticks(list(rows))
        ax.set_yticklabels(labels, color=ink)
        ax.set_xlabel(xlabel, color=grey)
        ax.set_title(title, loc="left", fontsize=13, weight="bold", color=ink, pad=8)
        ax.grid(color=grid, lw=0.8, zorder=0)
        ax.set_axisbelow(True)
        ax.spines[["top", "right", "left"]].set_visible(False)
        ax.spines["bottom"].set_color(grey)
        ax.tick_params(axis="y", length=0, labelsize=11)
        ax.tick_params(axis="x", colors=grey, labelsize=11)

    # left: rank correlation
    ax = axes[0]
    frame(ax, RHO_XLIM, [0.5, 0.6, 0.7, 0.8, 0.9, 1.0],
          f"Rank correlation with the published FE\nmedian {d['spearman_rho']}",
          "Spearman rho (1.0 = same ranking)")
    ax.vlines(m["spearman_rho"], top, bottom, color=ink, lw=1.6, ls=(0, (5, 3)), zorder=2)
    ax.text(m["spearman_rho"], label_y, "median", ha="center", va="center", fontsize=11, color=ink)
    ax.scatter([c["spearman_rho"] for c in cases], list(rows), s=80, color=dot, edgecolor="white", linewidth=1.2,
               zorder=3)
    w = result["worst_rho"]
    ax.text(w["value"] + 0.014, labels.index(w["case"]), f"{w['value']:.3f}", va="center", ha="left", fontsize=11,
            color=ink)

    # right: p99 ratio
    ax = axes[1]
    band = result["band"]
    frame(ax, RATIO_XLIM, [0.8, 0.9, 1.0, 1.1, 1.2],
          f"Peak (p99) tension vs the published FE\nmedian {d['p99_ratio']} ({result['median_p99_off_percent']})",
          "ours / theirs (1.0 = equal)")
    ax.axvspan(band["lo"], band["hi"], color=band_c, zorder=1, lw=0)
    ax.vlines(1.0, top, bottom, color=grey, lw=1.2, zorder=2)
    ax.vlines(m["p99_ratio"], top, bottom, color=ink, lw=1.6, ls=(0, (5, 3)), zorder=2)
    # the two lines are ~0.02 apart: "median" ends at the dashed line, the band label starts at the solid one
    ax.text(m["p99_ratio"] - 0.004, label_y, "median", ha="right", va="center", fontsize=11, color=ink)
    pct = round(100 * band["half_width"])
    ax.text(1.0 + 0.004, label_y, f"within {pct}%" if result["p99_all_within_band"] else
            f"{result['p99_cases_within_band']} of {n} within {pct}%", ha="left", va="center", fontsize=11, color=ink)
    ax.scatter([c["p99_ratio"] for c in cases], list(rows), s=80, color=dot, edgecolor="white", linewidth=1.2,
               zorder=3)
    w = result["worst_p99"]
    below = w["value"] < 1.0
    ax.text(w["value"] + (-0.014 if below else 0.014), labels.index(w["case"]), f"{w['value']:.3f}", va="center",
            ha="right" if below else "left", fontsize=11, color=ink)

    fig.text(0.04, 0.092, result["caption"], fontsize=11.5, color=ink, va="center")
    fig.text(0.04, 0.045, f"Median tension (p50) ours / theirs {d['p50_ratio']} ({result['median_p50_off_percent']}). "
             f"Data: Zenodo AAA dataset, doi {DOI}, CC BY 4.0.", fontsize=11, color=grey, va="center")
    fig.savefig(path, dpi=200, facecolor="white")
    plt.close(fig)


if __name__ == "__main__":
    raise SystemExit(main())

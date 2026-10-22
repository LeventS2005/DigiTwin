"""M6: (re)build every video figure and its index from outputs — no number typed in.

Re-run it whenever a demo number changes (a new copilot run, a new stress
result): the stills, the report preview and ``INDEX.md`` are rebuilt from the
files named below, and every number in the index is read from them.

Parts (each is skipped, with a note, when its input is missing):

* report preview: always re-rendered by THIS checkout's report.py from
  ``<run>/session.mfwork`` (journal replayed through the copilot's tool code,
  call timings from ``<run>/run.log`` if present, the canonical s1364 credit).
  Only a live copilot run (``transcript.json`` beside it) keeps its own page,
  and only when that page carries this renderer's marker. A page with raw
  markdown (``## ``, ``**``) stops the script. Then headless Chrome captures
  it (top, full page, phone width, print PDF);
* Workbench twin panel before / with the run (``--workbench``: a RUNNING
  Workbench, e.g. ``meshforge web --port 8139``; this script starts no
  server): session reset → capture, work file loaded with replay off and
  the ``--flow-metrics`` attached (``POST /twin/flow``, as the copilot does
  with ``--server``; skipped if the work file already carries them) →
  capture, the panel cropped from each;
* WSS map (``scripts/m6_wss_map.py``; the case must already hold the
  ``wallShearStress`` field — OpenFOAM is a heavy job, run it separately);
* Raghavan–Vorp vs linear chart and the stress stills, from ``--stress``
  (an ``aaa_stress`` summary .json with its .vtu beside it);
* CT overlays (``scripts/sac_overlay.py``), the normal-aorta strength chart
  (``scripts/m6_normal_strength.py``), and the CFD flow picture (copied);
* the per-case validation chart (``scripts/m6_validation_chart.py``), from
  ``--validation-rows`` (the with-thrombus tension validation's ``rows.json``:
  our wall tension vs the dataset's published FE on the 12 public AAA cases).

Run from the repository root, isolated (this checkout's src/ goes first):

    python -I scripts/m6_video_figures.py --run outputs/m5/scripted_natural_faces \\
        --stress outputs/m6/stress/session_aaa_stress.json --workbench http://127.0.0.1:8139
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
SCRIPTS = REPO / "scripts"
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
S1364_CREDIT = "TotalSegmentator dataset s1364 (Wasserthal et al.), CC BY 4.0, doi 10.5281/zenodo.10047292"


def py(script: str, *args: str) -> None:
    subprocess.run([sys.executable, "-I", str(SCRIPTS / script), *args], check=True,
                   stdout=subprocess.DEVNULL)


def chrome(out: Path, url: str, size: str, scale: float, profile: Path, *extra: str) -> None:
    subprocess.run([CHROME, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--no-first-run",
                    "--no-default-browser-check", "--disable-background-networking", "--disable-component-update",
                    "--disable-sync", "--disable-extensions", f"--user-data-dir={profile}", f"--window-size={size}",
                    f"--force-device-scale-factor={scale}", "--virtual-time-budget=8000", *extra,
                    *(() if any(e.startswith("--print-to-pdf") for e in extra) else (f"--screenshot={out}",)), url],
                   check=True, capture_output=True)


# --- report preview ---------------------------------------------------------------------------------------


def report_preview(run: Path, out_dir: Path, flow_metrics: Path | None) -> Path:
    """report.html for the figures: re-rendered from the run's work file by this
    checkout's renderer (a live copilot run keeps its own page if this renderer
    wrote it). Refuses a page that still shows raw markdown."""
    from meshforge.copilot.report import REPORT_HTML_MARKER

    out_dir.mkdir(parents=True, exist_ok=True)
    page = run / "report.html"
    live = (run / "transcript.json").is_file()
    if live and page.is_file() and REPORT_HTML_MARKER in page.read_text():
        shutil.copy2(page, out_dir / "report.html")
        if (run / "report.md").is_file():
            shutil.copy2(run / "report.md", out_dir / "report.md")
    else:
        _rerender(run, out_dir, flow_metrics)
    html = (out_dir / "report.html").read_text()
    if REPORT_HTML_MARKER not in html or "## " in html or "**" in html:
        raise SystemExit(f"{out_dir / 'report.html'}: not the HTML report (raw markdown or no renderer marker)")
    return out_dir / "report.html"


def _rerender(run: Path, out_dir: Path, flow_metrics: Path | None) -> None:
    from meshforge.copilot.report import render_report, tools_from_journal

    journal = json.loads((run / "session.mfwork").read_text())["journal"]
    tools, journal_flow, scan = tools_from_journal(journal)
    log = run / "run.log"
    if log.is_file():
        secs = [float(m.group(1)) for m in re.finditer(r"^\w+\s+(?:ok|FAILED)\s+([\d.]+) s", log.read_text(), re.M)]
        if len(secs) == len(tools.calls):
            for c, s in zip(tools.calls, secs):
                c["seconds"] = s
    md, html = render_report(
        request="(scripted dry run, natural path, no Claude)", ct_name=scan or "ct", credit=S1364_CREDIT,
        results=tools.results, calls=tools.calls, summary_text=None,
        guard_note="scripted dry run: no Claude summary", model="(none)",
        flow=(json.loads(flow_metrics.read_text()) if flow_metrics and flow_metrics.is_file()
              else journal_flow or None),
        replay_hint="meshforge replay session.mfwork")
    (out_dir / "report.md").write_text(md)
    (out_dir / "report.html").write_text(html)


def _trim_bottom(png: Path, pad: int = 40) -> None:
    """Cut the blank (white) tail of a page capture."""
    import matplotlib.image as mpimg
    import numpy as np

    img = mpimg.imread(png)
    rgb = img[..., :3] if img.dtype != np.uint8 else img[..., :3] / 255.0
    rows = np.where((rgb < 0.98).any(axis=(1, 2)))[0]
    if rows.size:
        mpimg.imsave(png, img[: min(rows.max() + pad, img.shape[0])])


def phone_capture(page: Path, out: Path, profile: Path, width: int = 390) -> None:
    """The report at phone width. Desktop headless Chrome will not make a window narrower than about
    500 px, so the page is shown in a ``width`` px iframe (its own phone layout applies) and cropped."""
    import matplotlib.image as mpimg

    frame = page.with_name("phone_frame.html")
    frame.write_text(f"<!doctype html><html><body style='margin:0;background:#fff'>"
                     f"<iframe src='{page.name}' style='width:{width}px;height:4400px;border:0;display:block'>"
                     f"</iframe></body></html>")
    chrome(out, frame.resolve().as_uri(), "640,4400", 2, profile)
    img = mpimg.imread(out)
    mpimg.imsave(out, img[:, : width * 2])
    _trim_bottom(out)


# --- Workbench twin panel --------------------------------------------------------------------------------


def _post(url: str, body: dict) -> None:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        r.read()


def _panel(img, scale: float):
    """Crop the twin panel: the right dock (392 css px at a 1600 px window) from below the top bar down to
    the next panel's title bar (a run of title-bar-coloured rows), else a fixed height."""
    import numpy as np

    x0, top = int(1208 * scale), int(30 * scale)
    col = (img[:, int(1590 * scale), :3] * 255).round() if img.dtype != np.uint8 else img[:, int(1590 * scale), :3]
    bar = (col[:, 0] >= 38) & (col[:, 0] <= 46)
    run, bottom = 0, int(504 * scale)
    for y in range(int(80 * scale), img.shape[0]):
        run = run + 1 if bar[y] else 0
        if run >= int(15 * scale):
            bottom = y - run - 1
            break
    return img[top:bottom, x0:]


def workbench(url: str, run: Path, out: Path, profile: Path, flow_metrics: Path | None = None) -> list[str]:
    """01 (empty session) and 02 (the run's work file, replay off, plus the flow metrics the copilot
    attaches with ``--flow-metrics``, unless the work file already carries a ``flow_metrics`` record)."""
    import matplotlib.image as mpimg

    wf = (run / "session.mfwork").resolve()
    attach = (flow_metrics is not None and flow_metrics.is_file()
              and not any(r.get("step") == "flow_metrics" for r in json.loads(wf.read_text())["journal"]))
    made = []
    for state, name in (("before", "01_twin_card_workbench_before"), ("run", "02_twin_card_workbench_s1364")):
        if state == "before":
            _post(f"{url}/session/reset", {})
        else:
            _post(f"{url}/workfile/load", {"path": str(wf), "replay": False})
            if attach:  # a 400 (a set the session refuses) stops the build loudly
                _post(f"{url}/twin/flow", {"metrics": json.loads(flow_metrics.read_text())})
        full = out / f"{name}_fullwindow.png"
        chrome(full, f"{url}/app/", "1600,1000", 2, profile)
        mpimg.imsave(out / f"{name}.png", _panel(mpimg.imread(full), 2))
        made += [f"{name}.png", full.name]
    return made


# --- index --------------------------------------------------------------------------------------------------


def _j(path: Path):
    return json.loads(path.read_text()) if path.is_file() else None


def write_index(out: Path, made: dict[str, str], skipped: list[str], src: dict[str, Path]) -> None:
    from meshforge.copilot.report import tools_from_journal

    stress, wss, rv, normal = (_j(src[k]) for k in ("stress", "wss", "rv", "normal"))
    val = _j(src["validation"]) if "10_validation_12cases.png" in made else None  # only when this build made it
    res = {}
    if (src["run"] / "session.mfwork").is_file():
        res = tools_from_journal(json.loads((src["run"] / "session.mfwork").read_text())["journal"])[0].results
    lines = ["# M6 video figures — built by `scripts/m6_video_figures.py` (re-run it after any number changes)", "",
             f"Demo case: {S1364_CREDIT}. Every number below is read from the files listed, not typed in.", ""]
    st, idx = res.get("wall_stress"), res.get("strength_index")
    if st:
        bands = {k: v for k, v in (idx or {}).items() if k.endswith("mmhg")}
        lines.append(f"- Copilot run `{src['run'].name}`: PWS {st['pws_systolic_kpa']} / {st['pws_whatif_kpa']} kPa at "
                     f"{st['systolic_mmhg']:g} / {st['whatif_mmhg']:g} mmHg ({st['pws_change_percent']} %)"
                     + "".join(f"; index ({k.replace('_', ' ')}) P10/P50/P90 {b['index_p10']} / {b['index_p50']} / "
                               f"{b['index_p90']}" for k, b in bands.items()))
    if stress:
        lines.append(f"- Stress summary `{src['stress'].name}`: PWS {stress['systolic']['pws_kpa']:.1f} kPa at "
                     f"{stress['systolic']['pressure_mmhg']:g} mmHg, thrombus modelled "
                     f"{bool((stress.get('thrombus') or {}).get('modelled'))}, max displacement "
                     f"{stress['systolic']['u_max_mm']:.3f} mm")
    lines += ["", "| file | what it shows | made from |", "|---|---|---|"]
    lines += [f"| `{f}` | {what} |" for f, what in made.items()]
    if wss:
        lines += ["", f"## WSS (`{src['wss'].name}`)", "",
                  f"Steady CFD case `{wss['case']}`; rho {wss['rho_kg_m3']:g} kg/m^3, nu {wss['nu_mm2_s']:g} mm^2/s. "
                  f"{wss['conversion']}. Surface: {wss['surface']}. Dilated: {wss['dilated_rule']}.", "",
                  "| | dilated lumen | rest of the lumen | whole lumen |", "|---|---|---|---|"]
        cols = [wss["dilated_wall"], wss["other_wall"], wss["whole_wall"]]
        for key, label, f in (("area_mm2", "surface area (cm^2)", lambda v: f"{v / 100:.1f}"),
                              ("p50_pa", "median (Pa)", lambda v: f"{v:.3f}"), ("mean_pa", "mean (Pa)", lambda v: f"{v:.3f}"),
                              ("p5_pa", "5th percentile (Pa)", lambda v: f"{v:.3f}"),
                              ("p95_pa", "95th percentile (Pa)", lambda v: f"{v:.3f}"), ("max_pa", "max (Pa)", lambda v: f"{v:.3f}"),
                              ("area_below_0.4_pa_percent", "area below 0.4 Pa (%)", lambda v: f"{v:.1f}")):
            lines.append(f"| {label} | " + " | ".join(f(c[key]) for c in cols) + " |")
        lines.append(f"\nLow-WSS line: {wss['low_wss_source']}. Descriptive only.")
    if rv:
        r = rv["raghavan_vorp"]
        lines += ["", f"## Raghavan–Vorp vs linear (`{src['rv'].name}`)", "",
                  f"At PWS {rv['pws_kpa']} kPa: tissue law {r['strain_at_pws_equibiaxial_percent']} % equibiaxial / "
                  f"{r['strain_at_pws_uniaxial_percent']} % uniaxial stretch; solver wall (E {rv['linear']['E_mpa']:g} MPa, "
                  f"nu {rv['linear']['nu']:g}) {rv['linear']['strain_at_pws_equibiaxial_percent']} %. "
                  f"R-V alpha {r['alpha_mpa']} / beta {r['beta_mpa']} MPa ({r['source']})."]
    if normal:
        lines += ["", f"## Wall strength bands (`{src['normal'].name}`, Cauchy, first-peak rule, empirical)", "",
                  "| band | strips / aortas | P10 | P50 | P90 (MPa) |", "|---|---|---|---|---|"]
        for b in normal["bands"].values():
            lines.append(f"| {b['label'].replace(chr(10), ' ')} | {b['n_strips']} / {b['n_aortas']} | {b['p10_mpa']} | "
                         f"{b['p50_mpa']} | {b['p90_mpa']} |")
        chk = normal.get("txt_method_check_on_aaa")
        lines.append(f"\n{normal['normal_source']}. Demo index {normal['demo_index']}."
                     + (f" TXT method check on the AAA reports: {chk['n_strips']} strips within "
                        f"{chk['max_abs_rel_diff_percent']} % of the library." if chk else ""))
    if val:
        d, band = val["medians_3dp"], round(100 * val["band"]["half_width"])
        lines += ["", f"## Validation (`{src['validation'].name}`)", "",
                  f"{val['caption']}. Medians over {val['n_cases']} cases: rank correlation {d['spearman_rho']}, p99 tension "
                  f"ratio ours / theirs {d['p99_ratio']} ({val['median_p99_off_percent']}), p50 ratio {d['p50_ratio']} "
                  f"({val['median_p50_off_percent']}); {val['p99_cases_within_band']} of {val['n_cases']} cases within "
                  f"{band} % at p99 (largest {val['p99_max_abs_deviation_percent']} %). Rows `{val['rows_file']}` "
                  f"(sha256 {val['rows_sha256'][:12]}), variant `{val['variant']}`: {val['variant_meaning']}. Their "
                  "surfaces are a reference segmentation and their tension is the published FE result; the chart "
                  "compares tension, it does not measure accuracy."]
    if skipped:
        lines += ["", "## Skipped", ""] + [f"- {s}" for s in skipped]
    (out / "INDEX.md").write_text("\n".join(lines) + "\n")


# --- main ------------------------------------------------------------------------------------------------------


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--run", type=Path, required=True, help="a copilot run folder (session.mfwork, report.*)")
    ap.add_argument("--stress", type=Path, required=True, help="the run's aaa_stress summary .json (.vtu beside it)")
    ap.add_argument("--out", type=Path, default=Path("outputs/m6/figures"))
    ap.add_argument("--workbench", default=None, help="a running Workbench, e.g. http://127.0.0.1:8139")
    ap.add_argument("--flow-metrics", type=Path, default=Path("outputs/m4/cfd/s1364_flow_metrics.json"),
                    help="pre-computed CFD metrics: the report preview's flow slot, and attached to the "
                    "Workbench session before the 02 capture (a missing file skips both)")
    ap.add_argument("--flow-png", type=Path, default=Path("outputs/m4/cfd/s1364_flow.png"))
    ap.add_argument("--wss-case", type=Path, default=Path("outputs/m6/wss/s1364_case"))
    ap.add_argument("--dilated-mm", default="28.15", help="the crop's non-dilated threshold (WSS split)")
    ap.add_argument("--ct", type=Path, default=Path("outputs/data/totalseg_v2/ct/s1364/ct.nii.gz"))
    ap.add_argument("--lumen-mask", type=Path, default=Path("outputs/m3/s1364/s1364_aorta_mask.nii.gz"))
    ap.add_argument("--sac-mask", type=Path, default=Path("outputs/m3/s1364/s1364_sac_mask.nii.gz"))
    ap.add_argument("--usp-normal", type=Path, default=Path("outputs/data/usp_normal"))
    ap.add_argument("--usp-manifest", type=Path, default=Path("outputs/m6/usp_normal_manifest.json"))
    ap.add_argument("--usp-aaa", type=Path, default=Path("outputs/data/usp_aaa_x64srrc39p_v3"))
    ap.add_argument("--validation-rows", type=Path, default=Path("outputs/m3/tension_validation_ilt/rows.json"),
                    help="the with-thrombus tension validation's per-case rows (a missing file skips the validation chart)")
    a = ap.parse_args(argv)

    out, work = a.out, a.out.parent
    out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="m6_chrome_") as tmp:  # a throwaway browser profile
        return _build(a, out, work, Path(tmp))


def _build(a, out: Path, work: Path, profile: Path) -> int:
    made, skipped = {}, []
    have_chrome = Path(CHROME).is_file()

    page = report_preview(a.run, work / "report_preview", a.flow_metrics)
    if have_chrome:
        url = page.resolve().as_uri()
        chrome(out / "03_report_html_top.png", url, "1280,800", 2, profile)
        chrome(out / "03_report_html_fullpage.png", url, "1280,3000", 1.5, profile)
        _trim_bottom(out / "03_report_html_fullpage.png")
        phone_capture(page, out / "03_report_html_phone.png", profile)
        chrome(out / "03_report_html_print.pdf", url, "1280,800", 1, profile, "--no-pdf-header-footer",
               f"--print-to-pdf={out / '03_report_html_print.pdf'}")
        made["03_report_html_top.png"] = ("report.html as a page: header, twin card, headline tiles | "
                                          f"{page.parent.name}/report.html (headless Chrome 2x)")
        made["03_report_html_fullpage.png"] = ("the whole report (results, pipeline with the gate, guard, caveats, "
                                               f"credit) | {page.parent.name}/report.html (headless Chrome 1.5x)")
        made["03_report_html_phone.png"] = (f"the report at phone width (390 px, its one-column layout) | "
                                            f"{page.parent.name}/report.html in a 390 px frame (2x)")
        made["03_report_html_print.pdf"] = f"print/PDF proof | {page.parent.name}/report.html (Chrome print-to-PDF)"
    else:
        skipped.append("report captures: no headless Chrome at " + CHROME)
    if a.workbench and have_chrome:
        flow_note = (f" + flow metrics `{a.flow_metrics.name}` attached (POST /twin/flow)"
                     if a.flow_metrics.is_file() else "")
        for f in workbench(a.workbench.rstrip("/"), a.run, out, profile, a.flow_metrics):
            before = "before" in f
            made[f] = ("Workbench twin panel " + ("before a run (every slot waits)" if before else
                       "filled from the run's journal") + (" — whole window" if "fullwindow" in f else "")
                       + f" | {a.workbench} " + ("after a session reset" if before else
                       f"+ work file `{a.run.name}/session.mfwork` loaded with replay off{flow_note}"))
    else:
        skipped.append("Workbench twin panel: pass --workbench <url of a running Workbench>")
    if (a.wss_case / "constant").is_dir():
        py("m6_wss_map.py", str(a.wss_case), str(work / "wss" / "s1364_wss"), "--dilated-mm", a.dilated_mm,
           "--label", S1364_CREDIT)
        shutil.copy2(work / "wss" / "s1364_wss.png", out / "04_wss_s1364.png")
        shutil.copy2(work / "wss" / "s1364_wss_render.png", out / "04b_wss_s1364_views.png")
        made["04_wss_s1364.png"] = "wall shear stress on the lumen + its distribution | scripts/m6_wss_map.py"
        made["04b_wss_s1364_views.png"] = "the two WSS views alone (anterior, left lateral) | scripts/m6_wss_map.py"
    else:
        skipped.append(f"WSS: no case at {a.wss_case}")
    if a.stress.is_file() and a.stress.with_suffix(".vtu").is_file():
        py("m6_rv_vs_linear.py", str(a.stress), str(work / "rv_vs_linear"))
        shutil.copy2(work / "rv_vs_linear.png", out / "05_rv_vs_linear.png")
        py("m6_stress_stills.py", str(a.stress.with_suffix(".vtu")), str(work / "stress" / "s1364_stress"),
           "--label", "TotalSegmentator s1364 (Wasserthal et al.), CC BY 4.0")
        for src, dst in (("s1364_stress_140.png", "06_stress_s1364_140.png"),
                         ("s1364_stress_140_vs_120.png", "06b_stress_s1364_140_vs_120.png")):
            if (work / "stress" / src).is_file():
                shutil.copy2(work / "stress" / src, out / dst)
                made[dst] = f"von Mises wall stress (thrombus caption from the summary) | scripts/m6_stress_stills.py on {a.stress.name}"
        made["05_rv_vs_linear.png"] = f"Raghavan–Vorp vs the solver's stiff-linear wall | scripts/m6_rv_vs_linear.py on {a.stress.name}"
    else:
        skipped.append(f"stress stills + R-V chart: no {a.stress} with a .vtu beside it")
    if a.ct.is_file() and a.lumen_mask.is_file() and a.sac_mask.is_file():
        py("sac_overlay.py", str(a.ct), str(a.lumen_mask), str(a.sac_mask), str(work / "overlay"), "s1364", "45", "61")
        for h, dst in (("45", "07_ct_overlay_s1364_h45.png"), ("61", "07b_ct_overlay_s1364_h61.png")):
            shutil.copy2(work / "overlay" / f"s1364_sac_h{h}.png", out / dst)
            made[dst] = f"axial CT: AI aorta outline (red) + recovered sac (green), {h} mm up | scripts/sac_overlay.py"
    else:
        skipped.append("CT overlays: CT or masks missing")
    if a.usp_manifest.is_file():
        extra = ["--validate-aaa", str(a.usp_aaa)] if (a.usp_aaa / "_sha256.txt").is_file() else []
        py("m6_normal_strength.py", str(a.usp_normal), str(a.usp_manifest), str(work / "normal_strength"),
           "--pws-summary", str(a.stress), *extra)
        shutil.copy2(work / "normal_strength.png", out / "09_normal_vs_aaa_strength.png")
        made["09_normal_vs_aaa_strength.png"] = ("wall strength bands: AAA lab v0 (the index) vs normal aorta | "
                                                 "scripts/m6_normal_strength.py")
    else:
        skipped.append("normal-aorta strength: no download manifest")
    if a.validation_rows.is_file():
        py("m6_validation_chart.py", str(a.validation_rows), str(out / "10_validation_12cases"))
        v = _j(out / "10_validation_12cases.json")
        d = v["medians_3dp"]
        made["10_validation_12cases.png"] = (
            f"per-case validation: our wall tension vs the dataset's published FE on {v['n_cases']} public AAA cases "
            f"(thrombus modelled); medians: rank correlation {d['spearman_rho']}, p99 ratio {d['p99_ratio']}, "
            f"p50 ratio {d['p50_ratio']} | scripts/m6_validation_chart.py on "
            f"{a.validation_rows.parent.name}/{a.validation_rows.name}")
    else:
        skipped.append(f"validation chart: no rows file at {a.validation_rows}")
    if a.flow_png.is_file():
        shutil.copy2(a.flow_png, out / "08_flow_s1364.png")
        made["08_flow_s1364.png"] = f"steady CFD flow (M4) | copy of {a.flow_png}"
    write_index(out, dict(sorted(made.items())), skipped,
                {"run": a.run, "stress": a.stress, "wss": work / "wss" / "s1364_wss.json",
                 "rv": work / "rv_vs_linear.json", "normal": work / "normal_strength.json",
                 "validation": out / "10_validation_12cases.json"})
    print(f"{len(made)} figures in {out}; skipped: {skipped or 'none'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

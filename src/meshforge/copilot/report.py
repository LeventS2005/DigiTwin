"""The copilot's report: deterministic, built only from tool outputs.

It opens with the **twin card**, a personal twin that grows one module at a
time. The aorta is module 1, lit; heart and kidneys are dimmed "next". The
data layers are slots that are either filled (shape, strength, stress, flow,
blood-pressure what-if) or waiting (growth needs a second scan). The prose
summary Claude writes is included only if it passed the numbers guard.
"""

from __future__ import annotations

import html
import re
from pathlib import PurePath
from typing import Any

CAVEATS = (
    "public CC-BY scan; no patient data",
    "investigational index, not a diagnosis and not a rupture prediction",
    "wall thickness assumed 1.5 mm",
)

_BODY_SVG = """<svg viewBox="0 0 120 260" width="120" height="260" role="img"
 aria-label="body outline with the aorta highlighted">
 <g fill="none" stroke="#9aa3ad" stroke-width="2">
  <circle cx="60" cy="24" r="16"/>
  <path d="M42 48 Q60 40 78 48 L84 130 Q60 138 36 130 Z"/>
  <path d="M42 52 L22 120 M78 52 L98 120"/>
  <path d="M46 134 L40 240 M74 134 L80 240"/>
 </g>
 <ellipse cx="66" cy="70" rx="9" ry="8" fill="#e8b4b4" opacity="0.35"/>
 <ellipse cx="50" cy="104" rx="5" ry="8" fill="#c9a96e" opacity="0.30"/>
 <ellipse cx="70" cy="104" rx="5" ry="8" fill="#c9a96e" opacity="0.30"/>
 <path d="M62 62 Q70 54 64 70 L62 120 L56 134 M62 120 L68 134"
  stroke="#d1342f" stroke-width="4" fill="none" stroke-linecap="round"/>
 <circle cx="62" cy="112" r="5" fill="#d1342f"/>
</svg>"""


def _slots(results: dict[str, dict], flow: dict | None) -> list[tuple[str, str, bool, str]]:
    seg, stress, idx = results.get("segment_aorta"), results.get("wall_stress"), results.get("strength_index")
    n = f"open data, {idx['n_aneurysms']} aneurysms / {idx['n_strips']} strips" if idx else ""
    return [
        ("Shape", "CT → AI outline (TotalSegmentator)", bool(seg), ""),
        ("Wall + thrombus", "recovered from the CT", bool(seg), ""),
        ("Stress", "finite elements under blood pressure", bool(stress), ""),
        ("Strength", "lab v0 — " + n if n else "lab v0", bool(idx), "wide band: our lab narrows it"),
        ("Blood flow", "CFD (steady, resting flow)", bool(flow), "" if flow else "not run here"),
        ("BP what-if", "lower pressure → lower stress", bool(stress), ""),
        ("Growth", "needs a second scan", False, "next"),
    ]


def twin_card_html(results: dict[str, dict], flow: dict | None = None) -> str:
    rows = "".join(
        f'<tr class="{"on" if ok else "off"}"><td>{"●" if ok else "○"}</td>'
        f"<td><b>{html.escape(k)}</b></td><td>{html.escape(v)}</td>"
        f'<td class="note">{html.escape(note)}</td></tr>'
        for k, v, ok, note in _slots(results, flow)
    )
    return (
        '<section class="twin"><h2>Your twin · v0</h2><div class="twin-grid">'
        f'<div>{_BODY_SVG}<p class="mods"><span class="lit">● aorta: module 1</span><br>'
        '○ heart: next<br>○ kidneys: next</p></div>'
        f'<table class="slots">{rows}</table></div></section>'
    )


def _fmt(v: Any) -> str:
    return "—" if v is None else str(v)


def _shown_input(args: dict[str, Any]) -> dict[str, Any]:
    """A tool's input as the report shows it: a path by its file name only,
    so no local folder (or user name) ends up on screen."""
    return {k: PurePath(v).name if k.endswith("_path") and isinstance(v, str) else v
            for k, v in args.items()}


def _first_sentence(text: str, limit: int = 220) -> str:
    """Up to the first full stop that ends a sentence (not the one in 1.30)."""
    s = re.split(r"\.(?:\s|$)", text, maxsplit=1)[0]
    return s if len(s) <= limit else s[: limit - 1] + "…"


def render_report(*, request: str, ct_name: str, credit: str, results: dict[str, dict],
                  calls: list[dict], summary_text: str | None, guard_note: str,
                  model: str, flow: dict | None = None,
                  replay_hint: str = "") -> tuple[str, str]:
    """(markdown, html) of the run, from the tool outputs only."""
    seg = results.get("segment_aorta", {})
    crop = results.get("crop_aneurysm", {})
    wall = results.get("mesh_wall", {})
    st = results.get("wall_stress", {})
    idx = results.get("strength_index", {})
    bands = {k: v for k, v in idx.items() if k.endswith("mmhg")}

    md = [f"# Aorta twin — investigational biomechanics report", "",
          f"**Request:** {request}", f"**Scan:** {ct_name} ({credit})", f"**Copilot:** {model}", ""]
    md += ["## Results", "",
           f"- Sac: maximum diameter {_fmt(seg.get('sac_max_diameter_mm'))} mm "
           f"(area-equivalent {_fmt(seg.get('sac_area_equivalent_diameter_mm'))} mm), "
           f"thrombus + wall up to {_fmt(seg.get('thrombus_plus_wall_thickness_mm_max'))} mm",
           f"- Crop: sac {_fmt(crop.get('sac_diameter_mm'))} mm vs neck "
           f"{_fmt(crop.get('neck_diameter_mm'))} mm (ratio {_fmt(crop.get('sac_to_neck_ratio'))})",
           f"- Wall mesh: {_fmt(wall.get('hex_cells'))} hex, {_fmt(wall.get('inverted_cells'))} inverted, "
           f"{_fmt(wall.get('capped_rays'))} capped, {_fmt(wall.get('floored_rays'))} floored, "
           f"{_fmt(wall.get('wall_thickness_measured_mm'))} mm thick along the surface normal — "
           f"gate {_fmt(wall.get('gate'))}",
           f"- Peak wall stress: {_fmt(st.get('pws_systolic_kpa'))} kPa at "
           f"{_fmt(st.get('systolic_mmhg'))} mmHg, {_fmt(st.get('pws_whatif_kpa'))} kPa at "
           f"{_fmt(st.get('whatif_mmhg'))} mmHg ({_fmt(st.get('pws_change_percent'))} %)"]
    for k, b in bands.items():
        md.append(f"- Index ({k.replace('_', ' ')}): P10 {b['index_p10']} · P50 {b['index_p50']} · "
                  f"P90 {b['index_p90']}")
    if idx:
        md.append(f"- Strength: open tensile data from {idx['n_aneurysms']} aneurysms, "
                  f"{idx['n_strips']} strips — our lab narrows this band")
    md += ["", "## Pipeline (every action journaled)", "",
           "| # | tool | input | result | s |", "|---|---|---|---|---|"]
    for i, c in enumerate(calls, 1):
        res = "ok" if c["ok"] else "FAILED → " + _first_sentence(c["error"])
        md.append(f"| {i} | {c['tool']} | `{_shown_input(c['input'])}` | {res} | {c['seconds']} |")
    md += ["", "## Copilot summary", "", summary_text or f"_({guard_note})_", "",
           "## Caveats", ""] + [f"- {c}" for c in CAVEATS]
    if replay_hint:
        md += ["", "## Reproduce", "", f"`{replay_hint}`"]
    markdown = "\n".join(md) + "\n"

    body = html.escape(markdown).replace("\n", "<br>\n")
    page = (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>Aorta twin report</title><style>"
        "body{font:15px/1.5 -apple-system,Segoe UI,sans-serif;max-width:900px;margin:24px auto;"
        "padding:0 16px;color:#1d232a;background:#fff}"
        ".twin{border:1px solid #d7dde3;border-radius:10px;padding:12px 16px;margin-bottom:20px}"
        ".twin h2{margin:0 0 8px;font-size:18px}.twin-grid{display:flex;gap:20px;flex-wrap:wrap}"
        ".slots td{padding:3px 8px}.slots tr.off{color:#8a949e}.slots tr.on td:first-child{color:#d1342f}"
        ".note{font-size:12px;color:#8a949e}.lit{color:#d1342f;font-weight:600}.mods{font-size:13px}"
        "</style></head><body>"
        + twin_card_html(results, flow) + f"<div class='md'>{body}</div></body></html>"
    )
    return markdown, page

"""D92 (M5): the Claude copilot, with a mocked client and a scripted backend.

No network, no API key, no heavy compute: the client replays scripted
responses and the backend returns canned step results. The loop, the gate
recovery, the numbers guard, the request shape and the report are tested.
"""
from __future__ import annotations

import builtins
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace as NS

import pytest

from meshforge.copilot import agent as ag
from meshforge.copilot.backend import HttpBackend, StepOutcome
from meshforge.copilot.guard import check_numbers, numbers_in
from meshforge.copilot.report import render_report, twin_card_html
from meshforge.copilot.tools import TOOLS, PipelineTools


# --- fakes ---------------------------------------------------------------------


def _use(name, args, i):
    return NS(type="tool_use", name=name, input=args, id=f"tu_{i}")


def _resp(*blocks, stop="tool_use"):
    return NS(content=list(blocks), stop_reason=stop, model="claude-opus-5-5",
              usage=NS(input_tokens=100, output_tokens=20))


def _text(t):
    return NS(type="text", text=t)


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.beta = NS(messages=NS(create=self._create))

    def _create(self, **kw):
        # snapshot the history as sent (the real SDK serialises at call time)
        self.calls.append({**kw, "messages": list(kw["messages"])})
        return self.responses.pop(0)


class FakeBackend:
    """Canned steps shaped like the real s1364 run (D93): the centreline sweep
    builds a clean wall that is 1.30 mm thick along the normal (gate FAIL);
    the plane sweep refuses caps that are not parallel, and on parallel caps
    builds the 1.50 mm wall that passes."""

    name = "fake"

    def __init__(self, planes_thickness=1.5014, centreline_thickness=1.3013):
        self.steps = []
        self.parallel = False
        self.thickness = {"planes": planes_thickness, "centreline": centreline_thickness}

    def run(self, step, knobs):
        self.steps.append((step, dict(knobs)))
        # the session's real shapes: payload nested under the step name
        if step == "segment":
            return StepOutcome(True, {"segment": {"totalseg": {"wall_time_s": 49.5},
                                                  "mask_volume_ml": 332.6}}, "")
        if step == "sac":
            return StepOutcome(True, {"sac": {"max_diameter": {"max_caliper": {"value_mm": 61.75},
                                                               "area_equivalent": {"value_mm": 52.17}},
                                              "sac_volume_ml": 161.8, "resolved_fraction": 0.661,
                                              "thickness_mm": {"all_rays": {"median": 3.17, "max": 25.52}}}}, "")
        if step == "crop":
            self.parallel = bool(knobs["parallel_caps"])
            return StepOutcome(True, {"crop": {"landmarks": {"sac_d_equiv_mm": 47.6,
                                                             "neck_d_equiv_mm": 20.07,
                                                             "sac_neck_ratio": 2.37}}}, "")
        if step == "volmesh":
            sweep = "centreline" if knobs["sweep"] == "auto" else knobs["sweep"]
            if sweep == "planes" and not self.parallel:
                return StepOutcome(False, {}, "", "ValueError: the end caps are 35.20 deg from "
                                   "parallel (the plane sweep needs <= 2); crop with parallel caps")
            return StepOutcome(True, {"n_cells": 7552, "quality": {"n_inverted": 0},
                                      "lattice": {"n_capped": 0, "n_floored": 0, "sweep": sweep,
                                                  "thickness_normal_median": self.thickness[sweep]}},
                               "")
        if step == "aaa_stress":
            return StepOutcome(True, {"refused": [], "systolic": {"von_mises_p99_mpa": 0.278243},
                                      "whatif": {"von_mises_p99_mpa": 0.238494},
                                      "whatif_ratio": {"pws": 0.857143},
                                      "thrombus": {"modelled": True}}, "")
        if step == "tissue_index":
            p = knobs["pws_mpa"]
            return StepOutcome(True, {"index": {"p10": p / 1.299, "p50": p / 0.650, "p90": p / 0.308},
                                      "strength_mpa": {"p10": 0.308, "p50": 0.650, "p90": 1.299},
                                      "n_aneurysms": 7, "n_strips": 13}, "")
        return StepOutcome(False, {}, "", f"unexpected step {step}")


GOOD_SUMMARY = (
    "The aneurysm sac measures 61.8 mm at its widest. The first wall mesh was only 1.3 mm thick "
    "along the surface normal (-13.2%), so it failed its quality gate and was rebuilt after "
    "re-cropping with parallel caps. With the thrombus modelled, the "
    "peak wall stress is 278.2 kPa at 140 mmHg and 238.5 kPa at 120 mmHg, a drop of 14.3%. The "
    "investigational index is 0.43 at P50 (0.21 to 0.90), from open strength data on 7 "
    "aneurysms and 13 strips - an investigational index, not a diagnosis."
)


def _script(final=GOOD_SUMMARY):
    seq = [("segment_aorta", {"ct_path": "ct.nii.gz"}), ("crop_aneurysm", {"parallel_caps": False}),
           ("mesh_wall", {"sweep": "auto"}), ("crop_aneurysm", {"parallel_caps": True}),
           ("mesh_wall", {"sweep": "planes"}),
           ("wall_stress", {"systolic_mmhg": 140, "whatif_mmhg": 120}), ("strength_index", {})]
    return [_resp(_use(n, a, i)) for i, (n, a) in enumerate(seq)] + [_resp(_text(final), stop="end_turn")]


def _run(tmp_path, responses, request="Assess this AAA at 140 and 120 mmHg."):
    client = FakeClient(responses)
    tools = PipelineTools(FakeBackend(), tmp_path / "work")
    run = ag.run_copilot(request, tools=tools, client=client, log=lambda m: None)
    return run, client, tools


# --- the loop ------------------------------------------------------------------


def test_full_run_recovers_from_a_failed_gate(tmp_path):
    run, client, tools = _run(tmp_path, _script())
    assert run.stop == "done" and run.complete
    failed = [c for c in run.calls if not c["ok"]]
    assert [c["tool"] for c in failed] == ["mesh_wall"]
    assert "quality gate" in failed[0]["error"] or "parallel" in failed[0]["error"]
    # the failed call reached Claude as an error tool_result, then the recovery ran
    tr = [m for m in run.transcript if m.get("tool_results")]
    assert any(r.get("is_error") for m in tr for r in m["tool_results"])
    assert run.results["mesh_wall"]["gate"] == "PASS"
    assert run.results["wall_stress"]["pws_systolic_kpa"] == 278.2
    seg = run.results["segment_aorta"]
    assert seg["ai_seconds"] == 49.5 and seg["sac_max_diameter_mm"] == 61.8  # nested read
    assert run.summary_text == GOOD_SUMMARY and "traced" in run.guard_note
    assert tools.backend.steps[0] == ("segment", {"path": "ct.nii.gz",
                                                  "lumen_out": str(tmp_path / "work" / "lumen.vtp"),
                                                  "mask_out": str(tmp_path / "work" / "aorta_mask.nii.gz")})


def test_request_shape_follows_the_api_guidance(tmp_path):
    _, client, _ = _run(tmp_path, _script())
    kw = client.calls[0]
    assert kw["model"] == "claude-opus-5-5"
    assert kw["thinking"] == {"type": "adaptive"}
    assert kw["output_config"]["effort"] == "high"
    assert kw["fallbacks"] == "default" and "server-side-fallback-2026-07-01" in kw["betas"]
    assert "tool_choice" not in kw  # forced tool use is a 400 on this model
    assert {t["name"] for t in kw["tools"]} == {
        "segment_aorta", "crop_aneurysm", "mesh_wall", "wall_stress", "strength_index"}
    assert all(t["strict"] and t["input_schema"]["additionalProperties"] is False for t in TOOLS)
    # append-only history: every request extends the previous one
    for a, b in zip(client.calls, client.calls[1:]):
        assert b["messages"][: len(a["messages"])] == a["messages"]


def test_stress_is_refused_before_a_wall_passes_the_gate(tmp_path):
    tools = PipelineTools(FakeBackend(), tmp_path)
    r = tools.run("wall_stress", {"systolic_mmhg": 140, "whatif_mmhg": 120})
    assert not r.ok and "quality gate" in r.error


def test_the_gate_fails_a_wall_thinner_than_its_nominal_thickness(tmp_path):
    """D93: on s1364 the centreline-swept wall was clean (0/0/0) but 1.30 mm
    thick along the normal, and read 382 vs 279 kPa."""
    tools = PipelineTools(FakeBackend(), tmp_path)
    tools.run("segment_aorta", {"ct_path": "ct.nii.gz"})
    tools.run("crop_aneurysm", {"parallel_caps": False})
    r = tools.run("mesh_wall", {"sweep": "auto"})
    assert not r.ok and not tools.gate_passed and r.payload["gate"] == "FAIL"
    assert r.payload["inverted_cells"] == 0 and r.payload["wall_thickness_measured_mm"] == 1.3
    assert r.payload["wall_thickness_off_percent"] == -13.2
    assert "1.30 mm thick along the surface normal, -13% off the 1.5 mm" in r.error
    assert "parallel_caps=true" in r.error and "sweep 'planes'" in r.error
    tools.run("crop_aneurysm", {"parallel_caps": True})
    ok = tools.run("mesh_wall", {"sweep": "planes"})
    assert ok.ok and tools.gate_passed and ok.payload["wall_thickness_measured_mm"] == 1.5
    # 4.9% off passes, 5.1% fails
    for t, passes in ((1.5 * 1.049, True), (1.5 * 0.949, False)):
        tl = PipelineTools(FakeBackend(planes_thickness=t), tmp_path)
        tl.run("segment_aorta", {"ct_path": "ct.nii.gz"})
        tl.run("crop_aneurysm", {"parallel_caps": True})
        assert tl.run("mesh_wall", {"sweep": "planes"}).ok is passes


def test_a_thin_plane_swept_wall_gets_no_false_recovery_hint(tmp_path):
    tools = PipelineTools(FakeBackend(planes_thickness=1.2), tmp_path)
    tools.run("segment_aorta", {"ct_path": "ct.nii.gz"})
    tools.run("crop_aneurysm", {"parallel_caps": True})
    r = tools.run("mesh_wall", {"sweep": "planes"})
    assert not r.ok and "No setting is known" in r.error and "parallel_caps" not in r.error


def test_an_unmeasured_wall_thickness_fails_the_gate(tmp_path):
    tools = PipelineTools(FakeBackend(planes_thickness=None), tmp_path)
    tools.run("segment_aorta", {"ct_path": "ct.nii.gz"})
    tools.run("crop_aneurysm", {"parallel_caps": True})
    r = tools.run("mesh_wall", {"sweep": "planes"})
    assert not r.ok and "could not be measured" in r.error


def test_guard_traces_numbers_from_a_failed_gate(tmp_path):
    run, _, _ = _run(tmp_path, _script())
    assert "1.3 mm" in run.summary_text and "traced" in run.guard_note


def test_guard_asks_once_then_accepts_a_corrected_summary(tmp_path):
    bad = GOOD_SUMMARY + " Rupture risk is 12% per year."
    responses = _script(final=bad) + [_resp(_text(GOOD_SUMMARY), stop="end_turn")]
    run, client, _ = _run(tmp_path, responses)
    assert run.summary_text == GOOD_SUMMARY and "after one correction" in run.guard_note
    assert "12" in client.calls[-1]["messages"][-1]["content"]


def test_guard_withholds_a_summary_that_stays_untraceable(tmp_path):
    bad = GOOD_SUMMARY + " Rupture risk is 12% per year."
    responses = _script(final=bad) + [_resp(_text(bad), stop="end_turn")]
    run, _, _ = _run(tmp_path, responses)
    assert run.summary_text is None and "withheld" in run.guard_note and "12" in run.guard_note


def test_a_refusal_stops_the_run_loudly(tmp_path):
    run, _, _ = _run(tmp_path, [_resp(_text(""), stop="refusal")])
    assert run.stop == "refusal" and not run.complete and run.summary_text is None


def test_missing_sdk_is_a_readable_error(monkeypatch, tmp_path):
    real = builtins.__import__

    def fake(name, *a, **k):
        if name == "anthropic":
            raise ImportError("no anthropic")
        return real(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake)
    with pytest.raises(RuntimeError, match="Anthropic SDK"):
        ag.run_copilot("x", tools=PipelineTools(FakeBackend(), tmp_path))


# --- guard ---------------------------------------------------------------------


def test_numbers_guard_rules():
    src = [{"pws_systolic_kpa": 278.2, "index_p50": 0.43, "change": -14.3, "n": 7}]
    assert numbers_in("P10 P50 s1364 C3D10 at 140 mmHg") == ["140"]
    assert check_numbers("278 kPa, 0.43, drops 14.3%, 7 aneurysms", src) == []
    assert check_numbers("280 kPa", src) == ["280"]  # not a rounding of 278.2
    assert check_numbers("at 120 mmHg", src, allowed_text="use 120 mmHg") == []


# --- report / twin card --------------------------------------------------------


def test_report_has_the_twin_card_and_only_tool_numbers(tmp_path):
    run, _, _ = _run(tmp_path, _script())
    md, page = render_report(request="r", ct_name="ct.nii.gz", credit="CC BY 4.0",
                             results=run.results, calls=run.calls, summary_text=run.summary_text,
                             guard_note=run.guard_note, model="claude-opus-5-5")
    assert "Your twin" in page and "aorta: module 1" in page and "needs a second scan" in page
    assert "7 aneurysms / 13 strips" in page
    assert "278.2 kPa at 140" in md and "FAILED" in md  # the gate failure is on the record
    # the whole first sentence of the failure (not cut at the point in 1.30)
    assert "FAILED → quality gate FAILED: the wall is 1.30 mm thick along the surface normal" in md
    assert "1.5 mm thick along the surface normal — gate PASS" in md
    assert "investigational index, not a diagnosis" in md
    sources = [c["payload"] for c in run.calls if c["ok"]]
    results_section = md.split("## Results")[1].split("## Pipeline")[0]
    assert check_numbers(results_section, sources) == []


def test_report_shows_a_scan_path_by_its_file_name_only():
    calls = [{"tool": "segment_aorta", "input": {"ct_path": "/Users/someone/data/s1364/ct.nii.gz"},
              "ok": True, "seconds": 50.4, "steps": [], "payload": {}, "error": None}]
    md, page = render_report(request="r", ct_name="ct.nii.gz", credit="CC BY 4.0", results={},
                             calls=calls, summary_text=None, guard_note="g", model="m")
    assert "{'ct_path': 'ct.nii.gz'}" in md and "/Users/" not in md and "/Users/" not in page


def test_twin_card_marks_unfilled_slots():
    card = twin_card_html({})
    assert card.count('class="off"') == 7  # nothing run yet: every slot waits


# --- HTTP backend --------------------------------------------------------------


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802 - http.server API
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.path == "/step/crop":
            out, code = {"step": "crop", "summary": {"echo": body}, "text": "cropped"}, 200
        else:
            out, code = {"detail": "crop: no neck above the sac"}, 400
        data = json.dumps(out).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


def test_http_backend_posts_steps_and_reports_400s():
    srv = HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        b = HttpBackend(f"http://127.0.0.1:{srv.server_port}")
        ok = b.run("crop", {"parallel_caps": 1})
        assert ok.ok and ok.summary == {"echo": {"parallel_caps": 1}} and ok.text == "cropped"
        bad = b.run("volmesh", {"sweep": "auto"})
        assert not bad.ok and "400" in bad.error and "no neck" in bad.error
    finally:
        srv.shutdown()


def test_http_backend_unreachable_is_loud():
    with pytest.raises(RuntimeError, match="not reachable"):
        HttpBackend("http://127.0.0.1:9", timeout=2).run("crop", {})

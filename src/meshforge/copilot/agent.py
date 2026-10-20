"""The copilot loop: Claude plans the pipeline, calls the tools, recovers from
a failed gate, and writes a short summary whose numbers must come from the
tools. A manual tool loop on the Messages API; the history is append-only
(thinking blocks pass back unchanged)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable

from meshforge.copilot.guard import check_numbers
from meshforge.copilot.tools import TOOLS, PipelineTools

MODEL = "claude-opus-5-5"
EFFORT = "high"
#: server-side refusal fallback: on a policy decline the API re-runs the
#: request on a fallback model inside the same call (opt-in, on by default here)
FALLBACK_BETA = "server-side-fallback-2026-07-01"
MAX_TOKENS = 16000

SYSTEM = """You run an investigational biomechanics pipeline on a public CT scan of an \
abdominal aortic aneurysm, for clinicians and investors watching a demo.

Use the tools in this order: segment_aorta, crop_aneurysm, mesh_wall, wall_stress, \
strength_index. Start with the standard settings (parallel_caps false, sweep "auto") and \
change a setting only when a step's result tells you to. A wall that fails its quality \
gate gives no reportable stress: read the error, fix the cause it names, and run the \
affected steps again. Use the blood pressures the user gives; otherwise 140 mmHg \
systolic with a 120 mmHg what-if.

When the pipeline is done, write a summary of four to six plain sentences. Quote only \
numbers that appear in the tool results, in the same units. Say how much open strength \
data the band rests on, and call the index an investigational index, not a diagnosis. \
If a quality gate failed and you recovered, say so in one sentence. Do not estimate a \
rupture probability or a time to rupture."""


@dataclass
class CopilotRun:
    request: str
    model: str
    stop: str  # done | refusal | max_tokens | max_turns
    summary_text: str | None
    guard_note: str
    calls: list[dict[str, Any]]
    results: dict[str, dict[str, Any]]
    transcript: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)

    @property
    def complete(self) -> bool:
        return self.stop == "done" and "strength_index" in self.results


def _client():
    try:
        import anthropic
    except ImportError as exc:
        raise RuntimeError(
            "the copilot needs the Anthropic SDK — install it with "
            "`conda install -n meshforge -c conda-forge anthropic` "
            "(or `pip install 'meshforge[copilot]'`)"
        ) from exc
    return anthropic.Anthropic()


def _dump(block) -> dict[str, Any]:
    if hasattr(block, "to_dict"):
        return block.to_dict()
    if hasattr(block, "model_dump"):
        return block.model_dump()
    return dict(block) if isinstance(block, dict) else {"repr": repr(block)}


def _text(content) -> str:
    return "\n".join(
        getattr(b, "text", "") for b in content if getattr(b, "type", None) == "text"
    ).strip()


def run_copilot(
    request: str,
    *,
    tools: PipelineTools,
    client=None,
    model: str = MODEL,
    effort: str = EFFORT,
    max_turns: int = 24,
    log: Callable[[str], None] = print,
) -> CopilotRun:
    """Drive the pipeline from a plain-language ``request``."""
    client = client if client is not None else _client()
    messages: list[dict[str, Any]] = [{"role": "user", "content": request}]
    transcript: list[dict[str, Any]] = [{"role": "user", "content": request}]
    usage = {"input_tokens": 0, "output_tokens": 0}

    def ask():
        resp = client.beta.messages.create(
            model=model,
            max_tokens=MAX_TOKENS,
            system=SYSTEM,
            tools=TOOLS,
            messages=messages,
            thinking={"type": "adaptive"},
            output_config={"effort": effort},
            betas=[FALLBACK_BETA],
            fallbacks="default",
        )
        u = getattr(resp, "usage", None)
        for key in usage:
            usage[key] += int(getattr(u, key, 0) or 0)
        messages.append({"role": "assistant", "content": resp.content})
        transcript.append({"role": "assistant", "stop_reason": resp.stop_reason,
                           "model": getattr(resp, "model", model),
                           "content": [_dump(b) for b in resp.content]})
        return resp

    stop, final = "max_turns", ""
    for _ in range(max_turns):
        resp = ask()
        if resp.stop_reason == "refusal":
            stop = "refusal"
            break
        if resp.stop_reason == "max_tokens":
            stop = "max_tokens"
            break
        if resp.stop_reason == "pause_turn":
            continue
        uses = [b for b in resp.content if getattr(b, "type", None) == "tool_use"]
        if resp.stop_reason != "tool_use" or not uses:
            stop, final = "done", _text(resp.content)
            break
        results = []
        for b in uses:
            log(f"copilot     : {b.name} {json.dumps(b.input)}")
            r = tools.run(b.name, dict(b.input))
            log(f"              {'ok' if r.ok else 'FAILED'} ({r.seconds:.1f} s)"
                + ("" if r.ok else f" — {r.error}"))
            block = {"type": "tool_result", "tool_use_id": b.id, "content": r.content()}
            if not r.ok:
                block["is_error"] = True
            results.append(block)
        messages.append({"role": "user", "content": results})
        transcript.append({"role": "user", "tool_results": results})

    # a failed gate's payload is a tool output too: its measured numbers may be quoted
    sources = [c["payload"] for c in tools.calls if c["payload"]]
    summary, note = None, "no summary: the run did not finish"
    if stop == "done":
        bad = check_numbers(final, sources, allowed_text=request)
        if not bad:
            summary, note = final, "numbers guard: every number traced to a tool output"
        else:
            log(f"guard       : numbers not from any tool: {', '.join(bad)} — asking once")
            fix = ("Your summary quotes numbers that no tool returned: " + ", ".join(bad)
                   + ". Rewrite it using only numbers that appear in the tool results, in "
                     "the same units, or leave those numbers out. Do not call tools.")
            messages.append({"role": "user", "content": fix})
            transcript.append({"role": "user", "content": fix})
            resp = ask()
            retry = _text(resp.content)
            bad2 = check_numbers(retry, sources, allowed_text=request)
            if resp.stop_reason == "end_turn" and retry and not bad2:
                summary, note = retry, "numbers guard: passed after one correction"
            else:
                note = ("summary withheld: numbers not traceable to a tool ("
                        + ", ".join(bad2 or bad) + "); the report below is tool output only")
    elif stop == "refusal":
        note = "the model declined; the report below is tool output only"
    return CopilotRun(request=request, model=model, stop=stop, summary_text=summary,
                      guard_note=note, calls=tools.calls, results=tools.results,
                      transcript=transcript, usage=usage)

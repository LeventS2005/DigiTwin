"""Static guards for the Workbench frontend fixes 5-7 (session 41).

Fixes 5 (busy state), 6 (Browse pickers) and 7 (collapsible panels + inline
errors) live entirely in ``web/static``, so the session-41 walkthrough was
their only evidence — a live browser run with pasted pixel measurements, and
nothing standing between a future edit and a silent regression.

These tests read the shipped assets as text and assert the *contracts* the
walkthrough confirmed: no browser, no JS engine, no DOM. That buys a drift
guard, not a rendering proof; the layout numbers themselves (left pane
1661px, error 0px under its button) stay a walkthrough measurement.

The most valuable guard here is ``test_every_path_knob_has_a_declared_browse
_kind``: it walks STEP_KNOBS, so adding a path-valued knob in Python fails
this file until someone decides whether it gets a picker.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from digitwin.pipeline import STEP_KNOBS, STEP_ORDER


def _static_dir() -> Path:
    import digitwin.web as web_pkg

    return Path(web_pkg.__file__).parent / "static"


@pytest.fixture(scope="module")
def app_js() -> str:
    return (_static_dir() / "app.js").read_text()


@pytest.fixture(scope="module")
def index_html() -> str:
    return (_static_dir() / "index.html").read_text()


@pytest.fixture(scope="module")
def styles_css() -> str:
    return (_static_dir() / "styles.css").read_text()


# ---- fix 5: no double-submit, and the busy state always unwinds ---------
# Every button that fires a request must go through withBusy(), which
# disables + relabels it for the duration and restores in `finally`.

#: Buttons deliberately NOT wrapped: they fire no long request.
_UNBUSIED_BUTTONS = {
    "#btn-reset",  # /session/reset is a dict clear — returns before a relabel would paint
    "#vp-play",    # client-side frame timer, no request at all
    "#fp-pick",    # arms/cancels a viewport pick and relabels itself; no request on click
    "#fp-swap",    # swaps two client-side ends; the Q/Re preview it triggers is a read-only POST
    # D115: opens the file picker only; the upload runs on the file input's
    # change with its own busy state (test_lab_upload_has_its_own_busy_state)
    "#btn-upload-scan",
}


def test_with_busy_disables_relabels_and_restores(app_js: str) -> None:
    body = _function_body(app_js, "async function withBusy")
    assert "btn.disabled = true" in body
    assert 'btn.classList.add("busy")' in body
    assert "btn.textContent = label" in body
    # The restore must be in `finally` — an exception mid-request must not
    # strand the button disabled and reading "Running…" forever.
    finally_block = body.split("finally", 1)[1]
    assert "btn.textContent = prevText" in finally_block
    assert 'btn.classList.remove("busy")' in finally_block
    assert "btn.disabled =" in finally_block


def test_busy_restore_is_state_derived_never_a_blind_false(app_js: str) -> None:
    # #btn-save tracks journal_len via refreshState(); restoring it to a flat
    # `false` would re-enable Save on an empty session. withBusy takes a
    # callback for exactly that, and the Save wiring must pass one.
    body = _function_body(app_js, "async function withBusy")
    assert 'typeof restoreDisabled === "function" ? restoreDisabled()' in body
    save_wiring = _slice_between(app_js, '$("#btn-save").addEventListener', '$("#btn-wload")')
    assert "lastJournalLen" in save_wiring, "Save's busy restore dropped its state callback"


@pytest.mark.parametrize("button", ["#btn-synth", "#btn-load", "#btn-save", "#btn-wload"])
def test_every_request_button_is_wrapped(app_js: str, button: str) -> None:
    wiring = _slice_between(app_js, f'$("{button}").addEventListener', "});\n  $(")
    assert "withBusy(" in wiring, f"{button} can be double-clicked into concurrent POSTs"


def test_step_run_buttons_are_wrapped(app_js: str) -> None:
    # The per-step Run button is built once in buildStepPanel and cloned for
    # all ~15 panels, so one assertion covers every generated panel.
    panel = _function_body(app_js, "function buildStepPanel")
    assert 'withBusy(btn, "Running…"' in panel


def test_no_unwrapped_button_slipped_in(app_js: str) -> None:
    # $("#x") and wrap.querySelector("#x") — the cfd flow picker wires its
    # buttons through the latter, which a $(...)-only scan would never see.
    wired = set(re.findall(
        r'(?:\$|\.querySelector)\("(#[\w-]+)"\)\.addEventListener\("click"', app_js
    ))
    unwrapped = {b for b in wired if b not in _UNBUSIED_BUTTONS}
    for button in sorted(unwrapped):
        wiring = _slice_between(app_js, f'$("{button}").addEventListener', "});\n  $(")
        assert "withBusy(" in wiring, (
            f"{button} fires on click without a busy state — wrap it in withBusy() "
            f"or add it to _UNBUSIED_BUTTONS with a reason"
        )


def test_busy_class_is_actually_styled(styles_css: str) -> None:
    # withBusy's class is cosmetic-only if nothing paints it.
    assert "button.run.busy" in styles_css


# ---- fix 6: a Browse… picker on every file field ------------------------
# STEP_KNOBS carries no "this is a path" flag, so the mapping is explicit
# here. A path-valued knob added in Python fails this test until it is
# listed — either with a dialog kind, or as a deliberate omission.

#: "<step>.<knob>" -> the browse kind app.js must offer, or None for a
#: documented omission. Kinds: "file" (open), "dir", "save".
_PATH_KNOBS: dict[str, str | None] = {
    # structural: every import_* step has a path, handled by a rule not a table
    "import_cad.path": "file",
    "import_cad3d.path": "file",
    "import_image.path": "file",  # + a second "dir" button, DICOM series
    "import_cso.path": "file",
    # explicit BROWSE_KNOBS entries
    "wall.outer_surface": "file",
    "recipe.path": "file",
    "screenshot.path": "save",
    "solve.ccx": "file",
    "cfd.case": "dir",
    # D83: the CT for the AI-segmentation step (a file, like every importer)
    "segment.path": "file",
    # D85: the contrast-lumen surface segment writes, and the lumen the crop
    # step cuts with the same planes (read) and writes (save)
    "segment.lumen_out": "save",
    "crop.lumen": "file",
    "crop.lumen_out": "save",
    "segment.mask_out": "save",
    # D86: the sac step reads a CT and a lumen mask — both files, both pickers
    "sac.path": "file",
    "sac.lumen_mask": "file",
    # D88: the AAA wall-stress step reads a crop report (neck diameter) and,
    # like solve, may name the CalculiX executable
    "aaa_stress.crop_report": "file",
    # D91: the lumen surface that bounds the thrombus solid
    "aaa_stress.lumen": "file",
    "aaa_stress.sac": "file",
    "aaa_stress.ccx": "file",
    # D89: the tissue index reads PWS from a wall-stress summary JSON — a real
    # input file, so it gets a picker
    "tissue_index.from_summary": "file",
    # Deliberate omissions — session 41 scoped fix 6 to the fields the
    # walkthrough exercised. These three are real path fields with no picker;
    # promoting them is a one-line BROWSE_KNOBS addition each.
    "replay.workfile": None,
    "solve.deck": None,
    "solve.temperature_from": None,
}

#: Knobs whose help text mentions a path/file word but which hold no path.
_NOT_ACTUALLY_PATHS = {"solve.E"}  # "static/frequency decks"


def _looks_like_a_path_knob(step: str, knob: str, help_text: str) -> bool:
    if f"{step}.{knob}" in _NOT_ACTUALLY_PATHS:
        return False
    # Name first — `workfile` and `deck` name a path without the help text
    # ever saying so — then the help, which catches `outer_surface`.
    if re.search(r"path|file|deck|case|dir|_from$", knob, re.I):
        return True
    return bool(
        re.search(r"\bpath\b|\bfile\b|director|folder|\.inp\b|\.yaml\b|\.frd\b", help_text, re.I)
    )


def test_every_path_knob_has_a_declared_browse_kind() -> None:
    found = {
        f"{step}.{knob}"
        for step in STEP_ORDER
        for knob, (_type, _default, help_text) in STEP_KNOBS[step].items()
        if _looks_like_a_path_knob(step, knob, help_text)
    }
    undeclared = found - set(_PATH_KNOBS)
    assert not undeclared, (
        f"new path-valued knob(s) {sorted(undeclared)} — decide whether each gets a "
        f"Browse… picker (add to BROWSE_KNOBS in app.js) and list it in _PATH_KNOBS"
    )
    stale = set(_PATH_KNOBS) - found
    assert not stale, f"_PATH_KNOBS names knob(s) that no longer exist: {sorted(stale)}"


def test_browse_knobs_table_matches_the_declared_kinds(app_js: str) -> None:
    table = _slice_between(app_js, "const BROWSE_KNOBS = {", "};")
    declared = dict(re.findall(r'"([\w.]+)":\s*"(\w+)"', table))
    expected = {
        key: kind
        for key, kind in _PATH_KNOBS.items()
        if kind is not None and not key.startswith("import_")
    }
    assert declared == expected


def test_import_path_fields_get_their_picker_structurally(app_js: str) -> None:
    # Not table entries: buildStepPanel keys off the step name, so a fifth
    # importer inherits the picker with no frontend edit at all.
    panel = _function_body(app_js, "function buildStepPanel")
    assert 'step.startsWith("import_") && knob === "path"' in panel
    assert 'addBrowseButton(label, input, "file")' in panel
    # DICOM arrives as a directory of slices, so import_image gets both.
    assert 'if (step === "import_image") addBrowseButton(label, input, "dir")' in panel


def test_top_level_file_fields_get_pickers_too(app_js: str) -> None:
    # The three fields outside STEP_KNOBS: load a surface, save/load a work file.
    for field, kind in (("#load-path", "file"), ("#save-path", "save"), ("#wload-path", "file")):
        assert re.search(
            rf'addBrowseButton\(\$\("{field}"\)\.parentElement,\s*\$\("{field}"\),\s*"{kind}"\)',
            app_js,
        ), f"{field} lost its Browse… button"


def test_save_kind_degrades_on_an_older_shell(app_js: str) -> None:
    # save_file is a session-41 slot. An older digitwin-desktop has open_file
    # only, and must fall back rather than throw on an undefined callable.
    body = _function_body(app_js, "function browseInto")
    assert "bridge.save_file" in body
    assert "slot = bridge.open_file" in body.split("save_file", 1)[1], (
        "the save-dialog path has no fallback for a shell without the slot"
    )


def test_browser_mode_explains_itself_once(app_js: str, styles_css: str) -> None:
    # The original defect: in a plain tab the buttons vanished with no reason
    # given. One note, at the top, not one per hidden button.
    body = _function_body(app_js, "function addBrowserModeNote")
    assert "if (bridgeAvailable()) return;" in body
    assert 'note.className = "browse-note"' in body
    assert "digitwin-desktop" in body, "the note must name the way to get native dialogs"
    assert body.count("createElement") == 1, "one note, not one per field"
    assert ".browse-note" in styles_css


def test_the_desktop_shell_exposes_all_three_dialog_slots() -> None:
    shell = (Path(__file__).resolve().parents[1] / "src/digitwin/gui/shell.py").read_text()
    for slot in ("open_file", "open_directory", "save_file"):
        assert f"def {slot}(" in shell, f"_FileBridge lost the {slot} slot"


# ---- fix 7: collapsible panels, and errors where the click was ---------


def test_step_panels_are_collapsible_details(app_js: str) -> None:
    panel = _function_body(app_js, "function buildStepPanel")
    assert 'createElement("details")' in panel
    assert 'createElement("summary")' in panel
    # Collapsed by default is the whole point — the pane was 7924px tall.
    assert 'box.open = loadPanelOpenPref(step, step.startsWith("import_"))' in panel


def test_source_panel_starts_open(index_html: str, app_js: str) -> None:
    assert re.search(r'<details id="source-box" class="step-box" open>', index_html)
    assert 'sourceBox.open = loadPanelOpenPref("source", true)' in app_js


def test_collapse_state_survives_a_reload_and_a_private_window(app_js: str) -> None:
    for fn in ("function loadPanelOpenPref", "function savePanelOpenPref"):
        body = _function_body(app_js, fn)
        assert "localStorage" in body
        # Safari private mode throws on localStorage access; an unguarded
        # read would take the whole panel build down with it.
        assert "try {" in body and "catch" in body, f"{fn} accesses localStorage unguarded"
    assert 'box.addEventListener("toggle"' in app_js


def test_errors_echo_inline_under_their_own_panel(app_js: str, styles_css: str) -> None:
    body = _function_body(app_js, "function panelErrorFor")
    assert 'querySelector(":scope > .panel-error")' in body
    assert "box.appendChild(el)" in body, "the error must land inside the panel, not in #log only"
    assert ".panel-error" in styles_css
    # …and clear on that panel's next success, so a fixed error can't linger.
    assert "clearErrorEl(panelErrorFor(box))" in app_js


def test_source_and_workfile_errors_have_slots_too(index_html: str, app_js: str) -> None:
    for slot in ("save-error", "wload-error"):
        assert f'id="{slot}" class="panel-error" hidden' in index_html
        assert f'setError($("#{slot}"), err)' in app_js


def test_summary_chevron_replaced_the_floated_legend(styles_css: str) -> None:
    assert ".step-box > summary" in styles_css
    assert '.step-box[open] > summary::before { content: "▾"; }' in styles_css
    assert "legend" not in styles_css, "the pre-fix floated legend rule is dead — drop it"


# ---- fix 2 (frontend half): panels laid out in the served order ---------


def test_panels_follow_catalog_step_order_and_complain_when_it_is_short(app_js: str) -> None:
    body = _function_body(app_js, "async function init")
    assert "catalog.step_order" in body
    assert "Array.isArray(catalog.step_order)" in body, "a missing/short step_order must not blank the UI"
    # Rule 6: a step /schema has but step_order forgot is still rendered —
    # loudly, at the end — never silently dropped.
    assert "WARNING" in body and "missing from /catalog step_order" in body


# ---- helpers ------------------------------------------------------------


def _function_body(source: str, signature: str) -> str:
    """Text from ``signature`` to the next top-level ``\\n}``.

    Crude on purpose: these assets are hand-written and flat, and a real JS
    parser is not a dependency this suite should grow.
    """
    start = source.index(signature)
    end = source.index("\n}", start)
    return source[start:end]


def _slice_between(source: str, start_marker: str, end_marker: str) -> str:
    start = source.index(start_marker)
    try:
        end = source.index(end_marker, start + len(start_marker))
    except ValueError:
        end = len(source)
    return source[start:end]


# ---- M6: the twin card panel ---------------------------------------------


def test_twin_outline_is_the_reports_own_svg(index_html: str) -> None:
    # one body outline for the report header and the Workbench panel
    from digitwin.copilot.report import BODY_SVG

    assert BODY_SVG in index_html
    for cls in ("tw-body", "tw-next", "tw-aorta"):
        assert f'class="{cls}"' in BODY_SVG


def test_twin_panel_paints_server_data_as_text_only(app_js: str, index_html: str) -> None:
    for slot in ('id="twin"', 'id="twin-slots"', 'id="twin-modules"', 'id="twin-scan"'):
        assert slot in index_html, slot
    body = _function_body(app_js, "function renderTwin")
    # innerHTML only ever clears a list; every server string goes through textContent
    assert re.findall(r'innerHTML\s*=\s*([^;]+);', body) == ['""', '""']
    assert body.count(".textContent =") >= 5
    assert 'dataset.lit = twin.lit ? "1" : "0"' in body
    refresh = _function_body(app_js, "async function refreshTwin")
    assert 'apiGet("/twin")' in refresh and "renderTwin(twin)" in refresh


def test_twin_panel_refreshes_on_every_session_change(app_js: str) -> None:
    # a step, another client's change (the copilot over HTTP), a work-file
    # load, a reset, and the first paint all repaint the twin
    for fn in ("async function afterStep", "function startAutoRefresh",
               "async function loadWorkfile", "async function resetSession",
               "async function init()"):
        assert "await refreshTwin()" in _function_body(app_js, fn), fn


def test_twin_lit_state_is_styled(styles_css: str) -> None:
    assert '.twin[data-lit="1"] .tw-aorta path' in styles_css
    assert ".slot.filled .slot-dot" in styles_css and ".slot.next" in styles_css



def test_no_local_folder_reaches_the_screen(app_js: str) -> None:
    """M6: the Workbench is recorded. History, summary and log show an
    absolute path value by its file name and any /Users/<name> as "~";
    display only (the journal keeps the real paths)."""
    shown = _function_body(app_js, "function shownJson")
    assert "ABS_PATH.test(v) ? shortPath(v) : v" in shown and "shownText(" in shown
    assert "shownText(text)" in _function_body(app_js, "function log")
    assert "shownJson(result.summary, 2)" in _function_body(app_js, "function showSummary")
    history = _function_body(app_js, "async function refreshHistory")
    assert "shownJson(rec.params" in history and "shownJson(rec.summary" in history
    assert "JSON.stringify" not in history


# ---- Demo mode (D108): presentation layout, off by default ----------------
# The Demo switch in the top bar sets ``body.demo``; everything it changes is
# CSS under that class plus a few app.js functions that only act under it.
# These guards keep the contract: off = the Workbench unchanged; on = the AAA
# path's steps as numbered tabs, a reveal, the running step, the patient atlas.

_DEMO_STEP_RE = re.compile(r'\["(?P<step>[a-z_0-9]+)",\s*"(?P<title>[^"]+)",\s*"(?P<hint>[^"]+)"\]')


def _demo_steps(app_js: str) -> list[tuple[str, str, str]]:
    start = app_js.index("const DEMO_STEPS = [")
    end = app_js.index("];", start)
    return [(m.group("step"), m.group("title"), m.group("hint")) for m in _DEMO_STEP_RE.finditer(app_js[start:end])]


def test_demo_switch_is_in_the_top_bar_and_persists(index_html: str, app_js: str) -> None:
    assert 'id="demo-toggle"' in index_html
    assert index_html.index('id="demo-toggle"') < index_html.index('id="workbench"')
    assert 'localStorage.setItem("digitwin.demo"' in app_js
    assert 'searchParams.set("demo", "1")' in app_js
    assert 'classList.toggle("demo", on)' in app_js
    # the URL wins over the stored preference, so ?demo=1 opens a fresh server in Demo mode
    assert 'get("demo")' in app_js and 'getItem("digitwin.demo")' in app_js


def test_demo_tabs_are_the_aaa_path_in_run_order(app_js: str) -> None:
    """Four to seven tabs, real steps, in the order the copilot runs them —
    which is also their STEP_ORDER (DOM) order, so no reordering is needed."""
    tabs = _demo_steps(app_js)
    steps = [t[0] for t in tabs]
    assert steps == ["segment", "crop", "volmesh", "aaa_stress", "tissue_index", "cfd"]
    assert 4 <= len(steps) <= 7
    places = [STEP_ORDER.index(s) for s in steps]  # KeyError-free: every one is a step
    assert places == sorted(places), "the tabs must sit in STEP_ORDER, the panels' DOM order"
    titles = [t[1] for t in tabs]
    assert titles[0].startswith("Load"), "the first tab is the scan going in"
    assert any("FEA" in t for t in titles) and any("CFD" in t for t in titles)
    assert all(t[1] and t[2] for t in tabs), "every tab has a title and a hint"
    # the sub-steps without a tab of their own map onto one that exists
    aliases = re.search(r"const DEMO_STEP_OF = \{([^}]*)\};", app_js).group(1)
    for alias, target in re.findall(r'([a-z_]+):\s*"([a-z_]+)"', aliases):
        assert target in steps, (alias, target)
    assert "sac" in aliases and "flow_metrics" in aliases


def test_demo_layout_hides_everything_else_only_under_body_demo(styles_css: str, index_html: str) -> None:
    rules = {
        "body.demo #step-panels > details.step-box { display: none; }",
        'body.demo #step-panels > details.step-box[data-demo="1"] { display: block; }',
        'body.demo #step-panels > details.step-box[data-demo="1"] > summary .step-name { display: none; }',
        "body.demo #help { display: none; }",
        "body.demo #source-box { display: none; }",
        "body.demo #history-panel { display: none; }",
    }
    for rule in rules:
        assert rule in styles_css, rule
    assert 'id="history-panel"' in index_html
    # no rule hides a panel without the body.demo (or, D115, body.lab) guard
    for line in styles_css.splitlines():
        if "details.step-box" in line and "display: none" in line:
            assert line.startswith(("body.demo ", "body.lab ")), line
    # the tab itself exists only under body.demo; off, the step's own name shows
    assert ".demo-tab { display: none; }" in styles_css
    assert "body.demo .demo-tab {" in styles_css


def test_demo_tabs_are_numbered_closed_and_marked_from_the_journal(app_js: str, styles_css: str) -> None:
    assert 'box.dataset.demo = "1"' in app_js
    assert 'box.dataset.demoState = "waiting"' in app_js
    assert 'tab.className = "demo-tab"' in app_js and 'num.textContent = String(i + 1)' in app_js
    assert "decorateDemoPanels();" in app_js and "initDemoMode();" in app_js
    # demo on: the tabs close (the path at a glance) and are painted at once
    apply = app_js[app_js.index("function applyDemo(on)"):app_js.index("function decorateDemoPanels()")]
    assert "closeDemoPanels();" in apply and "paintDemoTabs();" in apply
    # done = in the journal (truncation upstream puts later tabs back to waiting)
    assert "noteDemoJournal(journal);" in app_js[app_js.index("async function refreshHistory()"):]
    paint = app_js[app_js.index("function paintDemoTabs()"):]
    paint = paint[: paint.index("\n}\n") + 3]
    assert '"running"' in paint and '"done"' in paint and '"waiting"' in paint
    assert "demoDoneSteps.has(step)" in paint
    # running = GET /state `running`, through the alias map, on change and on clear
    busy = app_js[app_js.index("function updateBusyLine(st)"):app_js.index("function initDemoMode()")]
    assert busy.count("paintDemoTabs();") >= 2
    for state in ("running", "done", "failed"):
        assert f'details.step-box[data-demo-state="{state}"]' in styles_css, state


def test_demo_mesh_wall_tab_shows_the_gate_failure_in_the_reports_own_words(app_js: str) -> None:
    """While the copilot's last mesh_wall call failed its gate, the journal
    still holds that wall's volmesh record; the tab must not read as a pass.
    The twin's wall slot (GET /twin, from copilot/report.py) says so in a
    fixed note; app.js matches that note's start, so pin the two together."""
    import inspect

    from digitwin.copilot import report

    assert 'const WALL_GATE_FAIL_NOTE = "last wall gate FAIL";' in app_js
    assert 'f"last wall gate FAIL: {' in inspect.getsource(report)
    assert "noteDemoTwin(twin);" in app_js[app_js.index("function renderTwin(twin)"):]
    note = app_js[app_js.index("function noteDemoTwin(twin)"):]
    note = note[: note.index("\n}\n") + 3]
    assert 's.key === "wall"' in note and "startsWith(WALL_GATE_FAIL_NOTE)" in note
    paint = app_js[app_js.index("function paintDemoTabs()"):]
    paint = paint[: paint.index("\n}\n") + 3]
    assert 'step === "volmesh" && demoWallFailed ? "failed"' in paint
    # running wins over failed (a re-mesh in progress), failed over done
    assert paint.index('"running"') < paint.index('"failed"') < paint.index('"done"')


def test_busy_line_acts_only_in_demo_mode(app_js: str, index_html: str) -> None:
    assert 'id="vp-busy"' in index_html
    busy = app_js[app_js.index("function updateBusyLine(st)"):]
    assert "!(isDemo() || isLab())" in busy[:200]  # Demo, and the Lab (D115)
    assert "updateBusyLine(st);" in app_js                  # every /state poll
    assert "st.running" in app_js and "running_for" in app_js


def test_step_reveal_draws_a_new_artifact_in_from_the_top_in_demo_mode(
    app_js: str, index_html: str, styles_css: str
) -> None:
    """D112: the D108 curtain is gone; a NEW artifact is drawn in cell by cell
    from the top down (the polygons sorted by their highest z, the
    connectivity grown as a prefix) over 2.4 s, the previous artifact a faint
    shell meanwhile. Demo mode only, never under reduced motion; the same
    artifact served again (a journal-only step) is not replayed; a field or
    frame swap, a new load or a clear cancels a running sweep; the whole
    state stands at the end."""
    vp_js = (_static_dir() / "viewport.js").read_text()
    assert "vp-reveal" not in index_html and ".vp-reveal" not in styles_css and "playReveal" not in app_js
    assert "var SWEEP_MS = 2400;" in vp_js
    assert "function planSweep(state)" in vp_js and "function sweepState(plan, p)" in vp_js
    on = vp_js[vp_js.index("function sweepOn()"):vp_js.index("var TYPED")]
    assert "demoOn() && !reducedMotion()" in on
    plan = vp_js[vp_js.index("function planSweep(state)"):vp_js.index("function sweepState")]
    assert "pts[3 * conn[i + j] + 2]" in plan                 # the cell's highest z
    assert "cells.sort(function (a, b) { return b[2] - a[2]; });" in plan  # top first
    state = vp_js[vp_js.index("function sweepState(plan, p)"):vp_js.index("function sweepStop")]
    assert "plan.ordered.subarray(0, plan.offsets[k])" in state  # a prefix: nothing invented
    assert "Object.assign({}, plan.state, { mesh: mesh })" in state  # points, field, range untouched
    stop = vp_js[vp_js.index("function sweepStop(finish)"):vp_js.index("function sweepStep")]
    assert "items[i].set(items[i].plan.state)" in stop        # the whole state at the end
    assert "function load(payload, resetCamera, sweep)" in vp_js
    load = vp_js[vp_js.index("function load(payload, resetCamera, sweep)"):vp_js.index("function clear()")]
    assert load.index("sweepStop(false);") < load.index("S.mesh.value = payload.state;")
    assert "var animate = !!sweep && sweepOn();" in load
    assert "{ opacity: 0 }" in load and load.index("{ opacity: 0 }") < load.index("v.resetCamera();")
    assert load.index("v.resetCamera();") < load.index("begin();")  # framed on the whole mesh first
    clear = vp_js[vp_js.index("function clear()"):vp_js.index("function setOverlay")]
    assert "sweepStop(false);" in clear
    assert '<vtk-geometry-representation v-if="base" :property="baseProperty">' in vp_js
    # app.js passes the sweep only for a NEW artifact framed anew
    assert "window.DigiTwinViewport.load(payload, resetCamera, resetCamera && changed);" in app_js
    assert "function sameArtifact(a, b)" in app_js
    same = app_js[app_js.index("function sameArtifact(a, b)"):]
    assert "a.label === b.label && a.kind === b.kind" in same[:600]


def test_layer_switch_shows_stress_or_flow_in_demo_mode_only(app_js: str, index_html: str, styles_css: str) -> None:
    """D112: two buttons over the canvas switch the view between the held
    wall-stress and CFD results (GET /geometry?artifact=stress|cfd, nothing
    computed), Demo mode only, once the session holds both; the chosen layer
    is drawn in from the top down with the camera kept."""
    assert 'id="vp-layer-ctl"' in index_html and 'data-layer="stress"' in index_html and 'data-layer="cfd"' in index_html
    assert "body:not(.demo):not(.lab) #vp-layer-ctl { display: none; }" in styles_css  # Demo or Lab (D115)
    assert "function showLayer(layer)" in app_js and "function paintLayerButtons()" in app_js
    show = app_js[app_js.index("async function showLayer(layer)"):app_js.index("function paintLayerButtons()")]
    assert "`/geometry?artifact=${encodeURIComponent(layer)}`" in show
    assert "window.DigiTwinViewport.load(payload, false, true);" in show  # camera kept, drawn in
    paint = app_js[app_js.index("function paintLayerButtons()"):app_js.index("// ---- flow section") if "// ---- flow section" in app_js else None]
    assert "ctl.hidden = !((isDemo() || isLab()) && held.stress && held.cfd);" in paint  # Demo or Lab (D115)
    assert "vpHeld = st.artifacts || {};" in app_js            # from every /state refresh
    assert "initLayerSwitch();" in app_js
    # the flow is seen inside the wall: the stress result stays as a translucent shell
    assert 'const wall = await fetch("/geometry?artifact=stress");' in show
    assert "window.DigiTwinViewport.setShell(shell);" in show
    vp_js = (_static_dir() / "viewport.js").read_text()
    assert "function setShell(state)" in vp_js and "setShell: setShell," in vp_js
    assert '<vtk-geometry-representation v-if="shell" :property="shellProperty">' in vp_js
    assert "var SHELL_PROPERTY = { color: [0.74, 0.76, 0.8], opacity: 0.22, specular: 0.0 };" in vp_js
    load = vp_js[vp_js.index("function load(payload, resetCamera, sweep)"):vp_js.index("// The layer switch's shell")]
    assert "var base = S.shell && S.shell.value ? null : previous;" in load  # no second ghost of the wall
    refresh = app_js[app_js.index("async function refreshViewport(resetCamera)"):app_js.index("function showPayload")]
    assert "window.DigiTwinViewport.setShell(null);" in refresh          # the current artifact again: no shell


def test_atlas_panel_is_a_template_with_fifteen_regions_six_chips_and_real_rows_only(
    index_html: str, app_js: str, styles_css: str
) -> None:
    """D108 addendum 6: the patient atlas replaces the coverage illustration.
    The body map is exactly the 15 regions of digitwin.atlas.REGIONS (same
    ids, same names, in order), each focusable and labelled; six modality
    chips with All on; every study row comes from GET /atlas (nothing about
    the demo scan is written into the page); the empty state names what
    WOULD list and invents nothing; hover / focus / selected rules exist only
    under body.demo."""
    from digitwin import atlas as atlas_mod

    assert '<template id="atlas-svg">' in index_html
    t0 = index_html.index('<template id="atlas-svg">')
    tpl = index_html[t0:index_html.index("</template>", t0)]
    regions = re.findall(
        r'<path class="atlas-region" data-region="([a-z_]+)" tabindex="0" aria-label="([^"]+)" role="button"'
        r' data-cx="\d+" data-cy="\d+" d="M[^"]+Z"/>',
        tpl,
    )
    assert len(regions) == 15 and tpl.count('class="atlas-region"') == 15
    assert [rid for rid, _ in regions] == [rid for rid, _ in atlas_mod.REGIONS]
    assert dict(regions) == dict(atlas_mod.REGIONS)  # the aria-labels are the catalogue's names
    assert len(atlas_mod.REGIONS) == 15 and len({rid for rid, _ in atlas_mod.REGIONS}) == 15
    # sides are the patient's: every right-side region is drawn left of the figure's
    # centre line (the viewBox's half width), every left-side one right of it
    vb = re.search(r'class="atlas-svg" viewBox="0 0 (\d+) (\d+)"', tpl)
    mid = int(vb.group(1)) / 2
    for rid, cx in re.findall(r'data-region="([a-z_]+)"[^>]*data-cx="(\d+)"', tpl):
        if rid.endswith("_r"):
            assert int(cx) < mid, rid
        elif rid.endswith("_l"):
            assert int(cx) > mid, rid
        else:
            assert int(cx) == mid, rid  # head, chest, torso sit on the centre line
    # every region has its own closed path; the figure is drawn at 1 unit = 1 px
    assert ".atlas .atlas-svg { display: block; width: 170px; height: 330px;" in styles_css
    assert vb.group(1) == "170" and vb.group(2) == "330"
    assert "the patient's right is on the left" in tpl
    chips = re.findall(r'<button type="button" class="atlas-chip( on)?" data-modality="([^"]+)">([^<]+)</button>', tpl)
    assert [m for _, m, _ in chips] == ["All", "CT", "MRI", "X-ray", "US", "Angio"]
    assert [m for _, m, lab in chips if lab != m] == [] and [m for on, m, _ in chips if on] == ["All"]
    assert [m for _, m, _ in chips][1:] == list(atlas_mod.MODALITIES)
    assert 'id="atlas-hover"' in tpl and 'id="atlas-studies"' in tpl and 'id="atlas-scope"' in tpl
    assert "atlas · regions without studies are dim · sides are the patient's" in tpl
    # real rows only: the page and the script know no study, no scan, no patient
    atlas_js = app_js[app_js.index("// ---- patient atlas"):app_js.index("// The result reveal lives in viewport.js")]
    for banned in ("abdomen + pelvis", "s1364", "1.5 mm", "prior", "TotalSegmentator", "date withheld"):
        assert banned not in tpl, banned
        assert banned not in atlas_js, banned
    assert 'apiGet("/atlas")' in app_js
    assert 'loaded.textContent = "loaded ✓";' in app_js and "if (s.loaded) {" in app_js
    assert "would list here" in app_js  # the empty state
    assert "regions without studies are dim · sides are the patient's" in app_js
    # cloned above the twin only while Demo is on, removed when off
    apply = app_js[app_js.index("function applyDemo(on)"):app_js.index("function decorateDemoPanels()")]
    assert "buildAtlasPanel();" in apply and "removeAtlasPanel();" in apply
    assert "twin.parentElement.insertBefore(node, twin)" in app_js
    # refreshed with the twin on every session change; a no-op while Demo is off
    assert app_js.count("await refreshAtlas();") == app_js.count("await refreshTwin();") >= 4
    refresh = app_js[app_js.index("async function refreshAtlas()"):]
    assert 'if (!$("#atlas-panel")) return;' in refresh[:120]
    # states: has / none by data attribute; hover, focus and selected under body.demo only
    assert '.atlas .atlas-region[data-has="1"]' in styles_css
    assert "body.demo .atlas .atlas-region:hover" in styles_css
    assert "body.demo .atlas .atlas-region:focus-visible" in styles_css
    assert "body.demo .atlas .atlas-region.selected { stroke: #ff5d55; stroke-width: 2px;" in styles_css
    for line in styles_css.splitlines():
        if ".atlas-region" in line and (":hover" in line or ".selected" in line or ":focus-visible" in line):
            assert line.startswith("body.demo "), line
    # keyboard: Enter / Space select, Esc clears; click again clears
    assert 'e.key === "Enter" || e.key === " "' in app_js
    assert 'e.key === "Escape" && atlasSelected !== null' in app_js
    assert "selectAtlasRegion(atlasSelected === id ? null : id)" in app_js


def test_demo_mode_claims_nothing(index_html: str, app_js: str) -> None:
    demo_text = index_html[index_html.index('id="demo-switch"'):]
    for banned in ("rupture probability", "time to rupture", "diagnos", "predict"):
        # "not a diagnosis" is allowed in the twin foot; the demo additions add no claim
        assert banned not in app_js[app_js.index("const DEMO_STEPS"):app_js.index("async function init()")]
    assert "regions without studies are dim" in demo_text  # the atlas: real studies only, nothing invented
    assert "no patient loaded" in demo_text


def test_viewport_draws_the_ghost_outline_behind_the_mesh(app_js: str) -> None:
    """GET /geometry's ``ghost`` list (the whole AI outline, and the patient's
    own heart from the same run) is drawn as faint translucent representations
    behind the mesh and the QC overlay, one per entry, each in the fixed look
    of its kind (the aorta grey, the heart a warmer tint at the same opacity —
    never coloured by data); load() takes the list from the main payload only,
    clear() drops it."""
    vp_js = (_static_dir() / "viewport.js").read_text()
    assert "var GHOST_PROPERTY = { color: [0.78, 0.8, 0.84], opacity: 0.16" in vp_js
    assert "var HEART_GHOST_PROPERTY = { color: [0.88, 0.74, 0.72], opacity: 0.16" in vp_js
    assert "var GHOST_PROPERTIES = { aorta: GHOST_PROPERTY, heart: HEART_GHOST_PROPERTY };" in vp_js
    assert '<vtk-geometry-representation v-for="g in ghosts" :key="g.kind" :property="g.property">' in vp_js
    assert '<vtk-mesh :state="g.state"></vtk-mesh>' in vp_js
    assert vp_js.index('v-for="g in ghosts"') > vp_js.index('v-if="overlay"')  # drawn after, behind by opacity
    assert 'if (S.ghosts && "ghost" in payload) S.ghosts.value = ghostList(payload.ghost);' in vp_js
    assert "property: GHOST_PROPERTIES[kind] || GHOST_PROPERTY" in vp_js
    assert "if (S.ready && S.ghosts) S.ghosts.value = [];" in vp_js  # clear()
    assert "setGhosts: setGhosts," in vp_js
    assert "color-map-preset" not in vp_js[vp_js.index('v-for="g in ghosts"'):vp_js.index("</vtk-view>")]



def test_viewport_turntable_spins_only_in_demo_mode_after_idle(app_js: str) -> None:
    """D111: in Demo mode a view left alone for 3 s spins slowly about the
    camera's up axis (one turn in 45 s) and stops on any input to the canvas
    (press, scroll, touch, key; hovering does not stop it). Pure camera motion
    through vue_vtk's getCamera/setCamera; never outside Demo mode, never
    while a pick is armed, never without a mesh, never under reduced motion,
    paused while the tab is hidden; the Demo switch re-arms or stops it."""
    vp_js = (_static_dir() / "viewport.js").read_text()
    assert "var IDLE_MS = 3000;" in vp_js
    assert "var SPIN_DEG_PER_S = 8;" in vp_js
    demo_on = vp_js[vp_js.index("function demoOn()"):vp_js.index("function reducedMotion()")]
    assert 'return c.contains("demo") || c.contains("lab");' in demo_on  # Demo or Lab (D115)
    assert '"(prefers-reduced-motion: reduce)"' in vp_js
    spin = vp_js[vp_js.index("function spinStart()"):vp_js.index("function spinStop()")]
    assert "!demoOn() || reducedMotion()" in spin
    assert "if (!(S.mesh && S.mesh.value)) return;" in spin
    assert "if (S.pickCb) return;" in spin
    step = vp_js[vp_js.index("function spinStep(ts)"):vp_js.index("function spinStart()")]
    assert "!hasMesh || !demoOn()" in step and "spinStop();" in step
    assert "Math.min(ts - R.last, 100)" in step  # a stalled tab does not jump
    assert "view.setCamera({ position: [fp[0] + rx, fp[1] + ry, fp[2] + rz], focalPoint: fp, viewUp: up });" in vp_js
    assert 'var evs = ["pointerdown", "wheel", "touchstart", "keydown"];' in vp_js
    assert "pointermove" not in vp_js[vp_js.index("function watchIdle"):vp_js.index("// ---- flow section")]
    assert 'document.addEventListener("visibilitychange"' in vp_js
    assert "watchIdle(host); // D111" in vp_js
    arm = vp_js[vp_js.index("function armPick(cb, onMiss)"):vp_js.index("function onPick(e)")]
    assert arm.count("spinStop();") == 1 and arm.count("idleDisarm();") == 1 and arm.count("idleArm();") == 1
    assert "v.resetCamera();\n          idleArm();" in vp_js  # a newly framed artifact starts the clock
    clear = vp_js[vp_js.index("function clear()"):vp_js.index("function setOverlay")]
    assert "spinStop();" in clear and "idleDisarm();" in clear
    assert "wake: wake," in vp_js
    assert "if (window.DigiTwinViewport && window.DigiTwinViewport.wake) window.DigiTwinViewport.wake();" in app_js
    assert app_js.index("DigiTwinViewport.wake()") > app_js.index("function applyDemo(on)")


# ---- D115: Lab mode — Demo's six tabs without the right-hand pane ----------
def test_lab_switch_sits_beside_demo_never_both_on_and_persists(index_html: str, app_js: str) -> None:
    switches = index_html[index_html.index('class="mode-switches"'):index_html.index('id="help"')]
    assert switches.index('id="demo-toggle"') < switches.index('id="lab-toggle"')
    assert '<span class="demo-switch-label">Lab</span>' in switches
    assert '<span class="demo-switch-label">Demo</span>' in switches  # Demo's own switch, unchanged
    assert 'localStorage.setItem("digitwin.lab"' in app_js
    assert 'searchParams.set("lab", "1")' in app_js
    assert 'classList.toggle("lab", on)' in app_js
    assert 'get("lab")' in app_js and 'getItem("digitwin.lab")' in app_js
    init = _function_body(app_js, "function initLabMode()")
    assert "if (lab.checked && isDemo()) applyDemo(false);" in init  # Lab on turns Demo off
    assert "if (demo.checked && isLab()) applyLab(false);" in init  # and the other way round
    assert 'q.get("demo") === null' in init  # ?demo=1 (the copilot's window) always wins
    boot = _function_body(app_js, "async function init()")
    assert boot.index("decorateDemoPanels();") < boot.index("initDemoMode();") < boot.index("initLabMode();")


def test_lab_layout_drops_the_right_pane_only_under_body_lab(styles_css: str, index_html: str) -> None:
    for rule in (
        "body.lab #pane-right { display: none; }",
        "body.lab #workbench { grid-template-columns: minmax(268px, 296px) 1fr; }",
        "body.lab #step-panels > details.step-box { display: none; }",
        'body.lab #step-panels > details.step-box[data-demo="1"] { display: block; }',
        'body.lab #step-panels > details.step-box[data-demo="1"] > summary .step-name { display: none; }',
        "body.lab #source-box { display: none; }",
        "body.lab #help { display: none; }",
        ".lab-upload { display: none; }",
        "body.lab .demo-tab {",
    ):
        assert rule in styles_css, rule
    # the right pane goes in the Lab only; the three columns stay everyone else's
    hides_right = [ln for ln in styles_css.splitlines() if "#pane-right" in ln and "display: none" in ln]
    assert hides_right == ["body.lab #pane-right { display: none; }"]
    assert "grid-template-columns: minmax(268px, 296px) 1fr minmax(300px, 392px);" in styles_css
    # Upload scan: inside the left pane, above the step panels, a CT file
    # picker; an arrow where a number would be, so Demo's tabs keep 1-6
    left = index_html[index_html.index('id="pane-left"'):index_html.index('id="pane-center"')]
    assert left.index('id="lab-upload"') < left.index('id="step-panels"')
    accept = re.search(r'id="upload-scan-input" accept="([^"]+)"', left).group(1).split(",")
    assert {".nii", ".nii.gz", ".nrrd"} <= set(accept)
    assert '<span class="demo-title">Upload scan</span>' in left
    assert '<span class="demo-num" aria-hidden="true">↑</span>' in left
    for state in ("running", "done"):
        assert f'#lab-upload[data-demo-state="{state}"]' in styles_css, state


def test_lab_tabs_are_demos_own_six_and_upload_feeds_load_scan(app_js: str, styles_css: str) -> None:
    """The founder's brief: all of Demo's steps, minus the right-hand pane,
    plus Upload scan. So the Lab has no step list of its own: it shows the
    very panels Demo tags (data-demo), in Demo's order and numbering, and the
    upload's copy goes into Load scan's (segment's) path knob. The heart is
    the ghost Load scan already draws beside the aorta, not a step."""
    steps = [t[0] for t in _demo_steps(app_js)]
    assert steps == ["segment", "crop", "volmesh", "aaa_stress", "tissue_index", "cfd"]
    for gone in ("LAB_STEPS", "decorateLabPanels", "closeLabPanels", "paintLabTabs", "data-lab", "segment_heart"):
        assert gone not in app_js, gone
    assert "data-lab" not in styles_css
    assert "segment_heart" not in STEP_ORDER
    apply = _function_body(app_js, "function applyLab(on)")
    assert "closeDemoPanels();" in apply and "paintDemoTabs();" in apply
    assert "paintLayerButtons();" in apply  # the stress / flow switch, as in Demo
    up = _function_body(app_js, "async function uploadScan(file)")
    assert 'details.step-box[data-step="segment"]' in up and "next: Load scan" in up
    paint = _function_body(app_js, "function paintLabUpload()")
    assert '"running"' in paint and '"done"' in paint and '"waiting"' in paint
    assert 'demoDoneSteps.has("segment")' in paint
    assert "paintLabUpload();" in _function_body(app_js, "function paintDemoTabs()")
    assert "body:not(.demo):not(.lab) #vp-layer-ctl { display: none; }" in styles_css


def test_lab_upload_has_its_own_busy_state(app_js: str, index_html: str) -> None:
    init = _function_body(app_js, "function initUploadScan()")
    assert 'btn.addEventListener("click", () => input.click());' in init
    assert "labUploading = true;" in init and "btn.disabled = true;" in init
    fin = init.split("finally", 1)[1]
    assert "labUploading = false;" in fin and "btn.disabled = false;" in fin
    assert 'input.value = "";' in init and "setError(errEl, err);" in init
    assert 'id="upload-error"' in index_html
    up = _function_body(app_js, "async function uploadScan(file)")
    assert "/upload/scan?name=${encodeURIComponent(file.name)}" in up
    assert '"Content-Type": "application/octet-stream"' in up and "body: file," in up
    assert "target.value = data.path;" in up and "box.open = true;" in up

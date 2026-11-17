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
    # change with its own busy state (test_upload_scan_has_its_own_busy_state)
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


# ---- always-on features: running line, layer switch, patient atlas, Upload scan
# The Demo and Lab modes are gone (body classes, switches, tabs, ?demo= / ?lab=,
# their storage keys). What they used to gate is part of the plain Workbench:
# these guards pin each feature as always on, and the last test pins the modes
# as absent.


def test_busy_line_is_always_on_and_follows_the_state_poll(app_js: str, index_html: str) -> None:
    assert 'id="vp-busy"' in index_html
    busy = _function_body(app_js, "function updateBusyLine(st)")
    # the only gate is "is a step running": no mode, no body class
    assert "if (!st || !st.running) {" in busy[:80]
    assert "hideBusyLine();" in busy[:140]
    assert "isDemo" not in busy and "isLab" not in busy and "classList" not in busy
    assert "paintDemoTabs" not in app_js
    assert "updateBusyLine(st);" in _function_body(app_js, "function startAutoRefresh")  # every /state poll
    assert "st.running_for" in busy and "st.running" in busy
    assert "STEP_VERBS[busyStep]" in _function_body(app_js, "function tickBusyLine()")


def test_layer_switch_shows_stress_or_flow_whenever_both_results_are_held(
    app_js: str, index_html: str, styles_css: str
) -> None:
    """D112: two buttons over the canvas switch the view between the held
    wall-stress and CFD results (GET /geometry?artifact=stress|cfd, nothing
    computed), whenever the session holds both (no mode); the chosen layer is
    drawn at once with the camera kept."""
    assert 'id="vp-layer-ctl"' in index_html and 'data-layer="stress"' in index_html and 'data-layer="cfd"' in index_html
    # no CSS gate: the markup's own `hidden`, set by the script, is the only one
    assert "#vp-layer-ctl { display: none; }" not in styles_css
    assert "body:not(" not in styles_css
    assert "function showLayer(layer)" in app_js and "function paintLayerButtons()" in app_js
    show = app_js[app_js.index("async function showLayer(layer)"):app_js.index("function paintLayerButtons()")]
    assert "`/geometry?artifact=${encodeURIComponent(layer)}`" in show
    assert "window.DigiTwinViewport.load(payload, false);" in show  # camera kept, drawn at once
    paint = _function_body(app_js, "function paintLayerButtons()")
    assert "ctl.hidden = !(held.stress && held.cfd);" in paint  # both results held, and nothing else
    assert "isDemo" not in paint and "isLab" not in paint
    assert "vpHeld = st.artifacts || {};" in app_js            # from every /state refresh
    assert "initLayerSwitch();" in app_js
    # the flow is seen inside the wall: the stress result stays as a translucent shell
    assert 'const wall = await fetch("/geometry?artifact=stress");' in show
    assert "window.DigiTwinViewport.setShell(shell);" in show
    vp_js = (_static_dir() / "viewport.js").read_text()
    assert "function setShell(state)" in vp_js and "setShell: setShell," in vp_js
    assert '<vtk-geometry-representation v-if="shell" :property="shellProperty">' in vp_js
    assert "var SHELL_PROPERTY = { color: [0.74, 0.76, 0.8], opacity: 0.22, specular: 0.0 };" in vp_js
    assert "function load(payload, resetCamera) {" in vp_js  # two arguments: no sweep
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
    WOULD list and invents nothing; it is built at startup, above the twin
    card, in every session (no mode)."""
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
    atlas_js = app_js[app_js.index("// ---- patient atlas"):app_js.index("// The step running on the server right now")]
    for banned in ("abdomen + pelvis", "s1364", "1.5 mm", "prior", "TotalSegmentator", "date withheld"):
        assert banned not in tpl, banned
        assert banned not in atlas_js, banned
    assert 'apiGet("/atlas")' in app_js
    assert 'loaded.textContent = "loaded ✓";' in app_js and "if (s.loaded) {" in app_js
    assert "would list here" in app_js  # the empty state
    assert "regions without studies are dim · sides are the patient's" in app_js
    # cloned above the twin card when the page starts, and never removed
    boot = _function_body(app_js, "async function init()")
    assert "buildAtlasPanel();" in boot
    assert boot.index("buildAtlasPanel();") < boot.index("await refreshTwin();")
    assert "removeAtlasPanel" not in app_js
    build = _function_body(app_js, "function buildAtlasPanel()")
    assert "twin.parentElement.insertBefore(node, twin)" in build
    assert 'const twin = $("#twin-panel");' in build
    assert index_html.index('id="pane-right"') < index_html.index('id="twin-panel"') < index_html.index('<template id="atlas-svg">')
    # refreshed with the twin on every session change
    assert app_js.count("await refreshAtlas();") == app_js.count("await refreshTwin();") >= 4
    refresh = app_js[app_js.index("async function refreshAtlas()"):]
    assert 'if (!$("#atlas-panel")) return;' in refresh[:120]
    # states: has / none by data attribute; hover, focus and selected need no body class
    assert '.atlas .atlas-region[data-has="1"]' in styles_css
    assert ".atlas .atlas-region:hover" in styles_css
    assert ".atlas .atlas-region:focus-visible" in styles_css
    assert ".atlas .atlas-region.selected { stroke: #ff5d55; stroke-width: 2px;" in styles_css
    for line in styles_css.splitlines():
        if ".atlas-region" in line and (":hover" in line or ".selected" in line or ":focus-visible" in line):
            assert line.startswith(".atlas "), line
    # keyboard: Enter / Space select, Esc clears; click again clears
    assert 'e.key === "Enter" || e.key === " "' in app_js
    assert 'e.key === "Escape" && atlasSelected !== null' in app_js
    assert "selectAtlasRegion(atlasSelected === id ? null : id)" in app_js


def test_atlas_and_running_line_claim_nothing(index_html: str, app_js: str) -> None:
    atlas_text = index_html[index_html.index('<template id="atlas-svg">'):]
    scripted = app_js[app_js.index("// ---- the running step's phrase"):app_js.index("async function init()")]
    for banned in ("rupture probability", "time to rupture", "diagnos", "predict"):
        # "not a diagnosis" is allowed in the twin foot; the atlas and the running line add no claim
        assert banned not in scripted, banned
        assert banned not in atlas_text.lower().replace("not a diagnosis", ""), banned
    assert "regions without studies are dim" in atlas_text  # the atlas: real studies only, nothing invented
    assert "no patient loaded" in atlas_text


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



# ---- D115: Upload scan, a button in the source box -------------------------
def test_upload_scan_sits_in_the_source_box_and_feeds_load_scan(
    app_js: str, index_html: str, styles_css: str
) -> None:
    """A CT from this computer: the button lives in #source-box beside the
    other source controls (a plain ``button.run``, no tab markup), its bytes go
    to POST /upload/scan, and the server's copy fills Load scan's (the segment
    step's) path knob. Always on: no mode shows or hides it."""
    box = index_html[index_html.index('id="source-box"'):index_html.index('id="step-panels"')]
    assert '<button type="button" id="btn-upload-scan" class="run"' in box
    assert "Upload scan</button>" in box
    assert 'id="upload-status"' in box and 'id="upload-error"' in box
    accept = re.search(r'id="upload-scan-input" accept="([^"]+)"', box).group(1).split(",")
    assert {".nii", ".nii.gz", ".nrrd"} <= set(accept)
    assert box.index('id="btn-load"') < box.index('id="btn-upload-scan"')  # after the surface rows
    # nothing hides it: no .lab-upload card, no tab markup, no state attribute
    assert "lab-upload" not in index_html and "lab-upload" not in styles_css
    assert "data-demo" not in index_html and "data-demo-state" not in styles_css
    assert ".upload-status {" in styles_css
    up = _function_body(app_js, "async function uploadScan(file)")
    assert 'details.step-box[data-step="segment"]' in up and "next: Load scan" in up
    assert 'input[data-knob="path"]' in up and "target.value = data.path;" in up
    boot = _function_body(app_js, "async function init()")
    assert "initUploadScan();" in boot and "paintUploadStatus();" in boot
    status = _function_body(app_js, "function paintUploadStatus()")
    assert "uploadedScan" in status and "UPLOAD_HINT" in status
    assert "demoDoneSteps" not in app_js and "labUpload" not in app_js


def test_upload_scan_has_its_own_busy_state(app_js: str, index_html: str) -> None:
    init = _function_body(app_js, "function initUploadScan()")
    assert 'btn.addEventListener("click", () => input.click());' in init
    assert "uploading = true;" in init and "btn.disabled = true;" in init
    fin = init.split("finally", 1)[1]
    assert "uploading = false;" in fin and "btn.disabled = false;" in fin
    assert 'input.value = "";' in init and "setError(errEl, err);" in init
    assert 'id="upload-error"' in index_html
    up = _function_body(app_js, "async function uploadScan(file)")
    assert "/upload/scan?name=${encodeURIComponent(file.name)}" in up
    assert '"Content-Type": "application/octet-stream"' in up and "body: file," in up
    assert "target.value = data.path;" in up and "box.open = true;" in up


# ---- the Demo and Lab modes are gone ----------------------------------------
def test_demo_and_lab_modes_are_gone_from_the_frontend(
    app_js: str, index_html: str, styles_css: str
) -> None:
    """No switch, no body class, no URL parameter, no stored preference, no tab
    decoration: the Workbench has one layout. (The ``digitwin.panel.*``
    collapse keys are a different thing and stay.)"""
    sources = {"app.js": app_js, "index.html": index_html, "styles.css": styles_css}
    for name, text in sources.items():
        for gone in (
            "isDemo", "isLab", "applyDemo", "applyLab", "readDemoPref", "readLabPref",
            "body.demo", "body.lab", "body:not(.demo)", "demo-tab", "demo-switch", "demo-toggle", "lab-toggle",
            "mode-switches", "data-demo", "demoDoneSteps", "paintDemoTabs", "decorateDemoPanels", "DEMO_STEPS",
            "digitwin.demo", "digitwin.lab", "?demo", "?lab",
        ):
            assert gone not in text, (name, gone)
    # no URL lookup of a mode parameter, by either quote style
    assert not re.search(r"""\.(?:get|set|has|delete)\(\s*["'](?:demo|lab)["']""", app_js)
    assert not re.search(r"""classList\.(?:toggle|add|remove|contains)\(\s*["'](?:demo|lab)["']""", app_js)
    # the collapse keys stay
    assert "localStorage.getItem(`digitwin.panel.${step}`)" in app_js
    # the stale viewport words are gone with the turntable and the reveal
    for gone in ("turntable", "reveal", "DigiTwinViewport.wake"):
        assert gone not in app_js.lower(), gone

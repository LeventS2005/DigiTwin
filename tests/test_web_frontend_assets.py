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

from meshforge.pipeline import STEP_KNOBS, STEP_ORDER


def _static_dir() -> Path:
    import meshforge.web as web_pkg

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
    # D86: the sac step reads a CT and a lumen mask — both files, both pickers
    "sac.path": "file",
    "sac.lumen_mask": "file",
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
    # save_file is a session-41 slot. An older meshforge-desktop has open_file
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
    assert "meshforge-desktop" in body, "the note must name the way to get native dialogs"
    assert body.count("createElement") == 1, "one note, not one per field"
    assert ".browse-note" in styles_css


def test_the_desktop_shell_exposes_all_three_dialog_slots() -> None:
    shell = (Path(__file__).resolve().parents[1] / "src/meshforge/gui/shell.py").read_text()
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

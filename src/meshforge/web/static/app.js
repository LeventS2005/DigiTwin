// meshforge Workbench frontend (V35 increment 2).
//
// Rule 4: NO meshing/geometry/solve logic here. This module only calls the
// increment-1 JSON API and renders its responses. Endpoints used:
//   GET  /schema  /catalog  /journal  /state  /twin  /cfd/presets
//   POST /source/synth  /source/load  /step/<name>
//   POST /workfile/save  /workfile/load  /session/reset
//   POST /cfd/pick  /cfd/inflow   (D80 flow-ends picker, read-only)
// Plain ES module, no build step.

const $ = (sel) => document.querySelector(sel);

// ---- tiny fetch helper -------------------------------------------------
async function apiGet(path) {
  const res = await fetch(path);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(detailOf(data, res));
  return data;
}
async function apiPost(path, body) {
  const res = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(detailOf(data, res));
  return data;
}
function detailOf(data, res) {
  // FastAPI: 400 -> {detail: "<message>"}; 422 -> {detail: [ {..} ]}.
  if (data && data.detail !== undefined) {
    return typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail);
  }
  return `HTTP ${res.status} ${res.statusText}`;
}

// ---- on-screen paths (M6) ---------------------------------------------
// The Workbench is recorded for the demo: a local folder (and the user name
// in it) must not reach the screen. Display only — the journal, work files
// and requests keep the real paths. An absolute path value is shown by its
// file name; any other text has its /Users/<name> (or /home/<name>) prefix
// shown as "~".
const ABS_PATH = /^(\/|~\/|[A-Za-z]:[\\/])/;
const HOME_PREFIX = /\/(?:Users|home)\/[^\/\s"']+/g;
function shownText(text) {
  return String(text).replace(HOME_PREFIX, "~");
}
function shownJson(obj, indent) {
  return shownText(JSON.stringify(obj, (k, v) =>
    typeof v === "string" && ABS_PATH.test(v) ? shortPath(v) : v, indent));
}

// ---- log + summary -----------------------------------------------------
function log(text, isErr) {
  const el = $("#log");
  const line = document.createElement("span");
  if (isErr) line.className = "err";
  line.textContent = (isErr ? "ERROR: " : "") + shownText(text).replace(/\s+$/, "") + "\n";
  el.appendChild(line);
  el.scrollTop = el.scrollHeight;
}
function showSummary(result) {
  if (result && result.summary !== undefined) {
    $("#summary-view").textContent = shownJson(result.summary, 2);
  }
}

// After every action: surface the result text, then refresh journal + state.
async function afterStep(result) {
  if (result && result.text) log(result.text);
  showSummary(result);
  await refreshHistory();
  await refreshState();
  await refreshTwin();
  viewportField = null; // a new artifact -> let the server pick the default field
  await refreshViewport(true);
  // Any new mesh or source invalidates a picked inflow end; a cfd run keeps it
  // (so the next run only needs a new speed).
  if (!result || result.step !== "cfd") resetFlowPicker();
  await refreshPresets();
}
function onError(err) {
  log(String(err && err.message ? err.message : err), true);
}

// ---- viewport (V35 increment 3, pure client-side vtk.js) ---------------
let viewportUp = false; // did the vtk.js canvas mount?
let viewportField = null; // current color-by field (null = server default)
let vpFrames = 0; // CFD frame count of the current artifact
let vpTimes = []; // per-frame times
let vpPlaying = null; // setInterval handle while animating
let vpOverlay = null; // cached QC-overlay state so the toggle needs no refetch

function initViewport() {
  viewportUp = !!(
    window.MeshforgeViewport &&
    window.MeshforgeViewport.available() &&
    window.MeshforgeViewport.mount("vtk-canvas")
  );
  if (!viewportUp) {
    $("#vp-fallback").hidden = false;
    return;
  }
  $("#vp-field").addEventListener("change", (e) => {
    viewportField = e.target.value || null;
    refreshViewport(false).catch(onError);
  });
  $("#vp-frame").addEventListener("input", (e) => {
    stopPlay();
    loadFrame(Number(e.target.value)).catch(onError);
  });
  $("#vp-play").addEventListener("click", togglePlay);
  $("#vp-overlay").addEventListener("change", (e) => {
    window.MeshforgeViewport.setOverlay(e.target.checked ? vpOverlay : null);
  });
  $("#vp-section").addEventListener("change", (e) => {
    window.MeshforgeViewport.setSectionVisible(e.target.checked);
    $("#vp-wall-ctl").hidden = !e.target.checked;
    applyColourRange();
  });
  $("#vp-wall").addEventListener("change", (e) => {
    window.MeshforgeViewport.setWallVisible(e.target.checked);
  });
  $("#vp-fit").addEventListener("change", () => applyColourRange());
}

// Fetch the current artifact's geometry and (re)paint the canvas. resetCamera
// frames a NEW artifact; a field swap keeps the camera put.
async function refreshViewport(resetCamera) {
  if (!viewportUp) return;
  const url = "/geometry" + (viewportField ? `?field=${encodeURIComponent(viewportField)}` : "");
  const res = await fetch(url);
  if (res.status === 204) return clearViewport(); // nothing rendered yet
  if (!res.ok) return; // viewport hiccup — don't spam the log
  const payload = await res.json();
  window.MeshforgeViewport.load(payload, resetCamera);
  if (resetCamera) playReveal(); // demo mode only (no-op otherwise)
  populateFields(payload.fields || [], payload.field);
  updateColorbar(payload);
  setupFrames(payload);
  setupOverlay(payload);
  setupSection(payload);
  vpPayload = payload;
  applyColourRange();
}

// Colour range: the full field range, or (fit colours) the flow inside the
// section — both computed by the server; the page only chooses which.
let vpPayload = null;
function applyColourRange() {
  if (!vpPayload) return;
  const fit = $("#vp-fit").checked && vpPayload.section_range && $("#vp-section").checked;
  const range = fit ? vpPayload.section_range : vpPayload.data_range;
  if (!range) return;
  window.MeshforgeViewport.setRange(range);
  updateColorbar({ field: vpPayload.field, data_range: range });
}

function clearViewport() {
  if (viewportUp) window.MeshforgeViewport.clear();
  $("#vp-field-ctl").hidden = true;
  $("#vp-colorbar").hidden = true;
  $("#vp-anim").hidden = true;
  $("#vp-overlay-ctl").hidden = true;
  $("#vp-section-ctl").hidden = true;
  $("#vp-wall-ctl").hidden = true;
  $("#vp-fit-ctl").hidden = true;
  vpPayload = null;
  vpOverlay = null;
  stopPlay();
}

// Flow section (D82): a CFD result carries a section along the vessel
// centreline; the toggle shows it (wall translucent) or the plain wall.
function setupSection(payload) {
  $("#vp-section-ctl").hidden = !payload.section;
  $("#vp-fit-ctl").hidden = !payload.section_range;
  $("#vp-wall-ctl").hidden = !(payload.section && $("#vp-section").checked);
  window.MeshforgeViewport.setWallVisible($("#vp-wall").checked);
  window.MeshforgeViewport.setSectionVisible($("#vp-section").checked);
}

// QC overlay (e.g. raw CSO contours over the reconstructed surface): cache the
// serialized polylines and show the toggle only when the artifact carries one.
function setupOverlay(payload) {
  const ctl = $("#vp-overlay-ctl");
  vpOverlay = (payload.overlay && payload.overlay.state) || null;
  if (!vpOverlay) {
    ctl.hidden = true;
    window.MeshforgeViewport.setOverlay(null);
    return;
  }
  $("#vp-overlay-name").textContent = (payload.overlay && payload.overlay.label) || "contours";
  ctl.hidden = false;
  window.MeshforgeViewport.setOverlay($("#vp-overlay").checked ? vpOverlay : null);
}

function populateFields(fields, current) {
  const sel = $("#vp-field");
  const ctl = $("#vp-field-ctl");
  if (!fields.length) {
    ctl.hidden = true;
    sel.innerHTML = "";
    sel.dataset.fields = "";
    return;
  }
  const want = fields.join("");
  if (sel.dataset.fields !== want) {
    sel.innerHTML = "";
    for (const f of fields) {
      const o = document.createElement("option");
      o.value = f;
      o.textContent = f;
      sel.appendChild(o);
    }
    sel.dataset.fields = want;
  }
  if (current) sel.value = current;
  ctl.hidden = false;
}

function updateColorbar(payload) {
  const cb = $("#vp-colorbar");
  if (!payload.field || !payload.data_range) {
    cb.hidden = true;
    return;
  }
  $("#vp-cb-min").textContent = fmtNum(payload.data_range[0]);
  $("#vp-cb-max").textContent = fmtNum(payload.data_range[1]);
  $("#vp-cb-name").textContent = payload.field;
  cb.hidden = false;
}
function fmtNum(x) {
  if (x === null || x === undefined) return "";
  const a = Math.abs(x);
  if (a !== 0 && (a < 1e-3 || a >= 1e4)) return x.toExponential(2);
  return String(Math.round(x * 1000) / 1000);
}

function setupFrames(payload) {
  vpFrames = payload.n_frames || 0;
  vpTimes = payload.times || [];
  const anim = $("#vp-anim");
  if (vpFrames > 1) {
    const slider = $("#vp-frame");
    slider.max = String(vpFrames - 1);
    slider.value = "0";
    updateTimeLabel(0);
    anim.hidden = false;
  } else {
    anim.hidden = true;
    stopPlay();
  }
}
function updateTimeLabel(i) {
  const t = vpTimes[i];
  const tt = t !== undefined ? `t=${fmtNum(t)}` : "";
  $("#vp-time").textContent = `${tt}  (${i + 1}/${vpFrames})`;
}

// Load one CFD frame; keep the camera so the animation doesn't jump.
async function loadFrame(i) {
  if (!viewportUp || vpFrames < 1) return;
  const url = `/geometry/frame/${i}` + (viewportField ? `?field=${encodeURIComponent(viewportField)}` : "");
  const res = await fetch(url);
  if (!res.ok) return;
  const payload = await res.json();
  window.MeshforgeViewport.load(payload, false);
  $("#vp-frame").value = String(i);
  updateTimeLabel(i);
}

function togglePlay() {
  if (vpPlaying) return stopPlay();
  if (vpFrames < 2) return;
  $("#vp-play").textContent = "⏸";
  vpPlaying = setInterval(() => {
    const next = (Number($("#vp-frame").value) + 1) % vpFrames;
    loadFrame(next).catch(onError);
  }, 250);
}
function stopPlay() {
  if (vpPlaying) {
    clearInterval(vpPlaying);
    vpPlaying = null;
  }
  $("#vp-play").textContent = "▶";
}

// ---- twin card (M6) -----------------------------------------------------
// The copilot report's header as a live panel. GET /twin builds it from the
// session's journaled step outputs on the server; this only paints it. Every
// string goes in through textContent (no HTML from the server is trusted).
async function refreshTwin() {
  let twin;
  try {
    twin = await apiGet("/twin");
  } catch (err) {
    return; // a twin hiccup must not spam the log; the next refresh retries
  }
  renderTwin(twin);
}

function renderTwin(twin) {
  $("#twin").dataset.lit = twin.lit ? "1" : "0";
  $("#twin-scan").textContent = twin.scan || "no scan yet";
  const mods = $("#twin-modules");
  mods.innerHTML = "";
  for (const m of twin.modules || []) {
    const li = document.createElement("li");
    li.className = "mod" + (m.lit ? " lit" : "");
    li.textContent = `${m.lit ? "●" : "○"} ${m.name} · ${m.state}`;
    mods.appendChild(li);
  }
  const list = $("#twin-slots");
  list.innerHTML = "";
  for (const s of twin.slots || []) {
    const li = document.createElement("li");
    li.className = `slot ${s.state}`;
    li.dataset.slot = s.key;
    const dot = document.createElement("span");
    dot.className = "slot-dot";
    dot.textContent = s.state === "filled" ? "●" : s.state === "next" ? "◌" : "○";
    const body = document.createElement("div");
    body.className = "slot-body";
    const head = document.createElement("div");
    head.className = "slot-head";
    const label = document.createElement("b");
    label.textContent = s.label;
    head.appendChild(label);
    if (s.note) {
      const note = document.createElement("span");
      note.className = "slot-note";
      note.textContent = s.note;
      head.appendChild(note);
    }
    const detail = document.createElement("span");
    detail.className = "slot-detail";
    detail.textContent = s.detail;
    body.append(head, detail);
    for (const v of s.values || []) {
      const val = document.createElement("span");
      val.className = "slot-value";
      val.textContent = v;
      body.appendChild(val);
    }
    li.append(dot, body);
    list.appendChild(li);
  }
}

// ---- history + state ---------------------------------------------------
async function refreshHistory() {
  const { journal } = await apiGet("/journal");
  const tbody = $("#history tbody");
  tbody.innerHTML = "";
  for (const rec of journal) {
    const tr = document.createElement("tr");
    tr.appendChild(cell(rec.step, false, "step"));
    tr.appendChild(cell(shownJson(rec.params || {}), true));
    tr.appendChild(cell(shownJson(rec.summary || {}), true));
    tbody.appendChild(tr);
  }
}
function cell(text, mono, cls) {
  const td = document.createElement("td");
  const name = cls || (mono ? "mono" : "");
  if (name) td.className = name;
  td.textContent = text;
  return td;
}
let lastJournalLen = 0; // last-seen journal length — #btn-save's disabled rule

async function refreshState() {
  const st = await apiGet("/state");
  lastRev = st.rev; // this client is now current with the session
  $("#state-input").textContent = st.input_path ? shortPath(st.input_path) : "no source";
  const box = $("#state-artifacts");
  box.innerHTML = "";
  for (const [name, present] of Object.entries(st.artifacts || {})) {
    const chip = document.createElement("span");
    chip.className = "chip" + (present ? " on" : "");
    chip.textContent = (present ? "✓ " : "· ") + name;
    box.appendChild(chip);
  }
  // "Save work file" is enabled only on a non-empty journal (GUI parity).
  lastJournalLen = st.journal_len;
  $("#btn-save").disabled = !(lastJournalLen > 0);
}
function shortPath(p) {
  const parts = String(p).split("/");
  return parts[parts.length - 1] || p;
}

// ---- auto-refresh ------------------------------------------------------
// The server bumps a monotonic session revision on every state-changing call
// (steps, reset, work-file load — from ANY client). Polling it keeps this
// window current when the session changes underneath it, e.g. another browser
// tab or the desktop shell driving the same server; no manual reload needed.
let lastRev = null; // last /state rev this client rendered
let pollBusy = false;

function startAutoRefresh() {
  setInterval(async () => {
    if (pollBusy) return;
    pollBusy = true;
    try {
      const st = await apiGet("/state");
      updateBusyLine(st); // demo mode only (no-op otherwise)
      if (lastRev !== null && st.rev !== lastRev) {
        log("session changed — resyncing view");
        await refreshHistory();
        await refreshState(); // also updates lastRev
        await refreshTwin();
        viewportField = null;
        await refreshViewport(true);
        resetFlowPicker();
        await refreshPresets();
      } else if (lastRev === null) {
        lastRev = st.rev;
      }
    } catch (err) {
      // server briefly unreachable — keep polling quietly
    } finally {
      pollBusy = false;
    }
  }, 2000);
}

// ---- busy state + inline panel errors (FIX 5 / FIX 7) -------------------
// A long step (wall/volmesh/solve can run minutes) must disable its own
// button and show a running state so a second click can't fire a concurrent
// duplicate request; re-enabling always happens in a `finally` so an error
// can never strand the button disabled (rule 6: fail loudly, not fail-shut).
async function withBusy(btn, label, fn, restoreDisabled) {
  const prevText = btn.textContent;
  btn.disabled = true;
  btn.classList.add("busy");
  btn.textContent = label;
  try {
    return await fn();
  } finally {
    btn.textContent = prevText;
    btn.classList.remove("busy");
    // Restore the STATE-DERIVED disabled value (e.g. #btn-save tracks
    // journal_len via refreshState) — never a blind `false`, which would
    // fight whatever the last state refresh already decided.
    btn.disabled = typeof restoreDisabled === "function" ? restoreDisabled() : !!restoreDisabled;
  }
}

// Errors surface next to the button that caused them, not only in the
// far-right #log (~1000px away). Cleared on that panel's next successful run.
function panelErrorFor(box) {
  let el = box.querySelector(":scope > .panel-error");
  if (!el) {
    el = document.createElement("p");
    el.className = "panel-error";
    el.hidden = true;
    box.appendChild(el);
  }
  return el;
}
function setError(el, err) {
  el.textContent = String(err && err.message ? err.message : err);
  el.hidden = false;
}
function clearErrorEl(el) {
  el.hidden = true;
}

// ---- per-panel collapse state (FIX 7) ------------------------------------
// Collapsed by default (except source + import_*); persisted per step so a
// user's layout survives a reload. localStorage throws in private-mode
// browsers, so every read/write is guarded.
function loadPanelOpenPref(step, defaultOpen) {
  try {
    const v = localStorage.getItem(`meshforge.panel.${step}`);
    if (v === "1") return true;
    if (v === "0") return false;
  } catch (err) {
    // private mode / storage disabled — fall through to the default
  }
  return defaultOpen;
}
function savePanelOpenPref(step, open) {
  try {
    localStorage.setItem(`meshforge.panel.${step}`, open ? "1" : "0");
  } catch (err) {
    // private mode / storage disabled — the open/closed state just won't persist
  }
}

// ---- build the step panels from GET /schema ----------------------------
// Native "Browse…" for STEP_KNOBS fields beyond the structural import_*.path
// rule below. "<step>.<knob>" -> dialog kind, so a new file-ish knob is a
// one-line addition here (FIX 6).
const BROWSE_KNOBS = {
  "wall.outer_surface": "file",
  "recipe.path": "file",
  "screenshot.path": "save",
  "solve.ccx": "file",
  "cfd.case": "dir",
  "segment.path": "file",
  "segment.lumen_out": "save",
  "crop.lumen": "file",
  "crop.lumen_out": "save",
  "segment.mask_out": "save",
  "sac.path": "file",
  "sac.lumen_mask": "file",
  "aaa_stress.crop_report": "file",
  "aaa_stress.lumen": "file",
  "aaa_stress.sac": "file",
  "aaa_stress.ccx": "file",
  "tissue_index.from_summary": "file",
};

function buildStepPanel(step, knobs) {
  const box = document.createElement("details");
  box.className = "step-box";
  box.dataset.step = step;
  // Collapsed by default; source + import_* start open (a first run needs
  // them immediately). Persisted per step across reloads.
  box.open = loadPanelOpenPref(step, step.startsWith("import_"));
  box.addEventListener("toggle", () => savePanelOpenPref(step, box.open));
  const summary = document.createElement("summary");
  summary.textContent = step;
  box.appendChild(summary);
  if (step === "cfd") box.appendChild(buildFlowPicker(box));

  for (const [knob, spec] of Object.entries(knobs)) {
    const label = document.createElement("label");
    label.className = "knob";
    const name = document.createElement("span");
    name.className = "knob-name";
    name.textContent = knob;
    label.appendChild(name);
    const input = makeInput(step, knob, spec);
    label.appendChild(input);
    // Native "Browse…" (desktop shell only): import_* path fields are
    // structural (every import step has one); everything else comes from
    // the explicit BROWSE_KNOBS table above.
    if (step.startsWith("import_") && knob === "path") {
      addBrowseButton(label, input, "file");
      if (step === "import_image") addBrowseButton(label, input, "dir");
    } else {
      const kind = BROWSE_KNOBS[`${step}.${knob}`];
      if (kind) addBrowseButton(label, input, kind);
    }
    box.appendChild(label);
    if (spec.help) {
      const help = document.createElement("p");
      help.className = "knob-help";
      help.textContent = spec.help;
      box.appendChild(help);
    }
  }

  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "run";
  btn.textContent = "Run " + step;
  btn.addEventListener("click", () => {
    withBusy(btn, "Running…", async () => {
      try {
        await runStep(step, box);
        clearErrorEl(panelErrorFor(box));
      } catch (err) {
        onError(err);
        setError(panelErrorFor(box), err);
      }
    }).catch(onError);
  });
  box.appendChild(btn);
  return box;
}

function makeInput(step, knob, spec) {
  const input = document.createElement("input");
  input.dataset.knob = knob;
  input.dataset.type = spec.type;
  if (spec.type === "bool") {
    input.type = "checkbox";
    input.checked = spec.default === true;
  } else if (spec.type === "int" || spec.type === "float") {
    input.type = "number";
    input.step = spec.type === "int" ? "1" : "any";
    if (spec.default !== null && spec.default !== undefined) input.value = spec.default;
  } else {
    input.type = "text";
    if (spec.default !== null && spec.default !== undefined) input.value = spec.default;
  }
  return input;
}

// Collect a step's knobs into a POST body. Empty text/number inputs are
// omitted (drop_unset parity: unset -> the compute step's own default);
// checkboxes always send their boolean (GUI sends the pre-filled default).
function gatherKnobs(box) {
  const body = {};
  for (const input of box.querySelectorAll("input[data-knob], select[data-knob]")) {
    const knob = input.dataset.knob;
    const type = input.dataset.type;
    if (type === "bool") {
      body[knob] = input.checked;
    } else if (input.value !== "") {
      body[knob] = type === "int" || type === "float" ? Number(input.value) : input.value;
    }
  }
  return body;
}

async function runStep(step, box) {
  const result = await apiPost(`/step/${step}`, gatherKnobs(box));
  await afterStep(result);
}

// ---- cfd flow-ends picker (D80) ----------------------------------------
// Click near an end of the vessel in the viewport -> the server says which
// flat end cap that is (inflow) and which is the other (outflow). The picker
// then only WRITES the cfd panel's own knobs in their plain scalar form —
//   inlet = <inflow>=<speed>   outlet = <outflow>=0   nu = <preset>
// so the recorded step, a recipe and a replay are identical to the click.
// Q and Re come from the server (POST /cfd/inflow); nothing is computed here.
let fpState = null; // {inflow, outflow} patch geometry from POST /cfd/pick
let fpPresets = null; // GET /cfd/presets payload, or null (unit unknown)
let fpPreviewTimer = null;

function buildFlowPicker(box) {
  const wrap = document.createElement("div");
  wrap.className = "flow-picker";
  wrap.innerHTML = `
    <div class="fp-head">flow ends</div>
    <div class="fp-row">
      <button type="button" id="fp-pick" class="run">Pick inflow end</button>
      <button type="button" id="fp-swap" class="run" disabled title="exchange inflow and outflow">Swap</button>
    </div>
    <p id="fp-status" class="fp-status">run lumen (or volmesh), then click Pick and click the vessel near its inflow end</p>
    <label class="knob fp-speed"><span class="knob-name">speed</span>
      <input type="number" id="fp-speed" step="any" min="0" placeholder="inflow speed" />
      <span id="fp-speed-unit" class="fp-unit"></span></label>
    <label class="knob"><span class="knob-name">speed preset</span>
      <select id="fp-speed-preset"></select></label>
    <label class="knob"><span class="knob-name">fluid</span>
      <select id="fp-nu-preset"></select></label>
    <p id="fp-presets-note" class="fp-note" hidden></p>
    <p id="fp-preview" class="fp-preview"></p>`;
  const knobInput = (name) => box.querySelector(`input[data-knob="${name}"]`);
  wrap.querySelector("#fp-pick").addEventListener("click", () => togglePick(wrap));
  wrap.querySelector("#fp-swap").addEventListener("click", () => {
    if (!fpState) return;
    fpState = { inflow: fpState.outflow, outflow: fpState.inflow };
    applyFlowEnds(knobInput);
  });
  wrap.querySelector("#fp-speed").addEventListener("input", () => applyFlowEnds(knobInput));
  wrap.querySelector("#fp-speed-preset").addEventListener("change", (e) => {
    const p = fpPresets && fpPresets.speed.find((x) => x.id === e.target.value);
    if (!p) return;
    wrap.querySelector("#fp-speed").value = p.value;
    applyFlowEnds(knobInput);
  });
  wrap.querySelector("#fp-nu-preset").addEventListener("change", (e) => {
    const p = fpPresets && fpPresets.nu.find((x) => x.id === e.target.value);
    if (!p) return;
    knobInput("nu").value = p.value;
    applyFlowEnds(knobInput);
  });
  return wrap;
}

function togglePick(wrap) {
  const btn = wrap.querySelector("#fp-pick");
  const status = wrap.querySelector("#fp-status");
  if (btn.dataset.armed === "1") { // second click cancels
    window.MeshforgeViewport.disarmPick();
    btn.dataset.armed = "";
    btn.textContent = "Pick inflow end";
    status.textContent = "pick cancelled";
    return;
  }
  if (!viewportUp) return log("pick: the 3-D viewport is unavailable", true);
  const armed = window.MeshforgeViewport.armPick(
    (point) => onFlowPicked(wrap, point).catch((err) => {
      status.textContent = String(err.message || err);
      status.classList.add("err");
    }),
    () => { status.textContent = "nothing under the cursor — click ON the vessel, near its inflow end"; },
  );
  if (!armed) return;
  btn.dataset.armed = "1";
  btn.textContent = "Cancel pick";
  status.classList.remove("err");
  status.textContent = "click the vessel near its INFLOW end (drag still rotates)";
}

async function onFlowPicked(wrap, point) {
  const btn = wrap.querySelector("#fp-pick");
  btn.dataset.armed = "";
  btn.textContent = "Pick inflow end";
  const ends = await apiPost("/cfd/pick", { point });
  fpState = { inflow: ends.inflow, outflow: ends.outflow };
  const box = wrap.closest(".step-box"); // by class: the panel's tag is not a contract
  applyFlowEnds((name) => box.querySelector(`input[data-knob="${name}"]`));
}

// Write the cfd knobs from the picker state, then refresh the Q/Re preview.
function applyFlowEnds(knobInput) {
  const status = $("#fp-status");
  $("#fp-swap").disabled = !fpState;
  if (!fpState) return;
  status.classList.remove("err");
  const a = fpState.inflow;
  status.textContent =
    `inflow: '${a.name}' (area ${fmtNum(a.area)}, D_eq ${fmtNum(a.d_eq)})` +
    `  →  outflow: '${fpState.outflow.name}' (p = 0)`;
  const speed = Number($("#fp-speed").value);
  if (speed > 0) {
    knobInput("inlet").value = `${a.name}=${speed}`;
    knobInput("outlet").value = `${fpState.outflow.name}=0`;
  }
  clearTimeout(fpPreviewTimer);
  fpPreviewTimer = setTimeout(() => previewInflow(knobInput).catch(() => {}), 250);
}

async function previewInflow(knobInput) {
  const out = $("#fp-preview");
  const speed = Number($("#fp-speed").value);
  const nu = Number(knobInput("nu").value);
  if (!fpState || !(speed > 0) || !(nu > 0)) {
    out.textContent = "";
    return;
  }
  try {
    const rep = await apiPost("/cfd/inflow", { patch: fpState.inflow.name, speed, nu });
    out.classList.remove("err");
    out.textContent = (rep.notes || []).join("\n");
  } catch (err) {
    out.classList.add("err");
    out.textContent = String(err.message || err);
  }
}

function resetFlowPicker() {
  fpState = null;
  if (viewportUp) window.MeshforgeViewport.disarmPick();
  const btn = $("#fp-pick");
  if (!btn) return;
  btn.dataset.armed = "";
  btn.textContent = "Pick inflow end";
  $("#fp-swap").disabled = true;
  $("#fp-preview").textContent = "";
  const status = $("#fp-status");
  status.classList.remove("err");
  status.textContent = "run lumen (or volmesh), then click Pick and click the vessel near its inflow end";
}

// Presets follow the SOURCE's length unit; an unknown unit is refused by the
// server and the dropdowns say why instead of offering wrong-unit numbers.
async function refreshPresets() {
  const speedSel = $("#fp-speed-preset");
  const nuSel = $("#fp-nu-preset");
  if (!speedSel || !nuSel) return;
  const note = $("#fp-presets-note");
  const fill = (sel, items, unit) => {
    sel.innerHTML = "";
    const none = document.createElement("option");
    none.value = "";
    none.textContent = items.length ? "— choose —" : "— unavailable —";
    sel.appendChild(none);
    for (const p of items) {
      const o = document.createElement("option");
      o.value = p.id;
      o.textContent = `${p.label}: ${fmtNum(p.value)} ${unit || p.unit}`;
      sel.appendChild(o);
    }
    sel.disabled = !items.length;
  };
  try {
    fpPresets = await apiGet("/cfd/presets");
    fill(speedSel, fpPresets.speed);
    fill(nuSel, fpPresets.nu);
    $("#fp-speed-unit").textContent = `${fpPresets.length_unit}/${fpPresets.time_unit}`;
    note.hidden = true;
  } catch (err) {
    fpPresets = null;
    fill(speedSel, []);
    fill(nuSel, []);
    $("#fp-speed-unit").textContent = "model units/s";
    note.textContent = "presets: " + String(err.message || err);
    note.hidden = false;
  }
}

// ---- native "Browse…" (desktop shell file bridge) ----------------------
// The desktop window loads sources by SERVER-SIDE path, so a plain
// <input type=file> is useless (it exposes contents, not a path). The shell
// injects window.meshforgeBridge (a QFileDialog over QWebChannel); in a plain
// browser / headless there is no bridge and these buttons hide themselves.
function bridgeAvailable() {
  return !!(window.qt && window.qt.webChannelTransport);
}
function addBrowseButton(container, input, kind) {
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "browse";
  btn.textContent = kind === "dir" ? "Dir…" : kind === "save" ? "Save…" : "Browse…";
  btn.hidden = !bridgeAvailable();
  btn.addEventListener("click", () => browseInto(input, kind));
  container.appendChild(btn);
}
function browseInto(input, kind) {
  const bridge = window.meshforgeBridge;
  if (!bridge) return log("native file dialog unavailable (browser mode)", true);
  let slot = bridge.open_file;
  if (kind === "dir") {
    slot = bridge.open_directory;
  } else if (kind === "save") {
    slot = bridge.save_file;
    if (!slot) {
      // Older shell build with no save_file slot — degrade, don't throw.
      log("this desktop shell has no save dialog (older build) — using the open dialog instead", true);
      slot = bridge.open_file;
    }
  }
  slot.call(bridge, (path) => {
    if (path) {
      input.value = path;
      input.dispatchEvent(new Event("input", { bubbles: true }));
    }
  });
}
// A plain browser tab can't offer a native file dialog — the server reads
// files from THIS machine by path, and a browser has no such API — so every
// Browse… button above just hides itself. Explain that once (not 12 times).
function addBrowserModeNote() {
  if (bridgeAvailable()) return;
  const note = document.createElement("p");
  note.className = "browse-note";
  note.textContent =
    "No native file picker here — this is a plain browser tab, and the " +
    "server reads files from this machine by path, so it can't offer one. " +
    "Type paths by hand, or run meshforge-desktop for native Browse… dialogs.";
  const summary = $("#source-box").querySelector(":scope > summary");
  summary.after(note);
}

// ---- sources + work-file actions --------------------------------------
async function generateSynth() {
  const shape = $("#synth-shape").value;
  await afterStep(await apiPost("/source/synth", { shape, params: {} }));
}
async function loadSurface() {
  const path = $("#load-path").value.trim();
  if (!path) throw new Error("load surface: enter a path first");
  await afterStep(await apiPost("/source/load", { path }));
}
async function saveWorkfile() {
  const path = $("#save-path").value.trim();
  if (!path) throw new Error("save work file: enter a path first");
  const { path: written } = await apiPost("/workfile/save", { path });
  log(`work file saved -> ${written}`);
}
async function loadWorkfile() {
  const path = $("#wload-path").value.trim();
  if (!path) throw new Error("load work file: enter a path first");
  const replay = $("#wload-replay").checked;
  const res = await apiPost("/workfile/load", { path, replay });
  for (const r of res.results || []) log(r.text || `${r.step}: ok`);
  if (res.results && res.results.length) showSummary(res.results[res.results.length - 1]);
  await refreshHistory();
  await refreshState();
  await refreshTwin();
  viewportField = null;
  await refreshViewport(true);
  resetFlowPicker();
  await refreshPresets();
}
async function resetSession() {
  await apiPost("/session/reset", {});
  $("#summary-view").textContent = "run a step to see its summary";
  log("session reset");
  viewportField = null;
  clearViewport();
  resetFlowPicker();
  await refreshHistory();
  await refreshState();
  await refreshTwin();
  await refreshPresets();
}

// ---- init --------------------------------------------------------------

// ---- Demo mode (D108): presentation layout ---------------------------------
// Off = the Workbench exactly as before. On (the top-bar switch; URL ?demo=1;
// localStorage meshforge.demo) = three panel groups on the left, a result
// reveal in the viewport, the running step with its elapsed seconds, and the
// planned-coverage panel above the twin. Nothing here calls a compute route
// or changes what any step does (rule 4): it only shows the same results.
const DEMO_GROUPS = [
  ["Load", ["import_image", "import_cso"]],
  ["Contour & mesh", ["segment", "sac", "crop", "wall", "volmesh"]],
  ["Simulate", ["aaa_stress", "tissue_index", "cfd"]],
];
const DEMO_VERBS = {
  import_image: "loading the scan",
  import_cso: "loading the contours",
  segment: "contouring the aorta (AI outline)",
  sac: "recovering the sac from the CT",
  crop: "cropping the aneurysm",
  wall: "meshing the wall",
  volmesh: "meshing the volume",
  aaa_stress: "solving the wall stress (finite elements)",
  tissue_index: "computing the investigational index",
  cfd: "solving the flow (CFD)",
};
function isDemo() {
  return document.body.classList.contains("demo");
}
function readDemoPref() {
  try {
    const q = new URLSearchParams(window.location.search).get("demo");
    if (q !== null) return q === "1" || q === "true" || q === "on";
    return localStorage.getItem("meshforge.demo") === "1";
  } catch (err) {
    return false; // private mode / no storage: plain Workbench
  }
}
function applyDemo(on) {
  document.body.classList.toggle("demo", on);
  const t = $("#demo-toggle");
  if (t) t.checked = on;
  try {
    localStorage.setItem("meshforge.demo", on ? "1" : "0");
  } catch (err) {
    // private mode — the URL still carries it
  }
  try {
    const url = new URL(window.location.href);
    if (on) url.searchParams.set("demo", "1");
    else url.searchParams.delete("demo");
    history.replaceState(null, "", url.toString());
  } catch (err) {
    // no history API — nothing to keep
  }
  if (on) {
    buildCoveragePanel();
    openDemoPanels();
  } else {
    removeCoveragePanel();
    hideBusyLine();
  }
}
// Tag the demo steps' panels and put a heading before the first of each group.
// The panels keep their STEP_ORDER places; the CSS hides the untagged ones.
function decorateDemoPanels() {
  const host = $("#step-panels");
  for (const [title, steps] of DEMO_GROUPS) {
    let first = null;
    for (const step of steps) {
      const box = host.querySelector(`details.step-box[data-step="${step}"]`);
      if (!box) continue;
      box.dataset.demo = "1";
      if (!first) first = box;
    }
    if (first) {
      const h = document.createElement("h3");
      h.className = "demo-group";
      h.textContent = title;
      host.insertBefore(h, first);
    }
  }
}
function openDemoPanels() {
  for (const box of document.querySelectorAll('#step-panels details.step-box[data-demo="1"]')) box.open = true;
}
// The coverage panel is cloned from the inert <template> in index.html while
// Demo is on and removed when it is off, so it is not in the DOM otherwise.
function buildCoveragePanel() {
  if ($("#coverage-panel")) return;
  const tpl = $("#coverage-svg");
  const twin = $("#twin-panel");
  if (!tpl || !twin) return;
  const node = tpl.content.firstElementChild.cloneNode(true);
  twin.parentElement.insertBefore(node, twin);
  for (const btn of node.querySelectorAll(".cov-btn")) {
    btn.addEventListener("click", () => {
      node.dataset.layer = btn.dataset.layer;
      for (const b of node.querySelectorAll(".cov-btn")) b.classList.toggle("on", b === btn);
    });
  }
}
function removeCoveragePanel() {
  const p = $("#coverage-panel");
  if (p) p.remove();
}
// A NEW result wipes in over ~2 s: a cover over the canvas (CSS clip-path
// transition) — presentation of the final data, nothing invented.
let revealTimer = null;
function playReveal() {
  if (!isDemo()) return;
  const cover = $("#vp-reveal");
  if (!cover) return;
  clearTimeout(revealTimer);
  cover.classList.remove("go");
  cover.hidden = false;
  void cover.offsetWidth; // restart the transition from the covered state
  cover.classList.add("go");
  revealTimer = setTimeout(() => {
    cover.hidden = true;
    cover.classList.remove("go");
  }, 2600);
}
// The step running on the server right now (GET /state `running`, set while
// the step holds the session lock — also when the copilot drives it over HTTP)
// with its elapsed seconds, ticking locally between polls.
let busyTicker = null;
let busyStart = null;
let busyStep = null;
function updateBusyLine(st) {
  if (!isDemo() || !st || !st.running) {
    hideBusyLine();
    return;
  }
  if (busyStep !== st.running) {
    busyStep = st.running;
    busyStart = Date.now() - 1000 * (Number(st.running_for) || 0);
  }
  if (!busyTicker) busyTicker = setInterval(tickBusyLine, 250);
  tickBusyLine();
}
function tickBusyLine() {
  const el = $("#vp-busy");
  if (!el || !busyStep) return;
  const secs = Math.max(0, Math.round((Date.now() - busyStart) / 1000));
  el.textContent = `${DEMO_VERBS[busyStep] || busyStep + " running"}… ${secs} s`;
  el.hidden = false;
}
function hideBusyLine() {
  const el = $("#vp-busy");
  if (el) el.hidden = true;
  if (busyTicker) clearInterval(busyTicker);
  busyTicker = null;
  busyStep = null;
  busyStart = null;
}
function initDemoMode() {
  const t = $("#demo-toggle");
  if (t) t.addEventListener("change", () => applyDemo(t.checked));
  applyDemo(readDemoPref());
}

async function init() {
  // Source dropdown + panel order from /catalog (rule-4-safe: served by the
  // API, not hardcoded here). One fetch so the panel builder below can use
  // catalog.step_order without a second round trip.
  let catalog = null;
  try {
    catalog = await apiGet("/catalog");
    const sel = $("#synth-shape");
    for (const shape of catalog.shapes || []) {
      const opt = document.createElement("option");
      opt.value = shape;
      opt.textContent = shape;
      sel.appendChild(opt);
    }
  } catch (err) {
    onError(err);
  }

  // Step panels from the STEP_KNOBS schema, laid out in the server's
  // explicit STEP_ORDER (/catalog.step_order -> pipeline.STEP_ORDER) so
  // import_* panels always lead. Falls back to the schema's own key order
  // (also STEP_ORDER-sorted server-side) if the catalog fetch failed or came
  // back empty. A step present in /schema but absent from step_order is
  // still appended — never silently dropped (rule 6) — with a loud log.
  try {
    const schema = await apiGet("/schema");
    const host = $("#step-panels");
    const order =
      catalog && Array.isArray(catalog.step_order) && catalog.step_order.length
        ? catalog.step_order
        : Object.keys(schema);
    const placed = new Set();
    for (const step of order) {
      if (!(step in schema)) continue; // step_order named a step /schema doesn't have
      host.appendChild(buildStepPanel(step, schema[step]));
      placed.add(step);
    }
    for (const step of Object.keys(schema)) {
      if (placed.has(step)) continue;
      log(`WARNING: step "${step}" is missing from /catalog step_order — appended at the end of the panel list`, true);
      host.appendChild(buildStepPanel(step, schema[step]));
    }
  } catch (err) {
    onError(err);
  }

  decorateDemoPanels(); // tags the demo steps + group headings (inert until body.demo)
  initDemoMode();

  // Native "Browse…" for the source/session path fields (desktop shell);
  // and the one-line explanation when there's no bridge to drive them.
  addBrowseButton($("#load-path").parentElement, $("#load-path"), "file");
  addBrowseButton($("#save-path").parentElement, $("#save-path"), "save");
  addBrowseButton($("#wload-path").parentElement, $("#wload-path"), "file");
  addBrowserModeNote();

  // source panel: collapse persistence (starts open — see index.html).
  const sourceBox = $("#source-box");
  sourceBox.open = loadPanelOpenPref("source", true);
  sourceBox.addEventListener("toggle", () => savePanelOpenPref("source", sourceBox.open));

  $("#btn-synth").addEventListener("click", () => {
    withBusy($("#btn-synth"), "Generating…", async () => {
      try {
        await generateSynth();
        clearErrorEl(panelErrorFor(sourceBox));
      } catch (err) {
        onError(err);
        setError(panelErrorFor(sourceBox), err);
      }
    }).catch(onError);
  });
  $("#btn-load").addEventListener("click", () => {
    withBusy($("#btn-load"), "Loading…", async () => {
      try {
        await loadSurface();
        clearErrorEl(panelErrorFor(sourceBox));
      } catch (err) {
        onError(err);
        setError(panelErrorFor(sourceBox), err);
      }
    }).catch(onError);
  });
  $("#btn-save").addEventListener("click", () => {
    withBusy(
      $("#btn-save"),
      "Saving…",
      async () => {
        try {
          await saveWorkfile();
          clearErrorEl($("#save-error"));
        } catch (err) {
          onError(err);
          setError($("#save-error"), err);
        }
      },
      () => !(lastJournalLen > 0) // state-derived — see refreshState()
    ).catch(onError);
  });
  $("#btn-wload").addEventListener("click", () => {
    withBusy($("#btn-wload"), "Loading…", async () => {
      try {
        await loadWorkfile();
        clearErrorEl($("#wload-error"));
      } catch (err) {
        onError(err);
        setError($("#wload-error"), err);
      }
    }).catch(onError);
  });
  $("#btn-reset").addEventListener("click", () => resetSession().catch(onError));

  initViewport();
  await refreshPresets();
  await refreshHistory();
  await refreshState();
  await refreshTwin(); // pending slots before a run; filled if a session is in progress
  await refreshViewport(true); // paints if a session was already in progress
  startAutoRefresh(); // keep the view current on external session changes
  log("Workbench ready." + (viewportUp ? "" : " (3-D viewport unavailable)"));
}

init();

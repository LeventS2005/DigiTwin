// meshforge Workbench frontend (V35 increment 2).
//
// Rule 4: NO meshing/geometry/solve logic here. This module only calls the
// increment-1 JSON API and renders its responses. Endpoints used:
//   GET  /schema  /catalog  /journal  /state  /cfd/presets
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

// ---- log + summary -----------------------------------------------------
function log(text, isErr) {
  const el = $("#log");
  const line = document.createElement("span");
  if (isErr) line.className = "err";
  line.textContent = (isErr ? "ERROR: " : "") + text.replace(/\s+$/, "") + "\n";
  el.appendChild(line);
  el.scrollTop = el.scrollHeight;
}
function showSummary(result) {
  if (result && result.summary !== undefined) {
    $("#summary-view").textContent = JSON.stringify(result.summary, null, 2);
  }
}

// After every action: surface the result text, then refresh journal + state.
async function afterStep(result) {
  if (result && result.text) log(result.text);
  showSummary(result);
  await refreshHistory();
  await refreshState();
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

// ---- history + state ---------------------------------------------------
async function refreshHistory() {
  const { journal } = await apiGet("/journal");
  const tbody = $("#history tbody");
  tbody.innerHTML = "";
  for (const rec of journal) {
    const tr = document.createElement("tr");
    tr.appendChild(cell(rec.step, false, "step"));
    tr.appendChild(cell(JSON.stringify(rec.params || {}), true));
    tr.appendChild(cell(JSON.stringify(rec.summary || {}), true));
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
  $("#btn-save").disabled = !(st.journal_len > 0);
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
      if (lastRev !== null && st.rev !== lastRev) {
        log("session changed — resyncing view");
        await refreshHistory();
        await refreshState(); // also updates lastRev
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

// ---- build the step panels from GET /schema ----------------------------
function buildStepPanel(step, knobs) {
  const box = document.createElement("fieldset");
  box.className = "step-box";
  box.dataset.step = step;
  const legend = document.createElement("legend");
  legend.textContent = step;
  box.appendChild(legend);
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
    // Native "Browse…" for import_* path fields (desktop shell only).
    if (step.startsWith("import_") && knob === "path") {
      addBrowseButton(label, input, "file");
      if (step === "import_image") addBrowseButton(label, input, "dir");
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
  btn.addEventListener("click", () => runStep(step, box).catch(onError));
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
  btn.textContent = kind === "dir" ? "Dir…" : "Browse…";
  btn.hidden = !bridgeAvailable();
  btn.addEventListener("click", () => browseInto(input, kind));
  container.appendChild(btn);
}
function browseInto(input, kind) {
  const bridge = window.meshforgeBridge;
  if (!bridge) return log("native file dialog unavailable (browser mode)", true);
  const slot = kind === "dir" ? bridge.open_directory : bridge.open_file;
  slot.call(bridge, (path) => {
    if (path) {
      input.value = path;
      input.dispatchEvent(new Event("input", { bubbles: true }));
    }
  });
}

// ---- sources + work-file actions --------------------------------------
async function generateSynth() {
  const shape = $("#synth-shape").value;
  await afterStep(await apiPost("/source/synth", { shape, params: {} }));
}
async function loadSurface() {
  const path = $("#load-path").value.trim();
  if (!path) return log("load surface: enter a path first", true);
  await afterStep(await apiPost("/source/load", { path }));
}
async function saveWorkfile() {
  const path = $("#save-path").value.trim();
  if (!path) return log("save work file: enter a path first", true);
  const { path: written } = await apiPost("/workfile/save", { path });
  log(`work file saved -> ${written}`);
}
async function loadWorkfile() {
  const path = $("#wload-path").value.trim();
  if (!path) return log("load work file: enter a path first", true);
  const replay = $("#wload-replay").checked;
  const res = await apiPost("/workfile/load", { path, replay });
  for (const r of res.results || []) log(r.text || `${r.step}: ok`);
  if (res.results && res.results.length) showSummary(res.results[res.results.length - 1]);
  await refreshHistory();
  await refreshState();
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
  await refreshPresets();
}

// ---- init --------------------------------------------------------------
async function init() {
  // Source dropdown from the synth catalog (rule-4-safe: served by the API).
  try {
    const { shapes } = await apiGet("/catalog");
    const sel = $("#synth-shape");
    for (const shape of shapes) {
      const opt = document.createElement("option");
      opt.value = shape;
      opt.textContent = shape;
      sel.appendChild(opt);
    }
  } catch (err) {
    onError(err);
  }

  // Step panels from the STEP_KNOBS schema (insertion order = pipeline order).
  try {
    const schema = await apiGet("/schema");
    const host = $("#step-panels");
    for (const [step, knobs] of Object.entries(schema)) {
      host.appendChild(buildStepPanel(step, knobs));
    }
  } catch (err) {
    onError(err);
  }

  // Native "Browse…" next to the source surface-path field (desktop shell).
  addBrowseButton($("#load-path").parentElement, $("#load-path"), "file");

  $("#btn-synth").addEventListener("click", () => generateSynth().catch(onError));
  $("#btn-load").addEventListener("click", () => loadSurface().catch(onError));
  $("#btn-save").addEventListener("click", () => saveWorkfile().catch(onError));
  $("#btn-wload").addEventListener("click", () => loadWorkfile().catch(onError));
  $("#btn-reset").addEventListener("click", () => resetSession().catch(onError));

  initViewport();
  await refreshPresets();
  await refreshHistory();
  await refreshState();
  await refreshViewport(true); // paints if a session was already in progress
  startAutoRefresh(); // keep the view current on external session changes
  log("Workbench ready." + (viewportUp ? "" : " (3-D viewport unavailable)"));
}

init();

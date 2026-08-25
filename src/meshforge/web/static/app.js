// meshforge Workbench frontend (V35 increment 2).
//
// Rule 4: NO meshing/geometry/solve logic here. This module only calls the
// increment-1 JSON API and renders its responses. Endpoints used:
//   GET  /schema  /catalog  /journal  /state
//   POST /source/synth  /source/load  /step/<name>
//   POST /workfile/save  /workfile/load  /session/reset
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
}

function clearViewport() {
  if (viewportUp) window.MeshforgeViewport.clear();
  $("#vp-field-ctl").hidden = true;
  $("#vp-colorbar").hidden = true;
  $("#vp-anim").hidden = true;
  $("#vp-overlay-ctl").hidden = true;
  vpOverlay = null;
  stopPlay();
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
}
async function resetSession() {
  await apiPost("/session/reset", {});
  $("#summary-view").textContent = "run a step to see its summary";
  log("session reset");
  viewportField = null;
  clearViewport();
  await refreshHistory();
  await refreshState();
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
  await refreshHistory();
  await refreshState();
  await refreshViewport(true); // paints if a session was already in progress
  startAutoRefresh(); // keep the view current on external session changes
  log("Workbench ready." + (viewportUp ? "" : " (3-D viewport unavailable)"));
}

init();

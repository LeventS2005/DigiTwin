// meshforge viewport (V35 increment 3) — PURE client-side vtk.js.
//
// Renders a `vtk_mesh` state fetched from GET /geometry with **no WebSocket and
// no server-side GL**: Vue + trame-vtk's `vue_vtk` bundle, both served locally
// from /vendor. Rule 4: no meshing logic — it only paints a mesh the backend
// already serialized. Exposes `window.MeshforgeViewport`; app.js drives it.

(function () {
  "use strict";

  var S = {
    app: null, viewRef: null, mesh: null, range: null, preset: null, overlay: null, ready: false,
    // D82 flow section: the server-sampled section state, whether to show it,
    // and the wall's look while it shows (translucent, so the flow reads).
    section: null, sectionState: null, showSection: true, showWall: false, meshProperty: null, wallVisible: null,
    // D80 pick mode: armed for ONE click, then disarmed.
    pickModes: null, pickCb: null, pickMiss: null, downXY: null, upXY: null,
  };

  // Fixed look for the QC overlay (raw source contours): amber unlit lines so
  // they read over any surface colormap.
  var OVERLAY_PROPERTY = { color: [1.0, 0.72, 0.2], lineWidth: 2, lighting: false };
  // The wall around a shown flow section: a faint shell for context.
  var WALL_UNDER_SECTION = { opacity: 0.15 };
  var WALL_OPAQUE = { opacity: 1.0 };
  // A press-release that moved further than this (CSS px) was a rotate-drag,
  // not a pick — the browser still fires "click" after a drag.
  var PICK_SLOP_PX = 5;

  function available() {
    return !!(window.Vue && window.vue_vtk);
  }

  // Build the Vue app once, mounted on #<elId>. Reactive refs let us swap the
  // mesh (field / animation frame) while the camera is preserved.
  function mount(elId) {
    if (!available()) return false;
    var host = document.getElementById(elId);
    if (!host) return false;
    var empty = document.getElementById("vp-empty");
    if (empty) empty.remove(); // Vue takes over the host element

    var Vue = window.Vue;
    S.mesh = Vue.ref(null);
    S.range = Vue.ref([0, 1]);
    S.preset = Vue.ref("Cool to Warm");
    S.overlay = Vue.ref(null);
    S.section = Vue.ref(null);
    S.meshProperty = Vue.ref(WALL_OPAQUE);
    S.wallVisible = Vue.ref(true);
    S.pickModes = Vue.ref([]);

    // Remember where a press started and ended so onPick can tell a click
    // from a rotate-drag (capture phase: runs before vtk.js sees the event).
    host.addEventListener("pointerdown", function (e) { S.downXY = [e.clientX, e.clientY]; }, true);
    host.addEventListener("pointerup", function (e) { S.upXY = [e.clientX, e.clientY]; }, true);

    S.app = Vue.createApp({
      setup: function () {
        var view = Vue.ref(null);
        S.viewRef = view;
        return {
          view: view,
          mesh: S.mesh,
          range: S.range,
          preset: S.preset,
          overlay: S.overlay,
          overlayProperty: OVERLAY_PROPERTY,
          section: S.section,
          meshProperty: S.meshProperty,
          wallVisible: S.wallVisible,
          pickModes: S.pickModes,
          onPick: onPick,
        };
      },
      template:
        '<vtk-view ref="view" :background="[0.063,0.063,0.071]" style="width:100%;height:100%"' +
        '          :picking-modes="pickModes" @click="onPick">' +
        '  <vtk-geometry-representation v-if="mesh && wallVisible" :color-map-preset="preset" :color-data-range="range" :property="meshProperty">' +
        '    <vtk-mesh :state="mesh"></vtk-mesh>' +
        '  </vtk-geometry-representation>' +
        '  <vtk-geometry-representation v-if="section" :color-map-preset="preset" :color-data-range="range">' +
        '    <vtk-mesh :state="section"></vtk-mesh>' +
        '  </vtk-geometry-representation>' +
        '  <vtk-geometry-representation v-if="overlay" :property="overlayProperty">' +
        '    <vtk-mesh :state="overlay"></vtk-mesh>' +
        '  </vtk-geometry-representation>' +
        "</vtk-view>",
    });
    S.app.config.errorHandler = function (err) {
      console.error("[viewport]", err);
    };
    S.app.use(window.vue_vtk);
    S.app.mount("#" + elId);
    S.ready = true;
    return true;
  }

  // A view direction no principal axis lies along: 50.7 degrees off x and y,
  // 63.6 off z. Straight tubes run along z, blocks and plates along x/y, so any
  // axis-aligned direction renders SOME common artifact as a flat outline.
  var THREE_QUARTER = [0.634, -0.634, 0.444]; // unit (1, -1, 0.7)

  // Point the camera three-quarters at the scene, without moving it closer or
  // further: the offset keeps its current length, so this is a direction-only
  // change and the resetCamera that follows sets the distance that frames the
  // mesh. vue_vtk's camera is a PLAIN OBJECT read with getCamera() and written
  // back with setCamera() — it has no vtk.js setter methods on it. Every step
  // is guarded: without those two accessors the camera keeps the direction it
  // had, resetCamera still frames the mesh, and nothing errors.
  function aimThreeQuarter(view) {
    aimAlong(view, THREE_QUARTER);
  }

  // Point the camera from direction `dir` (unit) at the focal point, same
  // distance; view-up is +z projected off `dir` (+y when `dir` is ~vertical).
  // A flow section (D82) is aimed this way so it is seen face-on.
  function aimAlong(view, dir) {
    try {
      if (!view.getCamera || !view.setCamera) return;
      var cam = view.getCamera();
      var fp = cam && cam.focalPoint;
      var pos = cam && cam.position;
      if (!fp || !pos) return;
      var dx = pos[0] - fp[0], dy = pos[1] - fp[1], dz = pos[2] - fp[2];
      var dist = Math.sqrt(dx * dx + dy * dy + dz * dz) || 1;
      cam.position = [fp[0] + dir[0] * dist, fp[1] + dir[1] * dist, fp[2] + dir[2] * dist];
      var up = Math.abs(dir[2]) > 0.95 ? [0, 1, 0] : [0, 0, 1];
      var k = up[0] * dir[0] + up[1] * dir[1] + up[2] * dir[2];
      cam.viewUp = [up[0] - k * dir[0], up[1] - k * dir[1], up[2] - k * dir[2]];
      view.setCamera(cam);
    } catch (err) {
      console.warn("[viewport] camera aim unavailable", err);
    }
  }

  // ---- flow section (D82) -------------------------------------------------
  function applySection() {
    var on = !!(S.showSection && S.sectionState);
    S.section.value = on ? S.sectionState : null;
    S.meshProperty.value = on ? WALL_UNDER_SECTION : WALL_OPAQUE;
    // With the flow map showing, the wall shell is optional (off by default:
    // even translucent it hides the colours behind it).
    S.wallVisible.value = !on || S.showWall;
  }
  function setRange(r) {
    if (S.ready && r && r.length === 2) S.range.value = r.slice();
  }
  function setWallVisible(on) {
    S.showWall = !!on;
    if (S.ready) applySection();
  }
  function setSectionVisible(on) {
    S.showSection = !!on;
    if (S.ready) applySection();
  }

  // ---- pick mode (D80) ----------------------------------------------------
  // armPick(cb, onMiss): the NEXT click on the mesh calls cb([x, y, z]) and
  // disarms; a click on empty background calls onMiss and stays armed; a
  // rotate-drag is ignored. disarmPick() cancels.
  function armPick(cb, onMiss) {
    if (!S.ready) return false;
    S.pickCb = cb;
    S.pickMiss = onMiss || null;
    S.pickModes.value = ["click"];
    return true;
  }
  function disarmPick() {
    S.pickCb = null;
    S.pickMiss = null;
    if (S.ready) S.pickModes.value = [];
  }
  function onPick(e) {
    if (!S.pickCb) return;
    if (S.downXY && S.upXY) {
      var mx = S.upXY[0] - S.downXY[0], my = S.upXY[1] - S.downXY[1];
      if (Math.sqrt(mx * mx + my * my) > PICK_SLOP_PX) return; // a drag
    }
    var pos = e && e.worldPosition;
    if (!pos || pos.length !== 3) {
      if (S.pickMiss) S.pickMiss();
      return;
    }
    var cb = S.pickCb;
    disarmPick();
    cb([Number(pos[0]), Number(pos[1]), Number(pos[2])]);
  }

  // Show a serialized mesh. `resetCamera` frames it (used on a new artifact,
  // NOT on a field/frame swap so the view stays put during animation).
  function load(payload, resetCamera) {
    if (!S.ready || !payload || !payload.state) return;
    S.mesh.value = payload.state;
    S.sectionState = payload.section || null;
    applySection();
    if (payload.data_range && payload.data_range.length === 2) {
      S.range.value = payload.data_range.slice();
    }
    if (resetCamera) {
      var dir = payload.view_dir && payload.view_dir.length === 3 ? payload.view_dir : null;
      // Let Vue flush the new mesh into the scene, then frame + repaint it.
      setTimeout(function () {
        var v = S.viewRef && S.viewRef.value;
        if (!v || !v.resetCamera) return;
        // resetCamera re-frames along the camera's CURRENT direction, and the
        // default direction is straight down +z — so a straight tube opened as
        // a flat annulus. Aim first (face-on to a flow section when there is
        // one, else three-quarters); resetCamera then recentres on the mesh
        // bounds and keeps that direction.
        if (dir) aimAlong(v, dir);
        else aimThreeQuarter(v);
        v.resetCamera();
      }, 60);
    }
  }

  function clear() {
    if (S.ready && S.mesh) S.mesh.value = null;
    if (S.ready && S.overlay) S.overlay.value = null;
    S.sectionState = null;
    if (S.ready) applySection();
    disarmPick();
  }

  // Show/hide the QC overlay (a serialized polyline state, or null). Kept
  // separate from load() so app.js can toggle it without refetching the mesh.
  function setOverlay(state) {
    if (S.ready && S.overlay) S.overlay.value = state || null;
  }

  window.MeshforgeViewport = {
    mount: mount,
    load: load,
    clear: clear,
    setOverlay: setOverlay,
    setSectionVisible: setSectionVisible,
    setWallVisible: setWallVisible,
    setRange: setRange,
    armPick: armPick,
    disarmPick: disarmPick,
    available: available,
  };
})();

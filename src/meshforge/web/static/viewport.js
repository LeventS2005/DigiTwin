// meshforge viewport (V35 increment 3) — PURE client-side vtk.js.
//
// Renders a `vtk_mesh` state fetched from GET /geometry with **no WebSocket and
// no server-side GL**: Vue + trame-vtk's `vue_vtk` bundle, both served locally
// from /vendor. Rule 4: no meshing logic — it only paints a mesh the backend
// already serialized. Exposes `window.MeshforgeViewport`; app.js drives it.

(function () {
  "use strict";

  var S = { app: null, viewRef: null, mesh: null, range: null, preset: null, overlay: null, ready: false };

  // Fixed look for the QC overlay (raw source contours): amber unlit lines so
  // they read over any surface colormap.
  var OVERLAY_PROPERTY = { color: [1.0, 0.72, 0.2], lineWidth: 2, lighting: false };

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
        };
      },
      template:
        '<vtk-view ref="view" :background="[0.063,0.063,0.071]" style="width:100%;height:100%">' +
        '  <vtk-geometry-representation v-if="mesh" :color-map-preset="preset" :color-data-range="range">' +
        '    <vtk-mesh :state="mesh"></vtk-mesh>' +
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
    try {
      if (!view.getCamera || !view.setCamera) return;
      var cam = view.getCamera();
      var fp = cam && cam.focalPoint;
      var pos = cam && cam.position;
      if (!fp || !pos) return;
      var dx = pos[0] - fp[0], dy = pos[1] - fp[1], dz = pos[2] - fp[2];
      var dist = Math.sqrt(dx * dx + dy * dy + dz * dz) || 1;
      cam.position = [
        fp[0] + THREE_QUARTER[0] * dist,
        fp[1] + THREE_QUARTER[1] * dist,
        fp[2] + THREE_QUARTER[2] * dist,
      ];
      cam.viewUp = [0, 0, 1];
      view.setCamera(cam);
    } catch (err) {
      console.warn("[viewport] camera aim unavailable", err);
    }
  }

  // Show a serialized mesh. `resetCamera` frames it (used on a new artifact,
  // NOT on a field/frame swap so the view stays put during animation).
  function load(payload, resetCamera) {
    if (!S.ready || !payload || !payload.state) return;
    S.mesh.value = payload.state;
    if (payload.data_range && payload.data_range.length === 2) {
      S.range.value = payload.data_range.slice();
    }
    if (resetCamera) {
      // Let Vue flush the new mesh into the scene, then frame + repaint it.
      setTimeout(function () {
        var v = S.viewRef && S.viewRef.value;
        if (!v || !v.resetCamera) return;
        // resetCamera re-frames along the camera's CURRENT direction, and the
        // default direction is straight down +z — so a straight tube opened as
        // a flat annulus. Aim three-quarters first; resetCamera then recentres
        // on the mesh bounds and keeps that direction.
        aimThreeQuarter(v);
        v.resetCamera();
      }, 60);
    }
  }

  function clear() {
    if (S.ready && S.mesh) S.mesh.value = null;
    if (S.ready && S.overlay) S.overlay.value = null;
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
    available: available,
  };
})();

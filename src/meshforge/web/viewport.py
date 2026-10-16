"""Display bridge for the web viewport (V35 increment 3).

Turns a session artifact's **already-computed** pyvista mesh (handed over in a
``StepResult.render_hints``) into the vtk.js state the browser canvas renders.

**Rule 4 — this is a DISPLAY concern, not meshing.** It imports none of the
core mesh-building modules (geometry / meshing / synth / solve) and *recomputes
nothing*: the mesh arrives fully built. The derivations it performs are
render-prep, never stored back on the session: ``extract_surface`` on a
*volume* grid (the visible boundary of a solid), and — when the session handed
over a flow section (D82, built in ``geometry/ribbon.py``) — probing the
displayed volume's values onto that already-built surface, so the viewport can
show the flow inside a vessel instead of its no-slip wall. ``trame_vtk`` is
**lazy-imported inside the functions** so importing the base web layer never
pulls trame (matches the CLI's discipline).

The client renders this state fully **client-side** (pure vtk.js via trame-vtk's
``vue_vtk`` bundle); there is **no WebSocket** and no server-side GL — the server
only serializes.
"""
from __future__ import annotations

from typing import Any

# vtk bookkeeping arrays created by extract_surface / the probe — not user fields.
_INTERNAL_ARRAYS = frozenset(
    {"vtkOriginalPointIds", "vtkOriginalCellIds", "vtkValidPointMask", "vtkGhostType"}
)


def _renderable(mesh: Any, kind: str) -> Any:
    """The visible surface to display. A *volume* grid shows its boundary
    (render-prep, not meshing); everything else renders as-is."""
    if kind == "volume":
        return mesh.extract_surface(algorithm="dataset_surface")
    return mesh


def available_fields(mesh: Any) -> list[str]:
    """Colour-by-field candidates on a mesh (point + cell arrays), minus the
    vtk internal bookkeeping arrays. De-duplicated, order-preserving — a field
    (e.g. CFD ``U``/``p``) can live in BOTH point and cell data."""
    seen: list[str] = []
    for name in list(mesh.point_data.keys()) + list(mesh.cell_data.keys()):
        if name not in _INTERNAL_ARRAYS and name not in seen:
            seen.append(name)
    return seen


def _state_range(state: Any) -> list[float] | None:
    field_meta = state.get("field") if isinstance(state, dict) else None
    rng = field_meta.get("dataRange") if field_meta else None
    return [float(rng[0]), float(rng[1])] if rng is not None else None


def section_view(mesh: Any, section: Any) -> Any:
    """The displayed volume's values on the session-built flow section.

    Every section point lies inside the volume by construction (D82), so the
    probe is total; the probe's own bookkeeping arrays are dropped."""
    probed = section.sample(mesh)
    for name in _INTERNAL_ARRAYS:
        if name in probed.point_data:
            del probed.point_data[name]
    return probed


def serialize(
    mesh: Any,
    kind: str = "surface",
    field: str | None = None,
    section: Any = None,
) -> dict[str, Any]:
    """``pyvista`` mesh → ``{kind, field, fields, data_range, state}`` for the
    vtk.js canvas (+ ``section`` when a flow section rides along).

    ``field`` selects the scalar coloured; ``None`` picks the first available
    field (or none, for a fieldless mesh). An unknown ``field`` raises
    ``ValueError`` (→ a loud HTTP 400 at the endpoint), never a silent miscolour.
    With a ``section`` the colour range spans the section too: the interior
    flow can exceed anything on the boundary.
    """
    surf = _renderable(mesh, kind)
    fields = available_fields(surf)
    if field is not None and field not in fields:
        raise ValueError(
            f"unknown field {field!r} for this artifact; available: {fields or '(none)'}"
        )
    if field is None and fields:
        field = fields[0]

    # Lazy import: keeps `import meshforge.web.app` (and the CLI) trame-free.
    from trame_vtk.modules.vtk import vtk_mesh

    state = vtk_mesh(surf, field_to_keep=field)
    data_range = _state_range(state)
    payload = {
        "kind": kind,
        "field": field,
        "fields": fields,
        "data_range": data_range,
        "state": state,
    }
    if section is not None and field is not None:
        probed = section_view(mesh, section)
        if field in probed.point_data:
            sec_state = vtk_mesh(probed, field_to_keep=field)
            sec_range = _state_range(sec_state)
            if data_range and sec_range:
                payload["data_range"] = [
                    min(data_range[0], sec_range[0]),
                    max(data_range[1], sec_range[1]),
                ]
            payload["section"] = sec_state
            # A robust range of the flow INSIDE (2nd-98th percentile): a small
            # fast inflow otherwise pins the scale and paints the map one colour.
            import numpy as np

            vals = np.asarray(probed.point_data[field], dtype=float)
            if vals.ndim == 2:
                vals = np.linalg.norm(vals, axis=1)
            lo, hi = np.percentile(vals, [2.0, 98.0])
            if hi > lo:
                payload["section_range"] = [float(lo), float(hi)]
    return payload


def serialize_overlay(mesh: Any) -> dict[str, Any]:
    """A QC overlay (e.g. the raw CSO contour polylines) → its bare vtk.js
    state.  Display passthrough only: no colour fields, no ranges — the canvas
    paints it in a fixed colour over the main artifact."""
    from trame_vtk.modules.vtk import vtk_mesh

    return vtk_mesh(mesh)

"""Display bridge for the web viewport (V35 increment 3).

Turns a session artifact's **already-computed** pyvista mesh (handed over in a
``StepResult.render_hints``) into the vtk.js state the browser canvas renders.

**Rule 4 — this is a DISPLAY concern, not meshing.** It imports none of the
core mesh-building modules (geometry / meshing / synth / solve) and *recomputes
nothing*: the mesh arrives fully built. The one derivation it performs is
``extract_surface`` on a *volume* grid — that is render-prep (the visible
boundary of a solid), not mesh generation, and nothing it produces is stored
back on the session. ``trame_vtk`` is **lazy-imported inside the functions** so
importing the base web layer never pulls trame (matches the CLI's discipline).

The client renders this state fully **client-side** (pure vtk.js via trame-vtk's
``vue_vtk`` bundle); there is **no WebSocket** and no server-side GL — the server
only serializes.
"""
from __future__ import annotations

from typing import Any

# vtk bookkeeping arrays created by extract_surface — not user fields.
_INTERNAL_ARRAYS = frozenset({"vtkOriginalPointIds", "vtkOriginalCellIds"})


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


def serialize(mesh: Any, kind: str = "surface", field: str | None = None) -> dict[str, Any]:
    """``pyvista`` mesh → ``{kind, field, fields, data_range, state}`` for the
    vtk.js canvas.

    ``field`` selects the scalar coloured; ``None`` picks the first available
    field (or none, for a fieldless mesh). An unknown ``field`` raises
    ``ValueError`` (→ a loud HTTP 400 at the endpoint), never a silent miscolour.
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
    data_range: list[float] | None = None
    field_meta = state.get("field") if isinstance(state, dict) else None
    if field_meta:
        rng = field_meta.get("dataRange")
        if rng is not None:
            data_range = [float(rng[0]), float(rng[1])]
    return {
        "kind": kind,
        "field": field,
        "fields": fields,
        "data_range": data_range,
        "state": state,
    }


def serialize_overlay(mesh: Any) -> dict[str, Any]:
    """A QC overlay (e.g. the raw CSO contour polylines) → its bare vtk.js
    state.  Display passthrough only: no colour fields, no ranges — the canvas
    paints it in a fixed colour over the main artifact."""
    from trame_vtk.modules.vtk import vtk_mesh

    return vtk_mesh(mesh)

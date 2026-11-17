from __future__ import annotations

import warnings
from dataclasses import dataclass, field
from typing import Any, Callable

import numpy as np
import pyvista as pv

# Ill-posedness thresholds for measured-outer sampling (trap #4): nearest-
# point thickness COLLAPSES near co-terminal end caps (the nearest outer
# point is the cap disk, not the side wall). Never trust nearest-point there.
_COLLAPSE_FRAC = 0.3  # t < 0.3·median ⇒ suspicious
_ALIGN_MIN = 0.2  # direction to nearest outer point vs vertex normal


@dataclass
class ThicknessSpec:
    """One internal representation for every thickness input form.

    Position-based on purpose (trap #6): a bare per-vertex array silently
    mis-maps under any re-triangulation, so arrays are accepted only with a
    reference surface + a loud warning, and are converted to positions
    immediately.
    """

    kind: str  # scalar | array | outer | callable
    scalar: float | None = None
    fn: Callable[[np.ndarray], np.ndarray] | None = None
    outer: pv.PolyData | None = None
    ref_points: np.ndarray | None = None
    ref_values: np.ndarray | None = None
    notes: list[str] = field(default_factory=list)

    # -- constructors ------------------------------------------------------
    @classmethod
    def from_scalar(cls, value: float) -> "ThicknessSpec":
        if value <= 0:
            raise ValueError(f"thickness must be positive, got {value}")
        return cls(kind="scalar", scalar=float(value))

    @classmethod
    def from_vertex_array(cls, values: np.ndarray, reference: pv.PolyData) -> "ThicknessSpec":
        values = np.asarray(values, dtype=float)
        if values.shape != (reference.n_points,):
            raise ValueError(
                f"thickness array has {values.shape} values for "
                f"{reference.n_points} reference vertices"
            )
        if not np.all(values > 0):
            raise ValueError("thickness array must be strictly positive")
        warnings.warn(
            "per-vertex thickness arrays are index-mapped: values must match "
            "the reference surface's triangulate() vertex order — prefer a "
            "measured outer surface or a positional field",
            UserWarning,
            stacklevel=2,
        )
        return cls(kind="array", ref_points=np.asarray(reference.points), ref_values=values)

    @classmethod
    def from_outer_surface(cls, outer: pv.PolyData) -> "ThicknessSpec":
        if outer.n_open_edges > 0:
            raise ValueError("measured outer surface must be watertight")
        return cls(kind="outer", outer=outer)

    @classmethod
    def from_callable(cls, fn: Callable[[np.ndarray], np.ndarray]) -> "ThicknessSpec":
        return cls(kind="callable", fn=fn)

    # -- sampling ----------------------------------------------------------
    def sample(self, points: np.ndarray, *, normals: np.ndarray | None = None) -> np.ndarray:
        """Per-position thickness at ``points`` (with end-cap guard+repair
        for measured outers; repairs are reported in ``self.notes``)."""
        points = np.asarray(points, dtype=float)
        if self.kind == "scalar":
            return np.full(len(points), self.scalar)
        if self.kind == "callable":
            t = np.asarray(self.fn(points), dtype=float)
            if t.shape != (len(points),):
                raise ValueError(f"thickness callable returned shape {t.shape}")
            if not np.all(t > 0):
                raise ValueError("thickness callable produced non-positive values")
            return t
        if self.kind == "array":
            from scipy.spatial import cKDTree

            _, idx = cKDTree(self.ref_points).query(points)
            return self.ref_values[idx]
        if self.kind == "outer":
            return self._sample_outer(points, normals)
        raise ValueError(f"unknown thickness spec kind {self.kind!r}")

    def _sample_outer(self, points: np.ndarray, normals: np.ndarray | None) -> np.ndarray:
        closest_cells = self.outer.find_closest_cell(points, return_closest_point=True)
        closest = closest_cells[1]
        t = np.linalg.norm(closest - points, axis=1)

        median = float(np.median(t))
        ill = t < _COLLAPSE_FRAC * median
        if normals is not None:
            step = closest - points
            norm = np.linalg.norm(step, axis=1)
            ok = norm > 1e-12
            align = np.ones(len(points))
            align[ok] = np.einsum("ij,ij->i", step[ok] / norm[ok, None], normals[ok])
            ill |= align < _ALIGN_MIN

        n_ill = int(ill.sum())
        if n_ill:
            if n_ill == len(points):
                raise ValueError(
                    "every sampled thickness is ill-posed; the 'outer' surface "
                    "probably is not an offset of this inner surface"
                )
            from scipy.spatial import cKDTree

            good = ~ill
            _, nearest_good = cKDTree(points[good]).query(points[ill])
            t[ill] = t[good][nearest_good]
            self.notes.append(
                f"repaired {n_ill}/{len(points)} ill-posed thickness samples "
                "(collapsed/misaligned nearest-point near end caps)"
            )
        return t


def resolve_thickness(
    thickness: float | None,
    thickness_spec: ThicknessSpec | None,
) -> ThicknessSpec:
    """Precedence contract (trap #5): exactly one source, loudly.

    thickness=None → the spec wins; both set is ambiguous and refused, so a
    non-uniform wall can never silently degrade to a uniform scalar."""
    if thickness is not None and thickness_spec is not None:
        raise ValueError(
            "both a scalar thickness and a thickness field were given; "
            "pass thickness=None to use the field"
        )
    if thickness is not None:
        return ThicknessSpec.from_scalar(thickness)
    if thickness_spec is not None:
        return thickness_spec
    raise ValueError("no thickness given: pass a scalar or a thickness field")

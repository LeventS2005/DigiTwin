from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pyvista as pv


@dataclass
class VolumeQuality:
    """Nan-aware VTK quality summary for a volume mesh.

    Non-finite scaled Jacobians count as inverted (trap #13): a collapsed
    cell must never escape the inverted count or poison min/mean.
    """

    n_cells: int
    n_inverted: int
    sj_min: float
    sj_mean: float
    total_volume: float
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "n_cells": self.n_cells,
            "n_inverted": self.n_inverted,
            "sj_min": self.sj_min,
            "sj_mean": self.sj_mean,
            "total_volume": self.total_volume,
            "notes": self.notes,
        }


def volume_quality(grid: pv.UnstructuredGrid) -> VolumeQuality:
    q = grid.cell_quality(["scaled_jacobian", "volume"])
    sj = np.asarray(q["scaled_jacobian"], dtype=float)
    vol = np.asarray(q["volume"], dtype=float)

    finite = np.isfinite(sj)
    inverted = (~finite) | (sj <= 0.0) | ~np.isfinite(vol) | (vol <= 0.0)
    notes: list[str] = []
    if (~finite).sum():
        notes.append(f"{int((~finite).sum())} cells with non-finite scaled Jacobian")
    if inverted.any():
        notes.append(f"{int(inverted.sum())} inverted/degenerate cells")

    sj_valid = sj[finite]
    return VolumeQuality(
        n_cells=int(grid.n_cells),
        n_inverted=int(inverted.sum()),
        sj_min=float(sj_valid.min()) if sj_valid.size else float("nan"),
        sj_mean=float(sj_valid.mean()) if sj_valid.size else float("nan"),
        total_volume=float(np.nansum(np.where(np.isfinite(vol), vol, 0.0))),
        notes=notes,
    )

from __future__ import annotations

import math

import numpy as np
import pytest

from meshforge.geometry import compute_sections, detect_constrictions
from meshforge.geometry.centerline import Centerline
from meshforge.synth import generate


def _truth_centerline(truth) -> Centerline:
    """Wrap an AnalyticTruth as a Centerline so sections are tested in
    isolation from the computed-centerline pipeline."""
    return Centerline(
        points=truth.points,
        tangents=truth.tangents,
        arc_lengths=truth.arc_lengths,
        radii=truth.radii,
        length=truth.length,
        closed=truth.closed,
        voxel_size=0.0,
        notes=[],
    )


def _interior_stations(truth, margin: float) -> np.ndarray:
    s = truth.arc_lengths
    return np.where((s > margin) & (s < truth.length - margin))[0][::25]


def test_straight_tube_sections_are_circles() -> None:
    surface, truth = generate("straight_tube")
    r = truth.radii[0]
    sections = compute_sections(surface, _truth_centerline(truth),
                                stations=_interior_stations(truth, 1.0))
    for sec in sections:
        assert sec.area == pytest.approx(math.pi * r * r, rel=0.015)
        assert sec.d_min == pytest.approx(2 * r, rel=0.02)
        assert sec.d_max == pytest.approx(2 * r, rel=0.02)
        assert sec.d_equiv == pytest.approx(2 * r, rel=0.01)


def test_tapered_tube_sections_track_radius_profile() -> None:
    surface, truth = generate("tapered_tube")
    stations = _interior_stations(truth, 1.0)
    sections = compute_sections(surface, _truth_centerline(truth), stations=stations)
    for sec, i in zip(sections, stations):
        r_s = truth.radii[i]
        assert sec.area == pytest.approx(math.pi * r_s * r_s, rel=0.02)


def test_curved_tube_sections_stay_circular() -> None:
    surface, truth = generate("curved_tube")
    r = truth.radii[0]
    sections = compute_sections(surface, _truth_centerline(truth),
                                stations=_interior_stations(truth, 1.5))
    for sec in sections:
        # Perpendicular planes cut exact circles on a circular-arc sweep.
        assert sec.area == pytest.approx(math.pi * r * r, rel=0.03)


def test_constriction_detected_at_midlength() -> None:
    surface, truth = generate("constricted_tube", depth=0.4)
    stations = _interior_stations(truth, 0.5)
    sections = compute_sections(surface, _truth_centerline(truth), stations=stations)
    found = detect_constrictions(sections, rel_drop=0.2)

    assert len(found) == 1
    c = found[0]
    assert abs(c.s - truth.length / 2.0) < 2.0  # within sigma of mid
    # Area ratio ~ (1-depth)^2 = 0.36 against the r0 baseline.
    assert c.area_ratio == pytest.approx(0.36, abs=0.06)


def test_straight_tube_has_no_constrictions() -> None:
    surface, truth = generate("straight_tube")
    sections = compute_sections(surface, _truth_centerline(truth),
                                stations=_interior_stations(truth, 1.0))
    assert detect_constrictions(sections) == []


def test_bad_rel_drop_rejected() -> None:
    surface, truth = generate("straight_tube")
    sections = compute_sections(surface, _truth_centerline(truth),
                                stations=_interior_stations(truth, 1.0))
    with pytest.raises(ValueError, match="rel_drop"):
        detect_constrictions(sections, rel_drop=1.5)

from __future__ import annotations

import numpy as np
import pytest

from digitwin.geometry import parallel_transport_frames
from digitwin.synth import generate


def _assert_orthonormal(t, n, b) -> None:
    np.testing.assert_allclose(np.linalg.norm(n, axis=1), 1.0, atol=1e-9)
    np.testing.assert_allclose(np.linalg.norm(b, axis=1), 1.0, atol=1e-9)
    np.testing.assert_allclose(np.einsum("ij,ij->i", t, n), 0.0, atol=1e-9)
    np.testing.assert_allclose(np.einsum("ij,ij->i", t, b), 0.0, atol=1e-9)
    # Convention shared with the synth lattice: n × b = t.
    np.testing.assert_allclose(np.cross(n, b), t, atol=1e-9)


def test_straight_tube_frames_have_zero_twist() -> None:
    _, truth = generate("straight_tube")
    n, b = parallel_transport_frames(truth.tangents)
    _assert_orthonormal(truth.tangents, n, b)
    # Constant tangent -> the normal must not rotate at all.
    np.testing.assert_allclose(n, np.tile(n[0], (len(n), 1)), atol=1e-12)


def test_curved_tube_frames_match_analytic_plane() -> None:
    _, truth = generate("curved_tube")
    n, b = parallel_transport_frames(truth.tangents)
    _assert_orthonormal(truth.tangents, n, b)
    # For a planar arc, parallel transport keeps the out-of-plane direction
    # fixed: one frame vector stays ±y for the whole path.
    y = np.array([0.0, 1.0, 0.0])
    ny = np.abs(n @ y)
    by = np.abs(b @ y)
    out_of_plane = ny if ny[0] > by[0] else by
    assert out_of_plane.min() > 0.999999


def test_torus_frames_are_continuous() -> None:
    _, truth = generate("torus")
    n, b = parallel_transport_frames(truth.tangents)
    _assert_orthonormal(truth.tangents, n, b)
    # No flips: successive normals stay tightly aligned.
    dots = np.einsum("ij,ij->i", n[:-1], n[1:])
    assert dots.min() > 0.999


def test_bad_inputs_fail_loudly() -> None:
    with pytest.raises(ValueError, match=r"\(N>=2, 3\)"):
        parallel_transport_frames(np.array([[0.0, 0.0, 1.0]]))
    with pytest.raises(ValueError, match="reversal"):
        parallel_transport_frames(np.array([[0.0, 0.0, 1.0], [0.0, 0.0, -1.0]]))

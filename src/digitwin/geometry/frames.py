from __future__ import annotations

import numpy as np


def _transport(v: np.ndarray, t_from: np.ndarray, t_to: np.ndarray) -> np.ndarray:
    """Rodrigues-rotate v by the rotation taking t_from onto t_to."""
    cross = np.cross(t_from, t_to)
    sin_a = np.linalg.norm(cross)
    cos_a = float(np.dot(t_from, t_to))
    if cos_a < -0.999999:
        raise ValueError(
            "tangent reversal between samples; the path is not resolved "
            "finely enough for parallel transport"
        )
    if sin_a < 1e-12:
        return v.copy()
    k = cross / sin_a
    out = v * cos_a + np.cross(k, v) * sin_a + k * np.dot(k, v) * (1.0 - cos_a)
    out -= np.dot(out, t_to) * t_to
    return out / np.linalg.norm(out)


def _rotate_about(v: np.ndarray, axis: np.ndarray, angle: float) -> np.ndarray:
    c, s = np.cos(angle), np.sin(angle)
    return v * c + np.cross(axis, v) * s + axis * np.dot(axis, v) * (1.0 - c)


def parallel_transport_frames(
    tangents: np.ndarray, *, closed: bool = False
) -> tuple[np.ndarray, np.ndarray]:
    """Twist-free (parallel-transport) frames along a tangent sequence.

    Returns (normals, binormals) with n × b = t (matches the synth
    generators' convention). Frenet frames are deliberately avoided — they
    flip at inflection points.

    ``closed=True`` treats the sequence as one period of a loop (no
    duplicated wrap sample): the transport holonomy around the loop is
    measured and distributed as a linear twist correction along the stations
    so the frame transported across the seam coincides with frame 0.
    """
    t = np.asarray(tangents, dtype=float)
    if t.ndim != 2 or t.shape[1] != 3 or len(t) < 2:
        raise ValueError(f"tangents must be (N>=2, 3), got {t.shape}")
    norms = np.linalg.norm(t, axis=1)
    if not np.allclose(norms, 1.0, atol=1e-8):
        t = t / norms[:, None]

    n = np.empty_like(t)
    # Seed: the coordinate axis least aligned with t[0], orthogonalized.
    axis = np.eye(3)[np.argmin(np.abs(t[0]))]
    seed = axis - np.dot(axis, t[0]) * t[0]
    n[0] = seed / np.linalg.norm(seed)

    for i in range(1, len(t)):
        n[i] = _transport(n[i - 1], t[i - 1], t[i])

    if closed:
        # Holonomy: transport the last frame across the seam and measure the
        # rotation about t[0] it needs to land on n[0]. Rotation about the
        # local tangent commutes with transport, so a linear ramp
        # psi_i = -phi * i/N cancels it exactly at the seam.
        n_ret = _transport(n[-1], t[-1], t[0])
        phi = float(
            np.arctan2(np.dot(np.cross(n[0], n_ret), t[0]), np.dot(n[0], n_ret))
        )
        # psi_0 = 0, psi_{N-1} = -phi: the twist spreads over the N-1
        # internal steps and the seam transport lands exactly on n[0]
        # (rotation about the local tangent commutes with transport).
        n_count = len(t)
        for i in range(1, n_count):
            n[i] = _rotate_about(n[i], t[i], -phi * i / (n_count - 1))
        check = _transport(n[-1], t[-1], t[0])
        res = float(
            np.arctan2(np.dot(np.cross(n[0], check), t[0]), np.dot(n[0], check))
        )
        if abs(res) > 1e-9:
            raise ValueError(
                f"holonomy correction failed: seam residual {res:.3e} after "
                f"distributing {phi:.3e}"
            )

    b = np.cross(t, n)  # then n × b = t
    return n, b

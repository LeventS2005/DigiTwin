"""CalculiX .dat PRINT-block parsers (promoted from the V2 test helpers so
tests and the solve step share one implementation).

The .dat file interleaves headers like ``displacements (vx,vy,vz) ...`` and
``stresses (elem, integ.pnt., sxx, ...)`` with whitespace-separated data rows.
Parsers return empty dicts when the block is absent — callers decide whether
that is an error.
"""
from __future__ import annotations

import numpy as np

from meshforge.textguard import scrub


def parse_displacements(dat: str) -> dict[int, np.ndarray]:
    """``displacements`` blocks: 1-based node id -> (3,) vector.
    Later blocks (other NSETs / steps) overwrite earlier ids."""
    u: dict[int, np.ndarray] = {}
    in_block = False
    for line in dat.splitlines():
        if "displacements" in line:
            in_block = True
            continue
        parts = line.split()
        if in_block and len(parts) == 4:
            try:
                u[int(parts[0])] = np.array([float(v) for v in parts[1:]])
            except ValueError:
                in_block = False
        elif in_block and parts:
            in_block = False
    return u


def parse_eigenfrequencies(dat: str) -> np.ndarray:
    """*FREQUENCY eigenvalue blocks (V24) -> frequencies in CYCLES/TIME.

    ccx writes (probe-verified, session 6)::

         E I G E N V A L U E   O U T P U T

         MODE NO    EIGENVALUE   ... (RAD/TIME)  (CYCLES/TIME  (RAD/TIME)
              1   0.2494069E+05   0.1579262E+03   0.2513474E+02   0.0 ...

    The frequency is the 4th column. Returns an empty array when no
    block exists (static/heat decks); malformed rows inside a block are
    loud. Multiple steps concatenate in file order.
    """
    freqs: list[float] = []
    in_block = False
    for line in dat.splitlines():
        if "E I G E N V A L U E" in line and "O U T P U T" in line:
            in_block = True
            continue
        if not in_block:
            continue
        parts = line.split()
        if not parts:
            continue
        if not parts[0].isdigit():
            if "MODE" in line or "EIGENVALUE" in line or "TIME" in line or "PART" in line.upper():
                if "P A R T I C I P A T I O N" in line:
                    in_block = False
                continue
            in_block = False
            continue
        if len(parts) != 5:
            raise ValueError(
                f"malformed eigenvalue row (expected 5 columns): {scrub(repr(line))}"
            )
        freqs.append(float(parts[3]))
    return np.asarray(freqs, dtype=float)


def parse_buckling_factors(dat: str) -> np.ndarray:
    """*BUCKLE factor blocks (V32) -> factors as ccx prints them.

    ccx writes (probe-verified, session 9)::

         B U C K L I N G   F A C T O R   O U T P U T

         MODE NO       BUCKLING
                        FACTOR

              1   0.5162394E+00

    factor × applied load = critical load. Absent -> empty array
    (non-buckle decks); malformed rows inside a block are loud.
    THE probed trap the parser CANNOT fix: requesting too few factors
    makes ccx silently return higher modes only — degenerate doublets
    vanish without any warning. Keep n_modes generous.
    """
    factors: list[float] = []
    in_block = False
    for line in dat.splitlines():
        if "B U C K L I N G" in line and "O U T P U T" in line:
            in_block = True
            continue
        if not in_block:
            continue
        parts = line.split()
        if not parts:
            continue
        if not parts[0].isdigit():
            if "MODE" in line or "FACTOR" in line:
                continue
            in_block = False
            continue
        if len(parts) != 2:
            raise ValueError(
                f"malformed buckling factor row (expected 2 columns): {scrub(repr(line))}"
            )
        factors.append(float(parts[1]))
    return np.asarray(factors, dtype=float)


def parse_stresses(dat: str) -> dict[tuple[int, int], np.ndarray]:
    """``stresses`` blocks: (1-based element id, integration point) -> (6,)
    tensor in ccx order sxx, syy, szz, sxy, sxz, syz."""
    s: dict[tuple[int, int], np.ndarray] = {}
    in_block = False
    for line in dat.splitlines():
        if "stresses" in line:
            in_block = True
            continue
        parts = line.split()
        if in_block and len(parts) == 8:
            try:
                s[(int(parts[0]), int(parts[1]))] = np.array(
                    [float(v) for v in parts[2:]]
                )
            except ValueError:
                in_block = False
        elif in_block and parts:
            in_block = False
    return s

"""The ``tet_engine`` knob: one place that normalises and checks its value.

P4 (D119): the tet fills run on gmsh (default) or TetGen. A step, the journal
and the CLI carry the engine as a plain string; this module is the one check
for it (case-insensitive, a ValueError that lists the choices, which the web
layer turns into HTTP 400) and the one rule that nothing silently switches
engine: a TetGen request that cannot be honoured is an error, never a gmsh
fill.
"""

from __future__ import annotations

from digitwin.meshing.tetfill import ENGINES

DEFAULT_TET_ENGINE = "gmsh"


def normalize_tet_engine(engine) -> str:
    """The lower-case engine name; ValueError for anything but gmsh / tetgen."""
    key = str(engine).strip().lower()
    if key not in ENGINES:
        raise ValueError(
            f"unknown tet_engine {engine!r}; choose one of: {', '.join(ENGINES)}"
        )
    return key


def fill_with_engine(engine: str, what: str, call):
    """Run ``call()`` (a ``fill_shells`` call). With TetGen, a RuntimeError
    that does not already name TetGen is re-raised naming it, so a TetGen
    failure can never read as (or be mistaken for) a gmsh one. Nothing here,
    or anywhere a caller catches this, may retry the fill with gmsh."""
    try:
        return call()
    except RuntimeError as exc:
        if engine == "tetgen" and "tetgen" not in str(exc).lower():
            raise RuntimeError(f"TetGen {what} failed: {exc}") from exc
        raise

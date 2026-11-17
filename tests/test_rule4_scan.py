"""The rule-4 AST scanner itself (V43, D65).

The scanner guards the rule-4 boundary for the web + shell layers; a bug
that never flags anything would silently void that guard (rule 6). Each
violation shape is proven to be caught, and the docstring/comment false
positive the old substring scan suffered from is proven gone.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from rule4_scan import assert_holds_no_meshing_logic, meshing_logic_references


def _scan(tmp_path: Path, source: str) -> list[str]:
    path = tmp_path / "candidate.py"
    path.write_text(source)
    return meshing_logic_references(path)


def test_clean_module_passes(tmp_path: Path) -> None:
    assert _scan(tmp_path, "from digitwin.pipeline import MeshingSession\n") == []


def test_docstring_and_comment_mentions_are_not_violations(tmp_path: Path) -> None:
    # The exact false positive the old substring scan had.
    source = (
        '"""Docs may say digitwin.meshing or offset_wall freely."""\n'
        "# comment: digitwin.solve, export_abaqus\n"
        "x = 1\n"
    )
    assert _scan(tmp_path, source) == []


@pytest.mark.parametrize(
    "line",
    [
        "import digitwin.meshing\n",
        "import digitwin.meshing.wall as w\n",
        "from digitwin.geometry import frames\n",
        "from digitwin import synth\n",
        "from ..meshing import wall\n",  # relative, inside the package
        "from digitwin.meshing.wall import offset_wall\n",
        "from somewhere import offset_wall\n",
        "import importlib\nm = importlib.import_module('digitwin.solve')\n",
        "m = __import__('digitwin.meshing')\n",
        "y = offset_wall(x)\n",
        "y = pkg.export_abaqus(x)\n",
    ],
)
def test_each_violation_shape_is_caught(tmp_path: Path, line: str) -> None:
    hits = _scan(tmp_path, line)
    assert hits, f"scanner missed a rule-4 violation: {line!r}"
    path = tmp_path / "candidate.py"
    path.write_text(line)
    with pytest.raises(AssertionError, match="rule-4 violation"):
        assert_holds_no_meshing_logic(path)


def test_unrelated_solve_names_do_not_trip(tmp_path: Path) -> None:
    # "solve" itself is not forbidden — only the digitwin.solve module and
    # the listed entry points are.
    source = "def solve(x):\n    return x\n\nresult = solve(2)\n"
    assert _scan(tmp_path, source) == []


def test_forbidden_modules_are_spelled_digitwin() -> None:
    # The guard matches module paths textually: a stale package name here would
    # let every real digitwin.* import pass unflagged (rule 6, fail loudly).
    from rule4_scan import FORBIDDEN_MODULES

    assert FORBIDDEN_MODULES and all(m.startswith("digitwin.") for m in FORBIDDEN_MODULES)


def test_positive_control_absolute_digitwin_meshing_import_is_flagged(
    tmp_path: Path,
) -> None:
    # Positive control: a scanned source that really imports the meshing layer
    # by its absolute name must be flagged, in both import forms.
    for source in ("import digitwin.meshing\n", "from digitwin.meshing import x\n"):
        hits = _scan(tmp_path, source)
        assert hits, f"scanner missed an absolute digitwin.meshing import: {source!r}"
        assert "meshing" in hits[0]

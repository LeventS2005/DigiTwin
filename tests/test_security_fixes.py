"""Session-12 security fixes (from the session-11 audit,
``security analysis findings.md``). One block per finding; all ungated.

Threat model: the trust boundary is crafted INPUT FILES (``.mfwork``,
DWG, image volumes, solver outputs) + the ``MESHFORGE_*``/PATH
environment — not the user's own CLI args.
"""
from __future__ import annotations

import json

import numpy as np
import pytest

from meshforge.meshing import (
    build_block_mesh,
    extrude_blocks,
    split_boundary_group,
    valid_group_name,
)

# A group name carrying a newline that forges a CalculiX *CLOAD card — the
# canonical F1 injection payload.
EVIL_NAME = "lid\n*CLOAD\n1,1,1.0e30"


def _cavity_wall(n: int = 4):
    """Unit-square cavity, one cell thick, rim split into lid + walls."""
    loop = np.array([[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0]])
    bm = build_block_mesh([loop], target_size=1.0 / n)
    wall = extrude_blocks(bm, thickness=0.1, n_layers=1)
    split_boundary_group(
        wall, "outer_rim", axis="y", threshold=1.0 - 1e-9, name="lid"
    )
    return wall


# ---------------------------------------------------------------------------
# F1 (MEDIUM) — boundary/patch group-name injection into decks & foam dicts


def test_valid_group_name_accepts_the_internal_names():
    # Every name meshforge mints internally must pass, or the backstop in the
    # BoundaryGroup constructor would break real meshes.
    for good in ("inner", "outer", "inlet", "outlet", "front", "back",
                 "outer_rim", "hole_rims", "body", "lid", "_tmp", "wall.1",
                 "Patch-2", "a" * 64):
        assert valid_group_name(good) == good


def test_valid_group_name_refuses_dangerous_names():
    for bad in (EVIL_NAME, "lid\ninjection", "has space", "", "1leads",
                "a" * 65, "semi;colon", "tab\tchar", "esc\x1b[2J", "eq=uals",
                "com,ma", 42, None):
        with pytest.raises(ValueError, match="invalid boundary-group name"):
            valid_group_name(bad)  # type: ignore[arg-type]


def test_split_boundary_group_refuses_newline_name():
    # (a) The direct mint site: a newline-bearing name is refused LOUDLY
    # before any group is created (fail-loud, CLAUDE.md rule 6).
    wall = _cavity_wall()
    with pytest.raises(ValueError, match="invalid boundary-group name"):
        split_boundary_group(
            wall, "outer_rim", axis="y", threshold=1e-9, name=EVIL_NAME,
            side="below",
        )
    assert EVIL_NAME not in wall.boundary  # nothing was written


def test_boundary_group_constructor_backstop():
    from meshforge.meshing import BoundaryGroup

    with pytest.raises(ValueError, match="invalid boundary-group name"):
        BoundaryGroup(
            name=EVIL_NAME,
            cell_ids=np.array([0], dtype=np.int64),
            face_nodes=np.array([[0, 1, 2, 3]], dtype=np.int64),
        )


def test_workfile_split_patch_injection_refused(tmp_path):
    # (b) The crafted-input path. load_workfile validates the step NAME and
    # that params is a mapping — but NOT the param VALUES; that gap is what
    # lets a hand-edited .mfwork carry a forged split_patch name. The mint-
    # site validator now refuses it on replay instead of producing an
    # injected deck.
    from meshforge.pipeline import load_workfile, replay

    journal = [
        {
            "step": "split_patch",
            "params": {
                "group": "outer_rim", "axis": "y", "threshold": 1e-9,
                "name": EVIL_NAME, "side": "below",
            },
        }
    ]
    wf = tmp_path / "evil.mfwork"
    wf.write_text(json.dumps({"version": 1, "journal": journal}))

    # load_workfile still accepts the crafted step (proving the value gap the
    # audit flagged) — the defense is at the mint site, on replay.
    session = load_workfile(wf)
    assert session.journal[0]["params"]["name"] == EVIL_NAME

    session.volmesh = _cavity_wall()  # the upstream mesh split_patch needs
    with pytest.raises(ValueError, match="invalid boundary-group name"):
        replay(session)


def test_cfd_spec_refuses_injected_patch_name():
    # The CFD sink: patch names become boundaryField dict keys. CfdSpec's
    # constructor backstop refuses a forged name.
    from meshforge.solve.foam_case import CfdBC, CfdSpec

    with pytest.raises(ValueError, match="invalid boundary-group name"):
        CfdSpec(
            solver="simpleFoam",
            nu=0.01,
            bcs={EVIL_NAME: CfdBC("pressure", 0.0)},
        )


# ---------------------------------------------------------------------------
# F2 (LOW) — terminal-escape injection via raw tool output in exceptions

# ESC screen-clear + OSC title/clipboard change + BEL + raw CR — the classic
# terminal-injection payload a hostile solver / crafted result file could
# emit, which the CLI would print straight to a terminal.
ANSI_PAYLOAD = "start\x1b[2J\x1b]0;pwned\x07mid\rend\x7fdel"


def test_scrub_neutralises_control_chars_keeps_newline_tab():
    from meshforge.textguard import scrub

    out = scrub(ANSI_PAYLOAD)
    # Every C0/C1 control char is gone (ESC, BEL, CR, DEL)...
    for ctrl in ("\x1b", "\x07", "\r", "\x7f"):
        assert ctrl not in out
    # ...but the readable text survives, and \n / \t are preserved.
    assert "start" in out and "pwned" in out and "end" in out
    assert scrub("a\nb\tc") == "a\nb\tc"
    # A non-str is coerced, not crashed.
    assert scrub(1234) == "1234"


def test_dat_parser_error_message_is_terminal_safe():
    # A crafted .dat whose eigenvalue row carries an ESC sequence: the parse
    # error must not carry the raw control bytes into the printed message.
    from meshforge.solve.dat import parse_eigenfrequencies

    crafted = (
        "         E I G E N V A L U E   O U T P U T\n"
        "     1   2.0   3.0   \x1b[2J\x1b]0;x\x07\n"  # digit-led, only 4 cols
    )
    with pytest.raises(ValueError, match="malformed eigenvalue row") as exc:
        parse_eigenfrequencies(crafted)
    msg = str(exc.value)
    assert "\x1b" not in msg and "\x07" not in msg  # no raw escape bytes


# ---------------------------------------------------------------------------
# F3 (LOW) — dwg2dxf hang / DoS on a malformed DWG


def test_dwg2dxf_timeout_is_loud(monkeypatch):
    # A malformed/adversarial .dwg that drives LibreDWG into a hang must not
    # hang meshforge forever: the call is now capped at 120s and a timeout
    # raises a clear RuntimeError naming the file.
    import subprocess
    from pathlib import Path

    from meshforge.io import cad

    monkeypatch.setattr(cad.shutil, "which", lambda _name: "/usr/bin/dwg2dxf")

    def fake_run(argv, **kwargs):
        assert kwargs.get("timeout") == 120  # the cap is actually wired
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    monkeypatch.setattr(cad.subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match=r"exceeded the 120s timeout on evil\.dwg"):
        cad._open_document(Path("evil.dwg"), [], {})


# ---------------------------------------------------------------------------
# F4 (LOW) — image ingest memory-exhaustion via unbounded voxel count


def test_image_ingest_refuses_oversized_volume():
    # The V18 image path now enforces the same MAX_VOXELS ceiling the geometry
    # paths use. A zero-memory broadcast view lets us trip the real 40M limit
    # without allocating a giant array — the guard reads only array.shape and
    # fires before any full-array scan.
    from meshforge.geometry.centerline import MAX_VOXELS
    from meshforge.io.image import volume_to_surface

    n = 350  # 350**3 = 42_875_000 > 40_000_000
    assert n ** 3 > MAX_VOXELS
    oversized = np.broadcast_to(np.zeros((1, 1, 1)), (n, n, n))
    with pytest.raises(ValueError, match=rf"exceeds {MAX_VOXELS} voxels"):
        volume_to_surface(oversized, level=0.5)

"""V1 CAD import: chaining clean-up, arcs, units, blocks, layers, real DWG."""
from __future__ import annotations

import json
import math
import os
import shutil
from pathlib import Path

import numpy as np
import pytest

ezdxf = pytest.importorskip("ezdxf")

from meshforge.cli import main
from meshforge.io import file_sha256
from meshforge.io.cad import (
    MODELSPACE,
    _point_in_loop,
    _signed_area,
    list_blocks,
    load_profile,
)

# Registered in INPUTS.md — the user DWG is READ-ONLY (CLAUDE.md rule 1).
COTTAGE_SHA256 = "a4cb1d4085881dde3a7821b4bc4768cf7e991c160d5ec7a6d53b46753e7d9068"


def _cottage_dwg() -> Path | None:
    env = os.environ.get("MESHFORGE_COTTAGE_DWG")
    if env and Path(env).is_file():
        return Path(env)
    cand = Path(__file__).resolve().parents[1] / "Cottage_Pane_Window.dwg"
    return cand if cand.is_file() else None


needs_cottage = pytest.mark.skipif(
    shutil.which("dwg2dxf") is None or _cottage_dwg() is None,
    reason="dwg2dxf converter or Cottage_Pane_Window.dwg not available",
)


def _new_doc(units: int | None = 4):
    doc = ezdxf.new()
    if units is None:
        del doc.header["$INSUNITS"]
    else:
        doc.header["$INSUNITS"] = units
    return doc


def _save(doc, tmp_path: Path, name: str = "test.dxf") -> Path:
    path = tmp_path / name
    doc.saveas(path)
    return path


def _add_rect_lines(msp, x0, y0, w, h, *, order=(2, 0, 3, 1), reverse=(1, 3), layer="0"):
    """Rectangle as 4 loose LINEs, shuffled and partially reversed."""
    c = [(x0, y0), (x0 + w, y0), (x0 + w, y0 + h), (x0, y0 + h)]
    edges = [(c[i], c[(i + 1) % 4]) for i in range(4)]
    for i in order:
        a, b = edges[i]
        if i in reverse:
            a, b = b, a
        msp.add_line(a, b, dxfattribs={"layer": layer})


def _perimeter(loop: np.ndarray) -> float:
    closed = np.vstack([loop, loop[:1]])
    return float(np.linalg.norm(np.diff(closed, axis=0), axis=1).sum())


# ---------------------------------------------------------------------------
# chaining clean-up


def test_chaining_shuffled_reversed_rect_with_hole(tmp_path):
    doc = _new_doc()
    msp = doc.modelspace()
    _add_rect_lines(msp, 0, 0, 10, 8)
    _add_rect_lines(msp, 3, 3, 4, 3)
    prof = load_profile(_save(doc, tmp_path))

    assert prof.mesh_ready
    assert prof.units == "mm"
    assert len(prof.loops) == 2
    assert prof.components == [{"outer": 0, "holes": [1]}]
    assert _signed_area(prof.loops[0]) == pytest.approx(80.0)  # outer CCW
    assert _signed_area(prof.loops[1]) == pytest.approx(-12.0)  # hole CW
    assert _point_in_loop(prof.loops[1][0], prof.loops[0])
    assert prof.provenance["open_chains"] == []


def test_unclosed_gap_reported_loudly(tmp_path):
    doc = _new_doc()
    msp = doc.modelspace()
    msp.add_line((0, 0), (10, 0))
    msp.add_line((10, 0), (10, 8))
    msp.add_line((10, 8), (0, 8))
    msp.add_line((0, 8), (0, 0.5))  # 0.5 short of closing
    prof = load_profile(_save(doc, tmp_path))

    assert not prof.mesh_ready
    assert prof.loops == []
    recs = prof.provenance["open_chains"]
    assert len(recs) == 1
    assert recs[0]["gap"] == pytest.approx(0.5, rel=1e-9)
    assert recs[0]["n_points"] == 5
    assert any("UNCLOSED" in n for n in prof.provenance["notes"])


def test_gap_below_default_tolerance_closes(tmp_path):
    doc = _new_doc()
    msp = doc.modelspace()
    msp.add_line((0, 0), (10, 0))
    msp.add_line((10, 0), (10, 8))
    msp.add_line((10, 8), (0, 8))
    msp.add_line((0, 8), (0, 1e-9))  # default tol = 1e-6 x diag ~ 1.3e-5
    prof = load_profile(_save(doc, tmp_path))
    assert prof.mesh_ready
    assert len(prof.loops) == 1


def test_chain_tol_knob(tmp_path):
    doc = _new_doc()
    msp = doc.modelspace()
    msp.add_line((0, 0), (10, 0))
    msp.add_line((10, 0), (10, 8))
    msp.add_line((10, 8), (0, 8))
    msp.add_line((0, 8), (0, 0.05))
    path = _save(doc, tmp_path)

    wide = load_profile(path, chain_tol=0.1)
    assert wide.mesh_ready and len(wide.loops) == 1
    tight = load_profile(path, chain_tol=0.01)
    assert not tight.mesh_ready and len(tight.provenance["open_chains"]) == 1


def test_duplicate_segments_dropped(tmp_path):
    doc = _new_doc()
    msp = doc.modelspace()
    _add_rect_lines(msp, 0, 0, 10, 8, order=(0, 1, 2, 3), reverse=())
    msp.add_line((0, 0), (10, 0))  # exact duplicate of the bottom edge
    msp.add_line((10, 8), (10, 0))  # reversed duplicate of the right edge
    prof = load_profile(_save(doc, tmp_path))

    assert prof.mesh_ready
    assert len(prof.loops) == 1
    assert prof.provenance["counts"]["duplicates_dropped"] == 2
    assert any("duplicate" in n for n in prof.provenance["notes"])


def test_junction_chain_mode_keeps_rect_reports_chord(tmp_path):
    doc = _new_doc()
    msp = doc.modelspace()
    _add_rect_lines(msp, 0, 0, 10, 8, order=(0, 1, 2, 3), reverse=())
    msp.add_line((0, 0), (10, 8))  # chord touching two corners -> junctions

    prof = load_profile(_save(doc, tmp_path), mode="chain")
    assert not prof.mesh_ready  # the chord cannot close in chain mode
    assert len(prof.loops) == 1  # ... but the rectangle still does
    assert abs(_signed_area(prof.loops[0])) == pytest.approx(80.0)
    assert len(prof.provenance["open_chains"]) == 1
    assert any("junction" in n for n in prof.provenance["notes"])


def test_junction_auto_switches_to_faces(tmp_path):
    doc = _new_doc()
    msp = doc.modelspace()
    _add_rect_lines(msp, 0, 0, 10, 8, order=(0, 1, 2, 3), reverse=())
    msp.add_line((0, 0), (10, 8))  # chord splits the rect into two triangles

    prof = load_profile(_save(doc, tmp_path))  # mode="auto"
    assert prof.mesh_ready
    assert prof.provenance["mode"] == "faces"
    assert len(prof.components) == 2
    tri = sorted(_signed_area(prof.loops[c["outer"]]) for c in prof.components)
    assert tri == pytest.approx([40.0, 40.0])  # both CCW faces
    assert any("face extraction" in n for n in prof.provenance["notes"])


def test_faces_mode_rect_in_rect_ring_plus_island(tmp_path):
    doc = _new_doc()
    msp = doc.modelspace()
    _add_rect_lines(msp, 0, 0, 10, 8)
    _add_rect_lines(msp, 3, 3, 4, 3)
    prof = load_profile(_save(doc, tmp_path), mode="faces")

    assert prof.mesh_ready
    assert len(prof.components) == 2
    ring, island = prof.components  # sorted by |outer area| desc
    assert _signed_area(prof.loops[ring["outer"]]) == pytest.approx(80.0)
    assert len(ring["holes"]) == 1
    assert _signed_area(prof.loops[ring["holes"][0]]) == pytest.approx(-12.0)
    assert _signed_area(prof.loops[island["outer"]]) == pytest.approx(12.0)
    assert island["holes"] == []


def test_faces_mode_splits_t_junctions(tmp_path):
    doc = _new_doc()
    msp = doc.modelspace()
    _add_rect_lines(msp, 0, 0, 20, 10, order=(0, 1, 2, 3), reverse=())
    msp.add_line((10, 0), (10, 10))  # stem ends on edge interiors (two Ts)
    prof = load_profile(_save(doc, tmp_path), mode="faces")

    assert prof.mesh_ready
    assert len(prof.components) == 2
    areas = sorted(_signed_area(prof.loops[c["outer"]]) for c in prof.components)
    assert areas == pytest.approx([100.0, 100.0])
    assert prof.provenance["counts"]["t_splits"] == 2


def test_degenerate_loop_dropped_loudly(tmp_path):
    doc = _new_doc()
    msp = doc.modelspace()
    _add_rect_lines(msp, 0, 0, 10, 8, order=(0, 1, 2, 3), reverse=())
    # Tiny triangle far below the area floor (and below chain_tol size).
    msp.add_line((1, 1), (1 + 1e-6, 1))
    msp.add_line((1 + 1e-6, 1), (1, 1 + 2e-6))
    msp.add_line((1, 1 + 2e-6), (1, 1))
    prof = load_profile(_save(doc, tmp_path))

    assert len(prof.loops) == 1  # only the rectangle survives
    assert prof.provenance["counts"]["degenerate_dropped"] >= 1
    assert prof.mesh_ready


# ---------------------------------------------------------------------------
# arcs + flattening accuracy


def test_circle_flattening_accuracy(tmp_path):
    doc = _new_doc()
    doc.modelspace().add_circle((0, 0), 10)
    prof = load_profile(_save(doc, tmp_path), arc_tol=0.01)

    assert prof.mesh_ready
    assert len(prof.loops) == 1
    area = abs(_signed_area(prof.loops[0]))
    assert area == pytest.approx(math.pi * 100, rel=5e-3)
    assert _perimeter(prof.loops[0]) == pytest.approx(2 * math.pi * 10, rel=5e-3)


def test_bulged_lwpolyline_circle(tmp_path):
    doc = _new_doc()
    # Two vertices with bulge=1 (semicircles) closed -> full circle r=10.
    doc.modelspace().add_lwpolyline(
        [(-10, 0, 0, 0, 1.0), (10, 0, 0, 0, 1.0)], format="xyseb", close=True
    )
    prof = load_profile(_save(doc, tmp_path), arc_tol=0.01)
    assert prof.mesh_ready
    assert abs(_signed_area(prof.loops[0])) == pytest.approx(math.pi * 100, rel=5e-3)


def test_stadium_chains_lines_and_arcs(tmp_path):
    doc = _new_doc()
    msp = doc.modelspace()
    msp.add_line((-5, -2), (5, -2))
    msp.add_arc(center=(5, 0), radius=2, start_angle=-90, end_angle=90)
    msp.add_line((5, 2), (-5, 2))
    msp.add_arc(center=(-5, 0), radius=2, start_angle=90, end_angle=270)
    prof = load_profile(_save(doc, tmp_path), arc_tol=1e-3)

    assert prof.mesh_ready
    assert len(prof.loops) == 1
    truth = 10 * 4 + math.pi * 4  # rect + two semicircle caps
    assert abs(_signed_area(prof.loops[0])) == pytest.approx(truth, rel=1e-2)


# ---------------------------------------------------------------------------
# units


def test_units_missing_header_is_unitless_with_warning(tmp_path):
    doc = _new_doc(units=None)
    _add_rect_lines(doc.modelspace(), 0, 0, 10, 8)
    prof = load_profile(_save(doc, tmp_path))
    assert prof.units == "unitless"
    assert any("$INSUNITS" in n for n in prof.provenance["notes"])


def test_units_inches(tmp_path):
    doc = _new_doc(units=1)
    _add_rect_lines(doc.modelspace(), 0, 0, 10, 8)
    prof = load_profile(_save(doc, tmp_path))
    assert prof.units == "in"
    assert prof.provenance["insunits"] == 1


# ---------------------------------------------------------------------------
# block selection + layers


def _doc_with_blocks():
    doc = _new_doc()
    doc.modelspace().add_point((0, 0))  # unsupported only -> triggers fallback
    big = doc.blocks.new("BIG")
    _add_rect_lines(big, 0, 0, 20, 10)
    small = doc.blocks.new("SMALL")
    small.add_circle((0, 0), 5)
    anon = doc.blocks.new_anonymous_block()  # more entities than BIG, but
    _add_rect_lines(anon, 0, 0, 5, 5)  # anonymous -> must be excluded
    _add_rect_lines(anon, 10, 0, 5, 5)
    return doc


def test_block_autoselect_largest_named(tmp_path):
    prof = load_profile(_save(_doc_with_blocks(), tmp_path))
    assert prof.provenance["block"] == "BIG"
    assert any("auto-selected" in n for n in prof.provenance["notes"])
    assert len(prof.loops) == 1
    assert _signed_area(prof.loops[0]) == pytest.approx(200.0)


def test_block_explicit_selection(tmp_path):
    prof = load_profile(_save(_doc_with_blocks(), tmp_path), block="SMALL")
    assert prof.provenance["block"] == "SMALL"
    assert abs(_signed_area(prof.loops[0])) == pytest.approx(math.pi * 25, rel=5e-3)


def test_block_missing_lists_candidates(tmp_path):
    with pytest.raises(ValueError, match="BIG"):
        load_profile(_save(_doc_with_blocks(), tmp_path), block="NOPE")


def test_list_blocks_excludes_anonymous(tmp_path):
    entries = list_blocks(_save(_doc_with_blocks(), tmp_path))
    names = [e["name"] for e in entries]
    assert names[0] == MODELSPACE
    assert "BIG" in names and "SMALL" in names
    assert not any(n.startswith("*") for n in names)
    big = next(e for e in entries if e["name"] == "BIG")
    assert big["n_supported"] == 4
    assert big["types"] == {"LINE": 4}


def test_layers_filter(tmp_path):
    doc = _new_doc()
    msp = doc.modelspace()
    _add_rect_lines(msp, 0, 0, 10, 8, layer="KEEP")
    _add_rect_lines(msp, 20, 0, 5, 5, layer="DROP")
    path = _save(doc, tmp_path)

    both = load_profile(path)
    assert len(both.components) == 2
    kept = load_profile(path, layers=["KEEP"])
    assert len(kept.loops) == 1
    assert abs(_signed_area(kept.loops[0])) == pytest.approx(80.0)


# ---------------------------------------------------------------------------
# CLI


def test_cli_import_cad_roundtrip(tmp_path, capsys):
    doc = _new_doc()
    _add_rect_lines(doc.modelspace(), 0, 0, 10, 8)
    _add_rect_lines(doc.modelspace(), 3, 3, 4, 3)
    dxf = _save(doc, tmp_path, "part.dxf")
    outdir = tmp_path / "outputs"

    rc = main(["import-cad", str(dxf), "--outputs", str(outdir)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "mesh_ready  : True" in out
    data = json.loads((outdir / "part_profile.json").read_text())
    assert data["mesh_ready"] is True
    assert data["units"] == "mm"
    assert len(data["loops"]) == 2
    assert (outdir / "part_profile.txt").is_file()


def test_cli_import_cad_unclosed_exits_1(tmp_path, capsys):
    doc = _new_doc()
    msp = doc.modelspace()
    msp.add_line((0, 0), (10, 0))
    msp.add_line((10, 0), (10, 8))
    msp.add_line((10, 8), (0, 8))
    msp.add_line((0, 8), (0, 0.5))
    dxf = _save(doc, tmp_path, "gappy.dxf")

    rc = main(["import-cad", str(dxf), "--outputs", str(tmp_path / "outputs")])
    out = capsys.readouterr().out
    assert rc == 1
    assert "UNCLOSED" in out


def test_cli_list_blocks(tmp_path, capsys):
    dxf = _save(_doc_with_blocks(), tmp_path, "blocks.dxf")
    rc = main(["import-cad", str(dxf), "--list-blocks"])
    out = capsys.readouterr().out
    assert rc == 0
    assert MODELSPACE in out and "BIG" in out and "SMALL" in out


# ---------------------------------------------------------------------------
# real user DWG (skips cleanly when converter or file is absent)


@needs_cottage
def test_cottage_dwg_list_blocks():
    dwg = _cottage_dwg()
    before = file_sha256(dwg)
    assert before == COTTAGE_SHA256

    entries = list_blocks(dwg)
    names = {e["name"] for e in entries}
    assert {"Cottage Pane 5 x 4", "Cottage Pane 5"} <= names
    big = next(e for e in entries if e["name"] == "Cottage Pane 5 x 4")
    assert big["n_supported"] == 206

    assert file_sha256(dwg) == before  # rule 1: input byte-identical


@needs_cottage
def test_cottage_dwg_end_to_end():
    """Honest discovered structure (investigated 2026-07-06): the drawing is a
    planar subdivision (84 three-way junctions, 76 T-points), so auto mode
    switches to face extraction. 120 regions tile the elevation: the frame
    ring, exactly 20 panes of 177.03 x 170.68 mm (5 x 4 grid), 3 mullion
    gap strips, and 96 glazing-bar/trim faces."""
    dwg = _cottage_dwg()
    before = file_sha256(dwg)
    assert before == COTTAGE_SHA256

    prof = load_profile(dwg)
    assert file_sha256(dwg) == before  # rule 1: input byte-identical

    assert prof.provenance["block"] == "Cottage Pane 5 x 4"
    assert prof.units == "mm"
    assert prof.provenance["mode"] == "faces"
    assert prof.mesh_ready  # every region closed
    assert prof.provenance["open_chains"] == []
    assert prof.provenance["counts"]["merged_lines"] == 12  # DXF repair (D14)
    assert prof.provenance["counts"]["t_splits"] == 76

    lo, hi = prof.bbox()
    assert abs((hi[0] - lo[0]) - 921.1) < 0.5
    assert abs((hi[1] - lo[1]) - 1107.4) < 0.5

    assert len(prof.components) == 120

    # The 20 panes: faces with bbox ~177.03 x 170.68 mm.
    panes = []
    for comp in prof.components:
        outer = prof.loops[comp["outer"]]
        l, h = outer.min(axis=0), outer.max(axis=0)
        if abs((h[0] - l[0]) - 177.03) < 0.1 and abs((h[1] - l[1]) - 170.68) < 0.1:
            panes.append(comp)
    assert len(panes) == 20

    # Largest face = the frame ring: outer rect with one inner boundary hole.
    ring = prof.components[0]  # sorted by |outer area| desc
    ring_outer = prof.loops[ring["outer"]]
    assert _signed_area(ring_outer) == pytest.approx(921.11 * 1107.41, rel=1e-3)
    assert len(ring["holes"]) == 1

    # Every component: one CCW outer, holes CW and strictly inside it.
    for comp in prof.components:
        outer = prof.loops[comp["outer"]]
        assert _signed_area(outer) > 0
        for hole_i in comp["holes"]:
            hole = prof.loops[hole_i]
            assert _signed_area(hole) < 0
            assert _point_in_loop(hole[0], outer)

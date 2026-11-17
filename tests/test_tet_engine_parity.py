"""Engine parity of the lumen tet fill: gmsh and TetGen on the same closed
straight tube at the same mesh size must agree on volume, boundary labels
and cap areas, with every cell positive. Skips without ``tetgen``."""
from __future__ import annotations

import numpy as np
import pytest

pytest.importorskip("tetgen")
pytest.importorskip("gmsh")

from digitwin.meshing.lumen import fill_lumen  # noqa: E402
from digitwin.synth import generate  # noqa: E402

MESH_SIZE = 1.0


def _areas(wall) -> dict[str, float]:
    pts = np.asarray(wall.grid.points)
    out = {}
    for name, g in wall.boundary.items():
        f = np.asarray(g.face_nodes)
        assert f.shape[1] == 3, f"{name}: expected triangles, got {f.shape}"
        a, b, c = pts[f[:, 0]], pts[f[:, 1]], pts[f[:, 2]]
        out[name] = float(0.5 * np.linalg.norm(np.cross(b - a, c - a), axis=1).sum())
    return out


@pytest.fixture(scope="module")
def runs():
    surf, _ = generate("straight_tube")
    res = {"surface_volume": float(surf.volume)}
    for engine in ("gmsh", "tetgen"):
        wall = fill_lumen(surf, method="tet", mesh_size=MESH_SIZE, tet_engine=engine)
        cs = wall.grid.compute_cell_sizes(length=False, area=False, volume=True)
        vols = np.asarray(cs.cell_data["Volume"])
        res[engine] = {
            "wall": wall,
            "n_cells": int(wall.grid.n_cells),
            "volume": float(vols.sum()),
            "min_cell_volume": float(vols.min()),
            "areas": _areas(wall),
            "faces": {k: g.n_faces for k, g in wall.boundary.items()},
        }
    return res


def _msg(runs) -> str:
    return (
        f"cells gmsh={runs['gmsh']['n_cells']} tetgen={runs['tetgen']['n_cells']}; "
        f"volume gmsh={runs['gmsh']['volume']:.4f} tetgen={runs['tetgen']['volume']:.4f} "
        f"surface={runs['surface_volume']:.4f}; "
        f"faces gmsh={runs['gmsh']['faces']} tetgen={runs['tetgen']['faces']}"
    )


def test_volumes_agree_with_each_other_and_the_surface(runs):
    g, t, s = runs["gmsh"]["volume"], runs["tetgen"]["volume"], runs["surface_volume"]
    assert abs(g - t) / t < 0.01, _msg(runs)
    assert abs(t - s) / s < 0.01, _msg(runs)
    assert abs(g - s) / s < 0.01, _msg(runs)


def test_same_boundary_labels_and_cap_areas(runs):
    assert set(runs["gmsh"]["faces"]) == set(runs["tetgen"]["faces"]) == {
        "wall", "inlet", "outlet"}, _msg(runs)
    for lab in ("inlet", "outlet"):
        ag, at = runs["gmsh"]["areas"][lab], runs["tetgen"]["areas"][lab]
        assert abs(ag - at) / at < 0.01, f"{lab} area gmsh={ag} tetgen={at}; " + _msg(runs)
    assert runs["gmsh"]["areas"]["wall"] == pytest.approx(
        runs["tetgen"]["areas"]["wall"], rel=0.01), _msg(runs)


@pytest.mark.parametrize("engine", ["gmsh", "tetgen"])
def test_all_cells_positive(runs, engine):
    r = runs[engine]
    assert r["min_cell_volume"] > 0, f"{engine} min cell volume {r['min_cell_volume']}; " + _msg(runs)
    assert r["wall"].quality.n_inverted == 0, _msg(runs)


@pytest.mark.parametrize("engine", ["gmsh", "tetgen"])
def test_engine_recorded(runs, engine):
    assert f"tet engine: {engine}" in runs[engine]["wall"].notes, _msg(runs)
    assert f"tet engine: {engine}" in runs[engine]["wall"].summary()["notes"]


def _fill_notes(runs, engine) -> list[str]:
    return [n for n in runs[engine]["wall"].notes if n.startswith("tetgen ")]


def test_engines_are_told_apart_by_the_fill_note_and_the_mesh(runs):
    # "tet engine: X" is echoed from the request, so it cannot tell the engines
    # apart; the TetGen fill note comes from the fill itself
    tg = _fill_notes(runs, "tetgen")
    assert len(tg) == 1 and tg[0].startswith("tetgen ") and " -pYq1.4a" in tg[0], _msg(runs)
    assert _fill_notes(runs, "gmsh") == [], _msg(runs)
    assert not any("tetgen" in n.lower() and "tet engine" not in n
                   for n in runs["gmsh"]["wall"].notes), _msg(runs)
    # two different meshers: the cell counts differ
    assert runs["gmsh"]["n_cells"] != runs["tetgen"]["n_cells"], _msg(runs)

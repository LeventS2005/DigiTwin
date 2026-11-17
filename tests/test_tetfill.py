"""Engine-level tests of ``digitwin.meshing.tetfill.fill_shells`` with the
TetGen engine (TetGen: AGPL v3, private use only; optional ``tetgen``
package). The tests that run TetGen skip cleanly without the package; the
checks that only need the dispatch and the loud errors run everywhere."""

from __future__ import annotations

import math
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import numpy as np
import pytest

from digitwin.meshing import tetfill
from digitwin.meshing.tetfill import fill_shells, interior_point

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def tetgen_pkg():
    return pytest.importorskip("tetgen")


# ---------------------------------------------------------------- geometry
def revolved(zs, radii, n=24):
    """Closed shell of revolution about z: rings at ``zs`` with ``radii``,
    fan caps on the end axis points. Outward-wound."""
    zs, radii = np.asarray(zs, float), np.asarray(radii, float)
    th = np.linspace(0, 2 * np.pi, n, endpoint=False)
    pts = [[r * math.cos(t), r * math.sin(t), z] for z, r in zip(zs, radii) for t in th]
    pts += [[0, 0, zs[0]], [0, 0, zs[-1]]]
    pts = np.array(pts)
    tris = []
    for i in range(len(zs) - 1):
        for j in range(n):
            a, b = i * n + j, i * n + (j + 1) % n
            tris += [[a, b, b + n], [a, b + n, a + n]]
    lo, hi, last = len(pts) - 2, len(pts) - 1, (len(zs) - 1) * n
    for j in range(n):
        tris.append([lo, (j + 1) % n, j])
        tris.append([hi, last + j, last + (j + 1) % n])
    return pts, np.array(tris)


def tube(r, z0, z1, n=24, rings=8):
    return revolved(np.linspace(z0, z1, rings), np.full(rings, r), n)


def bent_tube(r=3.0, bend=20.0, n=16, rings=24):
    """A tube swept along a 120 degree arc (concave: the centroid of the
    shell is outside the solid). Frames by rotation about the arc axis."""
    ang = np.linspace(0, 2 * np.pi / 3, rings)
    pts = []
    for a in ang:
        c = np.array([bend * math.sin(a), 0.0, bend * (1 - math.cos(a))])
        tang = np.array([math.cos(a), 0.0, math.sin(a)])
        e1 = np.array([0.0, 1.0, 0.0])
        e2 = np.cross(tang, e1)
        for t in np.linspace(0, 2 * np.pi, n, endpoint=False):
            pts.append(c + r * (math.cos(t) * e1 + math.sin(t) * e2))
    pts += [pts[0] * 0 + np.array([0, 0, 0.0]),
            np.array([bend * math.sin(ang[-1]), 0.0, bend * (1 - math.cos(ang[-1]))])]
    pts = np.array(pts)
    tris = []
    for i in range(rings - 1):
        for j in range(n):
            a, b = i * n + j, i * n + (j + 1) % n
            tris += [[a, b, b + n], [a, b + n, a + n]]
    lo, hi, last = len(pts) - 2, len(pts) - 1, (rings - 1) * n
    for j in range(n):
        tris.append([lo, (j + 1) % n, j])
        tris.append([hi, last + j, last + (j + 1) % n])
    tris = np.array(tris)
    if shell_volume(pts, tris) < 0:  # frame handedness: make it outward
        tris = tris[:, [0, 2, 1]]
    return pts, tris


def box(lo, hi):
    lo, hi = np.array(lo, float), np.array(hi, float)
    p = np.array([[x, y, z] for z in (lo[2], hi[2]) for y in (lo[1], hi[1]) for x in (lo[0], hi[0])])
    t = [[0, 2, 3], [0, 3, 1], [4, 5, 7], [4, 7, 6], [0, 1, 5], [0, 5, 4],
         [2, 6, 7], [2, 7, 3], [0, 4, 6], [0, 6, 2], [1, 3, 7], [1, 7, 5]]
    return p, np.array(t)


def shell_volume(pts, tris):
    a, b, c = pts[tris[:, 0]], pts[tris[:, 1]], pts[tris[:, 2]]
    return float(np.einsum("ij,ij->i", a, np.cross(b, c)).sum() / 6.0)


def tet_volumes(points, tets):
    a, b, c, d = (points[tets[:, i]] for i in range(4))
    return np.einsum("ij,ij->i", np.cross(b - a, c - a), d - a) / 6.0


def inside(point, shell):
    return abs(tetfill._solid_angle_winding(point, *shell)) > 0.5


# ------------------------------------------------------- tetgen: one shell
def test_capped_tube_volume_orientation_and_boundary(tetgen_pkg):
    r, L = 5.0, 40.0
    shell = tube(r, 0, L, n=32, rings=20)
    f = fill_shells([shell], 3.0, engine="tetgen", site="lumen")
    vol = tet_volumes(f.points, f.tets)
    assert vol.min() > 0
    assert vol.sum() == pytest.approx(shell_volume(*shell), rel=1e-9)
    assert vol.sum() == pytest.approx(math.pi * r * r * L, rel=0.01)
    # every input triangle exactly once as a boundary face, marker = shell index
    assert len(f.boundary_faces) == len(shell[1])
    got = {tuple(sorted(t)): m for t, m in zip(f.boundary_faces.tolist(), f.boundary_markers.tolist())}
    assert len(got) == len(shell[1])
    want = {tuple(sorted(t)) for t in shell[1].tolist()}
    assert set(got) == want
    assert set(got.values()) == {1}
    assert f.opt_note.startswith("tetgen ") and "-pYq1.4a" in f.opt_note and f.opt_note.endswith(" tets)")
    assert str(len(f.tets)) in f.opt_note
    assert np.allclose(f.points[: len(shell[0])], shell[0])  # input points first


def test_per_shell_markers_are_offset(tetgen_pkg):
    pts, tris = tube(5.0, 0, 30, n=24, rings=10)
    marks = np.zeros(len(tris), int)
    marks[-48:-24] = 1  # the two caps (24 fan triangles each)
    marks[-24:] = 2
    f = fill_shells([(pts, tris)], 4.0, engine="tetgen", site="wall", markers=[marks])
    got = {tuple(sorted(t)): m for t, m in zip(f.boundary_faces.tolist(), f.boundary_markers.tolist())}
    for t, m in zip(tris.tolist(), marks.tolist()):
        assert got[tuple(sorted(t))] == 10 + m
    assert (f.boundary_markers != 0).all()


# ------------------------------------------------------- tetgen: nested
def _nested_checks(f, outer, inner):
    vol = tet_volumes(f.points, f.tets)
    assert vol.min() > 0
    assert vol.sum() == pytest.approx(shell_volume(*outer) - shell_volume(*inner), rel=1e-9)
    cent = f.points[f.tets].mean(axis=1)
    assert not any(inside(c, inner) for c in cent[:: max(1, len(cent) // 400)])
    # a tet centroid inside the inner shell would be inside its bounding box
    # first; the full check on every tet in the inner box:
    lo, hi = inner[0].min(axis=0), inner[0].max(axis=0)
    box_idx = np.where(np.all((cent > lo) & (cent < hi), axis=1))[0]
    assert not any(inside(cent[i], inner) for i in box_idx)
    assert len(f.boundary_faces) == len(outer[1]) + len(inner[1])
    assert set(np.unique(f.boundary_markers)) == {1, 2}


def test_nested_shells_explicit_hole(tetgen_pkg):
    outer, inner = tube(8.0, 0, 40, n=32, rings=16), tube(5.0, 5, 35, n=32, rings=16)
    f = fill_shells([outer, inner], 3.0, holes=[(0, 0, 20)], engine="tetgen", site="thrombus")
    _nested_checks(f, outer, inner)


def test_nested_shells_computed_hole(tetgen_pkg):
    outer, inner = tube(8.0, 0, 40, n=32, rings=16), tube(5.0, 5, 35, n=32, rings=16)
    f = fill_shells([outer, inner], 3.0, engine="tetgen", site="thrombus")
    _nested_checks(f, outer, inner)


def test_concave_cavity_dumbbell_computed_hole(tetgen_pkg):
    zs = np.linspace(10, 50, 33)
    radii = 1.5 + 4.5 * (np.abs(zs - 30) / 20.0) ** 2  # waist r 1.5, bells r 6
    inner = revolved(zs, radii, n=24)
    outer = tube(15.0, 0, 60, n=24, rings=10)
    p = interior_point(*inner)
    assert inside(p, inner)
    f = fill_shells([outer, inner], 3.5, engine="tetgen", site="thrombus")
    vol = tet_volumes(f.points, f.tets)
    assert vol.min() > 0
    assert vol.sum() == pytest.approx(shell_volume(*outer) - shell_volume(*inner), rel=1e-9)


@pytest.mark.parametrize("shell", [bent_tube(), tube(2.0, 0, 80, n=12, rings=4)], ids=["bent", "thin"])
def test_interior_point_is_inside_concave_and_thin(shell):
    p = interior_point(*shell)
    assert inside(p, shell)
    # independent check with VTK
    import pyvista as pv

    pd = pv.PolyData(shell[0], np.column_stack([np.full(len(shell[1]), 3), shell[1]]).ravel())
    chk = pv.PolyData(p.reshape(1, 3)).select_interior_points(pd, check_surface=False)
    assert bool(chk["selected_points"][0])


def test_bent_tube_is_concave_centroid_outside():
    shell = bent_tube()
    assert not inside(shell[0].mean(axis=0), shell)  # the trap the method avoids


def test_bent_cavity_filled_with_computed_hole(tetgen_pkg):
    inner = bent_tube(r=3.0, bend=20.0)
    lo, hi = inner[0].min(axis=0) - 8, inner[0].max(axis=0) + 8
    outer = box(lo, hi)
    f = fill_shells([outer, inner], 3.0, engine="tetgen", site="thrombus")
    vol = tet_volumes(f.points, f.tets)
    assert vol.min() > 0
    assert vol.sum() == pytest.approx(shell_volume(*outer) - shell_volume(*inner), rel=1e-9)


# ------------------------------------------------------- tetgen: failures
def test_self_intersecting_shell_is_a_loud_error(tetgen_pkg):
    p1, t1 = box((0, 0, 0), (10, 10, 10))
    p2, t2 = box((5, 5, 5), (15, 15, 15))
    shell = (np.vstack([p1, p2]), np.vstack([t1, t2 + 8]))
    with pytest.raises(RuntimeError, match=r"^TetGen") as e:
        fill_shells([shell], 3.0, engine="tetgen", site="lumen")
    # TetGen either raises or segfaults on this input, varying run to run;
    # both paths must name the self-intersection
    assert "self-intersect" in str(e.value)


def test_timeout_is_a_loud_error(tetgen_pkg, monkeypatch, tmp_path):
    # record the child the parent spawned, and keep the temp dirs in a known place
    spawned = []
    orig = subprocess.Popen

    def recording(cmd, **kw):
        proc = orig(cmd, **kw)
        spawned.append(proc)
        return proc

    monkeypatch.setattr(tetfill.subprocess, "Popen", recording)
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    t0 = time.time()
    with pytest.raises(RuntimeError, match=r"^TetGen") as e:
        fill_shells([tube(5.0, 0, 20)], 3.0, engine="tetgen", site="lumen", timeout=0.01)
    assert "timeout" in str(e.value) and "0.01" in str(e.value)
    assert time.time() - t0 < 20
    # the child and its process group are gone, and the temp dir was removed
    assert len(spawned) == 1
    proc = spawned[0]
    assert proc.poll() is not None, "the TetGen child was left running after the timeout"
    with pytest.raises(ProcessLookupError):
        os.killpg(proc.pid, 0)  # the child leads its own session / group
    assert not list(tmp_path.glob("digitwin_tetgen_*")), "temp dir left behind"


def test_crash_without_result_is_reported(tetgen_pkg, monkeypatch):
    # the child dies before writing any result: a loud "TetGen crashed" error
    orig = subprocess.Popen

    def dead_child(cmd, **kw):
        cmd = list(cmd)
        cmd[cmd.index("-c") + 1] = "import os; os._exit(7)"
        return orig(cmd, **kw)

    monkeypatch.setattr(tetfill.subprocess, "Popen", dead_child)
    with pytest.raises(RuntimeError, match=r"^TetGen crashed.*code 7"):
        fill_shells([tube(5.0, 0, 20)], 3.0, engine="tetgen", site="lumen")


def test_unknown_option_is_rejected(tetgen_pkg):
    with pytest.raises(TypeError):
        fill_shells([tube(5.0, 0, 20)], 3.0, engine="tetgen", site="lumen", bogus=1)


def test_engine_name_is_case_insensitive(tetgen_pkg):
    f = fill_shells([tube(5.0, 0, 20, n=16, rings=6)], 4.0, engine="TetGen", site="lumen")
    assert len(f.tets) > 0 and f.boundary_faces is not None


# ------------------------------------------------- dispatch, no package needed
def test_unknown_engine_is_value_error():
    with pytest.raises(ValueError, match="unknown tet engine"):
        fill_shells([tube(5.0, 0, 20)], 3.0, engine="netgen", site="lumen")


def test_missing_package_is_a_loud_error(monkeypatch):
    monkeypatch.setitem(sys.modules, "tetgen", None)  # "import tetgen" now fails
    for engine in ("tetgen", "TetGen"):  # also proves the case-insensitive dispatch
        with pytest.raises(RuntimeError) as e:
            fill_shells([tube(5.0, 0, 20)], 3.0, engine=engine, site="lumen")
        assert str(e.value) == (
            "TetGen is not installed in this environment: "
            "~/miniconda3/envs/digitwin/bin/pip install tetgen (AGPL v3: private use only)"
        )


def test_missing_package_inside_the_child_is_loud_too(monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, "tetgen", None)
    pts, tris = tube(5.0, 0, 20)
    np.savez(tmp_path / "in.npz", points=pts, tris=tris, markers=np.ones(len(tris), np.int32),
             holes=np.zeros((0, 3)), switches=np.array("pYq1.4a1Q"))
    tetfill._tetgen_child_run(str(tmp_path / "in.npz"), str(tmp_path / "out.npz"))
    with np.load(tmp_path / "out.npz") as data:
        assert "not installed" in str(data["error"])


def test_module_skips_cleanly_without_the_package(tmp_path):
    if os.environ.get("DIGITWIN_TETFILL_SKIPPROOF"):
        pytest.skip("this is the nested run of the skip proof")
    shim = tmp_path / "shim"
    shim.mkdir()
    (shim / "sitecustomize.py").write_text("import sys\nsys.modules['tetgen'] = None\n")
    env = dict(os.environ, DIGITWIN_TETFILL_SKIPPROOF="1",
               PYTHONPATH=os.pathsep.join([str(shim), str(REPO / "src")]))
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-rs",
         str(Path(__file__).resolve())],
        cwd=REPO, env=env, capture_output=True, text=True, timeout=600,
    )
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out
    assert "skipped" in out and "failed" not in out and "error" not in out.lower().replace("errors", ""), out
    assert "tetgen" in out.lower()


def test_child_exception_exits_nonzero_with_traceback_in_the_error(tetgen_pkg, monkeypatch):
    # an exception outside the guarded TetGen call (here: an unreadable input
    # file) must not exit 0: the parent reports the child's traceback tail
    orig = subprocess.Popen

    def broken_child(cmd, **kw):
        cmd = list(cmd)
        i = cmd.index("-c") + 1
        cmd[i] = cmd[i].replace("_tetgen_child_main(*sys.argv[1:3])",
                                "_tetgen_child_main('/nonexistent/in.npz', sys.argv[2])")
        return orig(cmd, **kw)

    monkeypatch.setattr(tetfill.subprocess, "Popen", broken_child)
    with pytest.raises(RuntimeError, match=r"^TetGen crashed.*code 1") as e:
        fill_shells([tube(5.0, 0, 20)], 3.0, engine="tetgen", site="lumen")
    assert "FileNotFoundError" in str(e.value)

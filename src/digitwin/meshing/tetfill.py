"""One entry for filling closed triangle shells with linear tetrahedra.

Why (P4 / D119): the three tet fills of the project (the lumen fill, the
thrombus fill and the tet wall backend) each carried their own copy of the
gmsh session. ``fill_shells`` is the single entry for them, so a second
engine (TetGen) can be added behind one switch instead of in three places.
gmsh is the default engine and the only one wired so far.

The gmsh branch is the code of the three sites MOVED here, parametrised per
site (feed order, entity layout, MeshSizeMin, optimizer ladder, error wording)
and nothing else: statement order and option values are those of the
original sites, so every mesh, count and message is unchanged. Each site keeps
its own execution mode: the lumen and wall fills run in-process; the thrombus
fill runs inside the 60 s child of ``digitwin.solve.ilt._run_gmsh``, which calls
this module from the child.

What stays in the sites (engine independent, unchanged): building the
pyvista grid, the winding check (the lumen and wall flip nodes 3 and 4, the
thrombus flips nodes 2 and 3 in ``ilt._orient``), and the boundary labelling.
``fill_shells`` returns the raw fill: all nodes the engine produced and the
tets as 0-based indices into them.
"""

from __future__ import annotations

import math
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

import numpy as np

ENGINES = ("gmsh", "tetgen")
SITES = ("lumen", "thrombus", "wall")

_NEEDS_GMSH = {
    "lumen": "the lumen tet fallback needs gmsh — install with: pip install 'digitwin[meshing]'",
    "wall": "the 'tet' backend needs gmsh — install with: pip install 'digitwin[meshing]'",
    "thrombus": "the thrombus mesh needs gmsh — install with: pip install 'digitwin[meshing]'",
}


@dataclass
class TetFill:
    """The raw result of a fill: ``points`` (n, 3) float, ``tets`` (m, 4)
    0-based int indices into ``points``, ``opt_note`` the optimizer note
    ('' for the thrombus, which has none).

    ``boundary_faces`` (k, 3) 0-based indices into ``points`` and
    ``boundary_markers`` (k,) int are set by the TetGen engine only (with -Y
    the boundary faces are exactly the input triangles, each carrying the
    marker it was given); the gmsh engine leaves them None."""

    points: np.ndarray
    tets: np.ndarray
    opt_note: str = ""
    boundary_faces: np.ndarray | None = None
    boundary_markers: np.ndarray | None = None


def _gmsh_module(site: str):
    """Import gmsh lazily; a missing gmsh is the site's own RuntimeError."""
    try:
        import gmsh
    except ImportError as exc:
        raise RuntimeError(_NEEDS_GMSH[site]) from exc
    return gmsh


def require_gmsh(site: str) -> None:
    """Raise the site's 'needs gmsh' RuntimeError now (the wall backend
    checks before it builds its lattice)."""
    _gmsh_module(site)


def fill_shells(shells, size, holes=(), engine="gmsh", *, site, **site_options):
    """Fill closed triangle shell(s) with tets at target edge ``size``.

    ``shells``: a sequence of ``(points (n, 3), tris (m, 3) 0-based)`` pairs.
    One pair for the lumen and the wall (the wall may carry per-triangle
    ``labels`` in ``site_options``), two pairs, outer then inner, for the
    thrombus. ``holes``: one interior point per cavity; the gmsh engine
    ignores it (each site's entity layout defines the cavity). ``site`` is
    'lumen', 'thrombus' or 'wall' and selects the per-site parametrisation.
    ``site_options`` (gmsh): lumen ``optimize``, ``remesh``; wall
    ``optimize``, ``labels``, ``closed_s``; thrombus none. TetGen: ``timeout``
    (s, default 120) and ``markers`` (one per-triangle int array, or None, per
    shell); the gmsh-only options are accepted and ignored. The TetGen engine
    needs ``holes`` for a cavity, except the thrombus (and the closed-S wall,
    given ``labels``), where one interior point of the cavity is computed.

    Returns a ``TetFill``. Failures keep the type and text of the original
    site (a failed lumen or thrombus fill is a RuntimeError)."""
    key = str(engine).lower()
    if key not in ENGINES:
        raise ValueError(
            f"unknown tet engine {engine!r}; choose one of: {', '.join(ENGINES)}"
        )
    if site not in SITES:
        raise ValueError(f"unknown tet-fill site {site!r}; choose one of: {', '.join(SITES)}")
    if key == "tetgen":
        return _fill_tetgen(site, shells, size, holes, **site_options)
    return _fill_gmsh(site, shells, size, **site_options)


def _fill_gmsh(site, shells, size, *, optimize=True, remesh=False, labels=None, closed_s=False):
    gmsh = _gmsh_module(site)

    gmsh.initialize()
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        if site == "lumen":
            shell_pts, shell_tris = shells[0]
            mesh_size = size
            gmsh.model.add("digitwin_lumen")
            node_tags = np.arange(1, len(shell_pts) + 1)
            gmsh.model.addDiscreteEntity(2, 1)
            gmsh.model.mesh.addNodes(2, 1, node_tags, shell_pts.ravel())
            gmsh.model.mesh.addElementsByType(1, 2, [], (shell_tris + 1).ravel())
            if remesh:
                gmsh.model.mesh.classifySurfaces(math.radians(40.0), True, True, math.pi)
                gmsh.model.mesh.createGeometry()
                surfaces = [tag for _, tag in gmsh.model.getEntities(2)]
                loop = gmsh.model.geo.addSurfaceLoop(surfaces)
                gmsh.option.setNumber("Mesh.MeshSizeMin", 0.5 * mesh_size)
            else:
                loop = gmsh.model.geo.addSurfaceLoop([1])
            gmsh.model.geo.addVolume([loop])
            gmsh.model.geo.synchronize()
            gmsh.option.setNumber("Mesh.MeshSizeMax", mesh_size)
        elif site == "wall":
            shell_pts, shell_tris = shells[0]
            patch_labels = labels
            gmsh.model.add("digitwin_wall")
            node_tags = np.arange(1, len(shell_pts) + 1)
            if closed_s:
                # Closed-loop wall (V9): the boundary is TWO disjoint closed
                # surfaces (outer torus + inner torus). One surface loop would
                # make gmsh fill the lumen too — the volume needs the outer
                # loop plus the inner loop as a cavity.
                remapped_labels = patch_labels  # 0=inner, 1=outer only
                gmsh.model.addDiscreteEntity(2, 1)
                gmsh.model.addDiscreteEntity(2, 2)
                gmsh.model.mesh.addNodes(2, 1, node_tags, shell_pts.ravel())
                outer_tris = shell_tris[remapped_labels == 1]
                inner_tris = shell_tris[remapped_labels == 0]
                gmsh.model.mesh.addElementsByType(1, 2, [], (outer_tris + 1).ravel())
                gmsh.model.mesh.addElementsByType(2, 2, [], (inner_tris + 1).ravel())
                loop_outer = gmsh.model.geo.addSurfaceLoop([1])
                loop_inner = gmsh.model.geo.addSurfaceLoop([2])
                gmsh.model.geo.addVolume([loop_outer, loop_inner])
            else:
                gmsh.model.addDiscreteEntity(2, 1)
                gmsh.model.mesh.addNodes(2, 1, node_tags, shell_pts.ravel())
                gmsh.model.mesh.addElementsByType(1, 2, [], (shell_tris + 1).ravel())
                loop = gmsh.model.geo.addSurfaceLoop([1])
                gmsh.model.geo.addVolume([loop])
            gmsh.model.geo.synchronize()
            gmsh.option.setNumber("Mesh.MeshSizeMax", size)
        else:  # thrombus: inside(outer) minus inside(inner)
            (outer_points, outer_tris), (inner_points, inner_tris) = shells
            gmsh.model.add("digitwin_ilt")
            offset = 0
            for tag, pts, tris in ((1, outer_points, outer_tris), (2, inner_points, inner_tris)):
                gmsh.model.addDiscreteEntity(2, tag)
                gmsh.model.mesh.addNodes(2, tag, np.arange(offset + 1, offset + len(pts) + 1), pts.ravel())
                gmsh.model.mesh.addElementsByType(tag, 2, [], (tris + offset + 1).ravel())
                offset += len(pts)
            loops = [gmsh.model.geo.addSurfaceLoop([1]), gmsh.model.geo.addSurfaceLoop([2])]
            gmsh.model.geo.addVolume(loops)
            gmsh.model.geo.synchronize()
            gmsh.option.setNumber("Mesh.MeshSizeMax", size)
            gmsh.option.setNumber("Mesh.MeshSizeMin", 0.5 * size)

        if site == "lumen":
            try:
                gmsh.model.mesh.generate(3)
            except Exception as gmsh_exc:  # gmsh raises a bare Exception
                # D85: fail loudly with a readable cause instead of a raw gmsh
                # traceback (seen on a real eccentric sac: the station rings of a
                # folding sweep overlap, so the ring shell self-intersects)
                raise RuntimeError(
                    f"lumen tet fill failed — gmsh: {gmsh_exc}. The station-ring shell "
                    f"is invalid, typically because the rings overlap where the "
                    f"centreline bends tighter than the section radius"
                ) from None
        elif site == "wall":
            gmsh.model.mesh.generate(3)
        else:
            try:
                gmsh.model.mesh.generate(3)
            except Exception as exc:  # gmsh raises a bare Exception
                raise RuntimeError(
                    f"thrombus tet fill failed — gmsh: {exc}. The lumen probably touches or "
                    f"crosses the sac surface"
                ) from None

        if site == "thrombus":
            try:
                gmsh.model.mesh.optimize("Netgen")
            except Exception:  # pragma: no cover - optimization is best effort
                pass
            opt_note = ""
        else:
            # BACKLOG #2: near-degenerate positive slivers. Netgen is the
            # stronger optimizer but may fail on discrete shells — fall back
            # to gmsh's default tet optimizer, loudly.
            opt_note = "optimization disabled (optimize=False)"
            if optimize:
                try:
                    gmsh.model.mesh.optimize("Netgen")
                    opt_note = "Netgen mesh optimization applied"
                except Exception as netgen_exc:
                    try:
                        gmsh.model.mesh.optimize("")
                        opt_note = (
                            f"Netgen optimization failed ({netgen_exc}); "
                            "default gmsh optimizer applied"
                        )
                    except Exception as default_exc:
                        opt_note = (
                            f"mesh optimization skipped (Netgen: {netgen_exc}; "
                            f"default: {default_exc})"
                        )

        tags, coords, _ = gmsh.model.mesh.getNodes()
        points = np.asarray(coords, dtype=float).reshape(-1, 3)
        tag_to_idx = np.zeros(int(tags.max()) + 1, dtype=np.int64)
        tag_to_idx[np.asarray(tags, dtype=np.int64)] = np.arange(len(tags))
        _, tet_nodes = gmsh.model.mesh.getElementsByType(4)
        tets = tag_to_idx[np.asarray(tet_nodes, dtype=np.int64)].reshape(-1, 4)
    finally:
        gmsh.finalize()

    if len(tets) == 0:
        if site == "lumen":
            raise ValueError("gmsh produced no tetrahedra for the lumen shell")
        if site == "wall":
            raise ValueError("gmsh produced no tetrahedra; the wall shell may be invalid")
    return TetFill(points=points, tets=tets, opt_note=opt_note)


# ---------------------------------------------------------------------------
# TetGen engine (private use only: TetGen is AGPL v3)
# ---------------------------------------------------------------------------
TETGEN_TIMEOUT_S = 120.0
_TETGEN_OPTS = {"timeout", "markers", "optimize", "remesh", "labels", "closed_s"}
_TETGEN_MISSING = (
    "TetGen is not installed in this environment: "
    "~/miniconda3/envs/digitwin/bin/pip install tetgen (AGPL v3: private use only)"
)


def _solid_angle_winding(point, points, tris) -> float:
    """Generalised winding number of ``point`` w.r.t. a closed triangle
    surface (sum of signed solid angles / 4 pi): +-1 inside, 0 outside,
    whichever way the triangles are wound."""
    v = points[tris] - np.asarray(point, dtype=float)
    a, b, c = v[:, 0], v[:, 1], v[:, 2]
    la, lb, lc = (np.linalg.norm(x, axis=1) for x in (a, b, c))
    num = np.einsum("ij,ij->i", a, np.cross(b, c))
    den = (la * lb * lc + np.einsum("ij,ij->i", a, b) * lc
           + np.einsum("ij,ij->i", a, c) * lb + np.einsum("ij,ij->i", b, c) * la)
    return float(2.0 * np.arctan2(num, den).sum() / (4.0 * math.pi))


def _first_hit(origin, direction, points, tris, skip: int) -> float | None:
    """Distance along the ray to the nearest triangle other than ``skip``
    (Moller-Trumbore), or None."""
    p0, p1, p2 = (points[tris[:, i]] for i in range(3))
    e1, e2 = p1 - p0, p2 - p0
    h = np.cross(direction, e2)
    det = np.einsum("ij,ij->i", e1, h)
    ok = np.abs(det) > 1e-14 * max(1.0, float(np.abs(det).max()))
    inv = np.where(ok, 1.0 / np.where(ok, det, 1.0), 0.0)
    s = origin - p0
    u = np.einsum("ij,ij->i", s, h) * inv
    q = np.cross(s, e1)
    w = np.einsum("ij,j->i", q, direction) * inv
    t = np.einsum("ij,ij->i", e2, q) * inv
    hit = ok & (u >= 0) & (w >= 0) & (u + w <= 1) & (t > 1e-9)
    hit[skip] = False
    return float(t[hit].min()) if hit.any() else None


def interior_point(points, tris, *, tries: int = 64) -> np.ndarray:
    """One point strictly inside the closed triangle shell, safe for a
    concave shell (the centroid is not). From a triangle centroid, shoot a
    ray along the triangle's normal, either way; the midpoint of the chord to
    the first other triangle hit lies inside the solid and as far from the
    surface as that chord allows. Each candidate is verified with the
    winding number; of the verified candidates the one with the longest chord
    wins. Raises RuntimeError if none of ``tries`` triangles verifies."""
    points = np.asarray(points, dtype=float)
    tris = np.asarray(tris, dtype=np.int64)
    corner = points[tris]
    cen = corner.mean(axis=1)
    nrm = np.cross(corner[:, 1] - corner[:, 0], corner[:, 2] - corner[:, 0])
    area2 = np.linalg.norm(nrm, axis=1)
    best, best_half, found = None, 0.0, 0
    for i in np.unique(np.linspace(0, len(tris) - 1, min(len(tris), int(tries))).astype(int)):
        if area2[i] <= 0:
            continue
        direction = nrm[i] / area2[i]
        for sign in (1.0, -1.0):
            t = _first_hit(cen[i], sign * direction, points, tris, int(i))
            if t is None:
                continue
            cand = cen[i] + sign * direction * (0.5 * t)
            if abs(_solid_angle_winding(cand, points, tris)) > 0.5:
                found += 1
                if 0.5 * t > best_half:
                    best, best_half = cand, 0.5 * t
        if found >= 6:
            break
    if best is None:
        raise RuntimeError(
            "TetGen: could not find an interior point of the cavity shell to use as its "
            f"hole (tried {min(len(tris), int(tries))} triangles); the shell is probably "
            "not a closed, consistent surface"
        )
    return best


def _tetgen_child_run(in_path: str, out_path: str) -> None:
    """Run TetGen on the arrays in ``in_path`` and write the result (or the
    error) to ``out_path``. Separate from ``_tetgen_child_main`` so a test can
    call it in-process."""
    with np.load(in_path) as data:
        args = {k: data[k] for k in data.files}
    try:
        try:
            import tetgen
        except ImportError:
            raise RuntimeError(_TETGEN_MISSING) from None
        import importlib.metadata

        version = importlib.metadata.version("tetgen")
        tg = tetgen.TetGen(args["points"], args["tris"], args["markers"])
        for h in args["holes"]:
            tg.add_hole([float(x) for x in h])
        tg.tetrahedralize(switches=str(args["switches"]))
        out = {
            "points": np.asarray(tg.node, dtype=float),
            "tets": np.asarray(tg.elem, dtype=np.int64),
            "faces": np.asarray(tg.trifaces, dtype=np.int64),
            "fmarkers": np.asarray(tg.triface_markers, dtype=np.int64),
            "version": np.array(version),
        }
    except Exception as exc:
        np.savez(out_path, error=np.array(f"{type(exc).__name__}: {exc}"))
        return
    np.savez(out_path, **out)


def _tetgen_child_main(in_path: str, out_path: str) -> None:
    """Entry point of the guarded TetGen child. TetGen's wrapper can segfault
    at interpreter teardown after a failed run (seen on a self-intersecting
    surface), so exit hard once the result file is written."""
    try:
        _tetgen_child_run(in_path, out_path)
    finally:
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(0)


def _fill_tetgen(site, shells, size, holes=(), **opts):
    import importlib.util

    unknown = set(opts) - _TETGEN_OPTS
    if unknown:
        raise TypeError(f"unexpected options for the tetgen engine: {sorted(unknown)}")
    timeout = float(opts.get("timeout", TETGEN_TIMEOUT_S))
    size = float(size)
    if not (size > 0 and math.isfinite(size)):
        raise ValueError(f"tet size must be a positive number, got {size!r}")
    if not shells:
        raise ValueError("fill_shells needs at least one shell")
    try:
        found = importlib.util.find_spec("tetgen")
    except (ImportError, ValueError):
        found = None
    if found is None:
        raise RuntimeError(_TETGEN_MISSING)

    # one combined surface: shell k's points are offset, its triangles carry
    # the marker (k + 1) * 10 + per-shell marker, else k + 1
    per_shell = list(opts.get("markers") or [])
    per_shell += [None] * (len(shells) - len(per_shell))
    if site == "wall" and per_shell[0] is None and opts.get("labels") is not None:
        per_shell[0] = opts["labels"]
    all_pts, all_tris, all_marks, offset = [], [], [], 0
    for k, (pts, tris) in enumerate(shells):
        pts = np.asarray(pts, dtype=float).reshape(-1, 3)
        tris = np.asarray(tris, dtype=np.int64).reshape(-1, 3)
        if len(tris) == 0 or tris.min() < 0 or tris.max() >= len(pts):
            raise ValueError(f"shell {k}: empty or out-of-range triangle indices")
        if per_shell[k] is None:
            marks = np.full(len(tris), k + 1, dtype=np.int64)
        else:
            m = np.asarray(per_shell[k], dtype=np.int64).ravel()
            if len(m) != len(tris) or (m < 0).any():
                raise ValueError(f"shell {k}: markers must be {len(tris)} non-negative ints")
            marks = (k + 1) * 10 + m
        all_pts.append(pts)
        all_tris.append(tris + offset)
        all_marks.append(marks)
        offset += len(pts)
    points, tris = np.vstack(all_pts), np.vstack(all_tris)
    markers = np.concatenate(all_marks).astype(np.int32)

    hole_pts = np.asarray(holes, dtype=float).reshape(-1, 3)
    if len(hole_pts) == 0:
        cavity = None
        if site == "thrombus" and len(shells) >= 2:
            cavity = shells[1]  # (outer, inner): the inner shell is the cavity
        elif site == "wall" and opts.get("closed_s") and opts.get("labels") is not None:
            lab = np.asarray(opts["labels"])
            cavity = (shells[0][0], np.asarray(shells[0][1])[lab == 0])  # 0 = inner torus
        if cavity is not None:
            hole_pts = interior_point(*cavity).reshape(1, 3)

    vmax = size**3 / (6.0 * math.sqrt(2.0))
    vtxt = f"{vmax:.6g}"
    if "e" in vtxt:
        vtxt = f"{vmax:.12f}"
    switches = f"pYq1.4a{vtxt}Q"

    out = _run_tetgen_child(points, tris, markers, hole_pts, switches, timeout)

    pts_out, tets = out["points"], out["tets"]
    faces, fmark = out["faces"], out["fmarkers"]
    if len(tets) == 0:
        raise RuntimeError("TetGen produced no tetrahedra; the shell(s) may be invalid")
    if len(faces) != len(tris):
        raise RuntimeError(
            f"TetGen returned {len(faces)} boundary faces for {len(tris)} input triangles; "
            "the surface was modified or is not a clean closed shell"
        )
    if (fmark == 0).any():
        raise RuntimeError(
            f"TetGen returned {int((fmark == 0).sum())} boundary faces without a marker"
        )
    a, b, c, d = (pts_out[tets[:, i]] for i in range(4))
    vol = np.einsum("ij,ij->i", np.cross(b - a, c - a), d - a) / 6.0
    if not (vol.min() > 0):
        raise RuntimeError(
            f"TetGen returned {int((vol <= 0).sum())} tets with zero or negative volume "
            f"(min {vol.min():.3g})"
        )
    note = f"tetgen {out['version']} -{switches} ({len(tets)} tets)"
    return TetFill(points=pts_out, tets=tets, opt_note=note,
                   boundary_faces=faces, boundary_markers=fmark)


def _run_tetgen_child(points, tris, markers, holes, switches, timeout) -> dict:
    """Run TetGen in a child process killed after ``timeout`` s (TetGen can
    spin on a degenerate surface, and its wrapper can crash the interpreter)."""
    from digitwin.io.segment import _kill

    src = str(Path(__file__).resolve().parents[2])  # the dir holding digitwin/
    code = ("import sys; sys.path.insert(0, sys.argv[3]); "
            "from digitwin.meshing.tetfill import _tetgen_child_main; "
            "_tetgen_child_main(*sys.argv[1:3])")
    with tempfile.TemporaryDirectory(prefix="digitwin_tetgen_") as tmp:
        in_path, out_path = os.path.join(tmp, "in.npz"), os.path.join(tmp, "out.npz")
        log_path = os.path.join(tmp, "child.log")
        np.savez(in_path, points=points, tris=tris.astype(np.int64), markers=markers,
                 holes=np.asarray(holes, dtype=float).reshape(-1, 3), switches=np.array(switches))
        with open(log_path, "wb") as log:
            proc = subprocess.Popen(
                [sys.executable, "-I", "-c", code, in_path, out_path, src],
                stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, cwd=tmp,
                start_new_session=(os.name == "posix"),
            )
            try:
                rc = proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                raise RuntimeError(
                    f"TetGen did not finish within {timeout:g} s (timeout) and was stopped; "
                    "the surface may be degenerate or the element size too small for it"
                ) from None
            finally:
                _kill(proc)  # no-op once it exited
        if not os.path.exists(out_path):
            lines = [ln for ln in Path(log_path).read_text(errors="replace").splitlines()
                     if ln.strip() and ln.strip() not in ("parsing switches", "success")]
            hint = ""
            if rc < 0 or rc in (134, 139):  # killed by a signal (SEGV / abort)
                # TetGen's usual reaction to an invalid surface: on a
                # self-intersecting input it either raises or segfaults
                hint = (" (TetGen died on a signal, typical for an invalid surface: "
                        "self-intersecting or overlapping shells)")
            raise RuntimeError(f"TetGen crashed: the process exited with code {rc} and no "
                               f"result{hint}" + (": " + " | ".join(lines[-5:]) if lines else ""))
        with np.load(out_path) as data:
            out = {k: data[k] for k in data.files}
    if "error" in out:
        msg = str(out["error"])
        if "not installed" in msg:
            raise RuntimeError(msg.split(": ", 1)[-1] if msg.startswith("RuntimeError: ") else msg)
        if "self-intersect" in msg:
            raise RuntimeError(
                "TetGen rejected the surface: it self-intersects (overlapping or crossing "
                f"shells, or coplanar caps of nested shells) — {msg}"
            )
        raise RuntimeError(f"TetGen failed: {msg}")
    out["version"] = str(out["version"])
    return out

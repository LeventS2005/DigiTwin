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
from dataclasses import dataclass

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
    ('' for the thrombus, which has none)."""

    points: np.ndarray
    tets: np.ndarray
    opt_note: str = ""


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
    ``optimize``, ``labels``, ``closed_s``; thrombus none.

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
        raise NotImplementedError("tet engine 'tetgen' is not wired yet")
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

"""Structured lumen (blood-volume) fill (V47).

Fills the interior of a tubular surface with hexahedra: stations along the
validated centreline (embedded > skeleton, the same choice the wall lattice
makes), per-station lumen rings ray-cast from the centreline, each ring
O-grid-filled in its own frame plane (V26 ``ogrid_disk`` with a PINNED block
topology so every station meshes identically), consecutive disks swept into
hexes. Where the structured fill fails — a section too far from a disk, or a
bend so tight the swept cells fold (the fold number F = r*kappa, probed
session 34/35) — ``method="auto"`` falls back to a gmsh tetrahedral fill of
the SAME station-ring boundary shell, with a loud note naming the fold
number. Boundary groups are ``wall`` / ``inlet`` / ``outlet`` either way.

Probe evidence (D75): swept from the validated centreline, 19 of the 20 real
cardiac arches reach ``inverted == 0`` structured; smoothing is a
do-no-harm-guarded pass (V21 pattern in 3-D) because a plain Laplacian
BREAKS valid meshes and true Winslow cannot fix the fold obstruction.
"""
from __future__ import annotations

import math

import numpy as np
import pyvista as pv

from meshforge.geometry import parallel_transport_frames
from meshforge.meshing.base import BoundaryGroup, WallMesh
from meshforge.quality.volume import volume_quality

_LABELS = {0: "wall", 2: "inlet", 3: "outlet"}


def _station_geometry(surface, centerline, *, n_theta: int, n_axial: int):
    """Stations, frames and ray-cast lumen radii (the wall-lattice sampling).

    Returns (stations, tangents, normals, binormals, radii, arc). Fails loud
    on a ray miss — a centreline that exits the surface cannot anchor ANY
    lumen fill (structured or tet), so there is no fallback from here.
    """
    if centerline.closed:
        raise ValueError(
            "lumen fill supports open centrelines only (V47); a closed-loop "
            "lumen is a solid torus — a different block graph"
        )
    idx = np.unique(
        np.linspace(0, len(centerline.points) - 1, n_axial).round().astype(int)
    )
    stations = centerline.points[idx]
    tangents = centerline.tangents[idx]
    normals, binormals = parallel_transport_frames(tangents, closed=False)
    theta = np.linspace(0.0, 2.0 * math.pi, n_theta, endpoint=False)
    cos_t, sin_t = np.cos(theta), np.sin(theta)
    ray_len = 5.0 * float(centerline.radii.max())
    s_count = len(stations)
    radii = np.empty((s_count, n_theta))
    for i in range(s_count):
        for j in range(n_theta):
            d = cos_t[j] * normals[i] + sin_t[j] * binormals[i]
            hits, _ = surface.ray_trace(
                stations[i], stations[i] + d * ray_len, first_point=True
            )
            if len(hits) == 0:
                raise ValueError(
                    f"lumen ray missed the surface at station {i}, θ index "
                    f"{j}; centerline may exit the surface or ray length is "
                    "too short"
                )
            radii[i, j] = np.linalg.norm(hits - stations[i])
    arc = centerline.arc_lengths[idx]
    return stations, tangents, normals, binormals, radii, arc, cos_t, sin_t


def _fold_numbers(tangents, arc, radii) -> np.ndarray:
    """Per-station fold number F = r_max * kappa (folds where F >= 1)."""
    ds = np.gradient(arc)
    kvec = np.gradient(tangents, axis=0) / ds[:, None]
    kappa = np.linalg.norm(kvec, axis=1)
    return radii.max(axis=1) * kappa


def _fold_note(F: np.ndarray) -> str:
    return (
        f"fold number F=r·κ: max {float(F.max()):.2f}, "
        f"{int((F >= 1.0).sum())} station(s) with F >= 1"
    )


def _station_disks(radii, cos_t, sin_t, *, core_fraction: float):
    """O-grid every station ring with ONE pinned block topology.

    Counts are pinned from the median-perimeter station; a station whose
    section is too far from a disk raises ``ogrid_disk``'s loud ValueError
    (the auto cascade turns that into the tet fallback)."""
    from meshforge.meshing.ogrid import ogrid_disk

    S = radii.shape[0]
    rings2d = [
        np.column_stack([radii[i] * cos_t, radii[i] * sin_t]) for i in range(S)
    ]
    perims = np.array([
        float(np.linalg.norm(np.diff(np.vstack([r, r[:1]]), axis=0), axis=1).sum())
        for r in rings2d
    ])
    ref = int(np.argsort(perims)[S // 2])
    # target ~ ring segment length at the median station -> n_t ~ n_theta/4.
    target = perims[ref] / len(cos_t)
    bm_ref = ogrid_disk([rings2d[ref]], target_size=target,
                        core_fraction=core_fraction)
    # Pin the reference counts: parse them from the V26 note (the mesh's own
    # declared layout — kept in one place there).
    note = bm_ref.notes[0]
    pin_t = int(note.split("n_t ")[1].split(" ")[0])
    pin_r = int(note.split("n_r ")[1].split(" ")[0])
    disks = []
    quads = None
    for i in range(S):
        bm = ogrid_disk([rings2d[i]], target_size=target,
                        core_fraction=core_fraction, n_t=pin_t, n_r=pin_r)
        if quads is None:
            quads, n_node, rim = bm.quads, len(bm.points), bm.edge_groups["outer_rim"]
        elif len(bm.points) != n_node or bm.quads.shape != quads.shape:
            raise ValueError(
                f"O-grid topology drift at station {i} despite pinned counts "
                f"({len(bm.points)} vs {n_node} nodes) — cannot sweep"
            )
        disks.append(bm.points)
    return disks, quads, rim, n_node


def _sweep(stations, normals, binormals, disks, quads, n_node):
    """Lift each 2-D disk into its station plane and sweep hexes."""
    S = len(stations)
    pts = np.vstack([
        stations[i]
        + disks[i][:, 0:1] * normals[i][None, :]
        + disks[i][:, 1:2] * binormals[i][None, :]
        for i in range(S)
    ])
    nq = len(quads)
    hexes = np.empty(((S - 1) * nq, 8), dtype=np.int64)
    for i in range(S - 1):
        b0, b1 = i * n_node, (i + 1) * n_node
        hexes[i * nq:(i + 1) * nq, 0:4] = quads + b0
        hexes[i * nq:(i + 1) * nq, 4:8] = quads + b1
    return pts, hexes


def _hex_grid(pts, hexes) -> pv.UnstructuredGrid:
    cells = np.column_stack(
        [np.full(len(hexes), 8, dtype=np.int64), hexes]
    ).ravel()
    ctypes = np.full(len(hexes), pv.CellType.HEXAHEDRON, dtype=np.uint8)
    grid = pv.UnstructuredGrid(cells, ctypes, pts)
    grid.cell_data["region"] = np.ones(grid.n_cells, dtype=np.int32)
    return grid


def _ogrid_boundary(quads, rim, n_node, S) -> dict[str, BoundaryGroup]:
    """wall (swept rim quads) / inlet (station-0 disk) / outlet (last disk)."""
    nq = len(quads)
    edge_quad = {}
    for qi, q in enumerate(quads):
        for k in range(4):
            edge_quad[frozenset((int(q[k]), int(q[(k + 1) % 4])))] = qi
    wall_cells, wall_faces = [], []
    for i in range(S - 1):
        b0, b1 = i * n_node, (i + 1) * n_node
        for a, b in rim:
            qi = edge_quad[frozenset((int(a), int(b)))]
            wall_cells.append(i * nq + qi)
            wall_faces.append([b0 + a, b0 + b, b1 + b, b1 + a])
    b_last = (S - 1) * n_node
    inlet_faces = quads[:, ::-1].copy()  # outward = -tangent
    outlet_faces = quads + b_last
    return {
        "wall": BoundaryGroup(
            "wall",
            cell_ids=np.asarray(wall_cells, dtype=np.int64),
            face_nodes=np.asarray(wall_faces, dtype=np.int64),
        ),
        "inlet": BoundaryGroup(
            "inlet",
            cell_ids=np.arange(nq, dtype=np.int64),
            face_nodes=np.asarray(inlet_faces, dtype=np.int64),
        ),
        "outlet": BoundaryGroup(
            "outlet",
            cell_ids=np.arange((S - 2) * nq, (S - 1) * nq, dtype=np.int64),
            face_nodes=np.asarray(outlet_faces, dtype=np.int64),
        ),
    }


# --- guarded smoothing (V21 do-no-harm, in 3-D) -----------------------------


def _boundary_nodes(quads, rim, n_node, S) -> np.ndarray:
    """Node ids on the lumen boundary: the swept rim + both end disks."""
    mask = np.zeros(S * n_node, dtype=bool)
    rim_nodes = np.unique(rim)
    for i in range(S):
        mask[i * n_node + rim_nodes] = True
    mask[:n_node] = True
    mask[(S - 1) * n_node:] = True
    return mask


def guarded_smooth(
    pts: np.ndarray,
    hexes: np.ndarray,
    boundary: np.ndarray,
    *,
    iterations: int,
) -> tuple[np.ndarray, int]:
    """Do-no-harm smoothing: per interior node, a Laplacian (edge-neighbour
    mean) proposal is accepted — full step, else half, else quarter — only
    if the min VTK scaled Jacobian over the node's incident hexes does not
    drop. The guard uses THE reported metric (a cheaper corner-Jacobian
    proxy let a real 0.22 -> 0.19 VTK regression through on VAM775, D75).
    Nodes are greedily coloured so no two simultaneously-moved nodes share
    a hex — within a colour class every node's guard depends only on its
    own position, so the class moves as one vectorized batch and each
    accepted move can only RAISE its local min: global sj_min and
    inverted-0 can never regress (the V21 guarantee, probed on the real
    arches). Returns (new points, accepted move count)."""
    interior = np.nonzero(~boundary)[0]
    if len(interior) == 0 or iterations <= 0:
        return pts, 0
    n = len(pts)
    edges = ((0, 1), (1, 2), (2, 3), (3, 0), (4, 5), (5, 6), (6, 7), (7, 4),
             (0, 4), (1, 5), (2, 6), (3, 7))
    nbr: list[set[int]] = [set() for _ in range(n)]
    node_hex: list[list[int]] = [[] for _ in range(n)]
    for hi, h in enumerate(hexes):
        for a, b in edges:
            nbr[h[a]].add(int(h[b]))
            nbr[h[b]].add(int(h[a]))
        for v in h:
            node_hex[int(v)].append(hi)
    # Greedy colouring of the shares-a-hex conflict graph.
    hex_nodes = [set(map(int, h)) for h in hexes]
    color: dict[int, int] = {}
    for v in interior:
        used = set()
        for hi in node_hex[v]:
            for w in hex_nodes[hi]:
                if w != v and w in color:
                    used.add(color[w])
        c = 0
        while c in used:
            c += 1
        color[int(v)] = c
    classes: dict[int, list[int]] = {}
    for v, c in color.items():
        classes.setdefault(c, []).append(v)
    # Per-class CSR: neighbour lists (for the Laplacian proposal) and
    # incident-hex lists (for the guard). Within a class no hex repeats.
    class_data = []
    for cls in classes.values():
        nodes = np.asarray(sorted(cls), dtype=np.int64)
        nb_lists = [np.fromiter(sorted(nbr[v]), dtype=np.int64) for v in nodes]
        nptr = np.concatenate([[0], np.cumsum([len(x) for x in nb_lists])])
        hs_lists = [np.asarray(node_hex[v], dtype=np.int64) for v in nodes]
        hptr = np.concatenate([[0], np.cumsum([len(x) for x in hs_lists])])
        class_data.append(
            (nodes, np.concatenate(nb_lists), nptr,
             np.concatenate(hs_lists), hptr)
        )
    pts = np.array(pts, dtype=float)
    h_scale = float(
        np.mean(np.linalg.norm(pts[hexes[:, 1]] - pts[hexes[:, 0]], axis=1))
    )
    # Guard evaluations run on THE reported metric: one scratch grid whose
    # points are swapped per evaluation (VTK recomputes quality on call).
    scratch = _hex_grid(pts, hexes)

    def vtk_sj(p: np.ndarray) -> np.ndarray:
        scratch.points = p
        sj = np.asarray(
            scratch.cell_quality(["scaled_jacobian"])["scaled_jacobian"],
            dtype=float,
        )
        return np.where(np.isfinite(sj), sj, -np.inf)

    accepted = 0
    for _ in range(iterations):
        moved = False
        for nodes, nb, nptr, hs, hptr in class_data:
            counts = np.diff(nptr)[:, None]
            prop = np.add.reduceat(pts[nb], nptr[:-1], axis=0) / counts
            old = pts[nodes].copy()
            before = np.minimum.reduceat(vtk_sj(pts)[hs], hptr[:-1])
            pending = np.ones(len(nodes), dtype=bool)
            best = old.copy()
            for t in (1.0, 0.5, 0.25):
                idx = np.nonzero(pending)[0]
                if len(idx) == 0:
                    break
                pts[nodes[idx]] = old[idx] + t * (prop[idx] - old[idx])
                after = np.minimum.reduceat(vtk_sj(pts)[hs], hptr[:-1])
                ok = pending & (after >= before - 1e-12)
                best[ok] = pts[nodes[ok]]
                pending &= ~ok
            pts[nodes] = best  # rejected nodes revert to their old position
            disp = np.linalg.norm(best - old, axis=1)
            real = disp > 1e-9 * h_scale
            accepted += int(real.sum())
            if real.any():
                moved = True
        if not moved:
            break
    return pts, accepted


# --- tet fallback on the same station-ring shell -----------------------------


def _ring_shell(stations, normals, binormals, radii, cos_t, sin_t):
    """Watertight labelled boundary shell of the lumen straight from the
    ray-cast rings (INDEPENDENT of the O-grid — a station whose section
    defeats the O-grid still has a valid star-shaped ring): lateral ring
    quads split to triangles (label 0 wall, outward radial), end caps as
    centre fans (labels 2 inlet / 3 outlet, outward ∓ tangent)."""
    S, T = radii.shape
    ring_pts = np.concatenate([
        stations[i]
        + (radii[i] * cos_t)[:, None] * normals[i][None, :]
        + (radii[i] * sin_t)[:, None] * binormals[i][None, :]
        for i in range(S)
    ])
    pts = np.vstack([ring_pts, stations[0], stations[S - 1]])
    c0, c1 = S * T, S * T + 1
    tris: list[tuple[int, int, int]] = []
    labels: list[int] = []
    for i in range(S - 1):
        b0, b1 = i * T, (i + 1) * T
        for j in range(T):
            j1 = (j + 1) % T
            tris.append((b0 + j, b0 + j1, b1 + j1))
            tris.append((b0 + j, b1 + j1, b1 + j))
            labels.extend((0, 0))
    b_last = (S - 1) * T
    for j in range(T):
        j1 = (j + 1) % T
        tris.append((c0, j1, j))  # inlet fan, outward -tangent
        labels.append(2)
        tris.append((c1, b_last + j, b_last + j1))  # outlet fan, outward +t
        labels.append(3)
    return pts, np.asarray(tris, dtype=np.int64), np.asarray(labels, dtype=np.int64)


def _tet_fill(shell_pts, shell_tris, patch_labels, *, mesh_size, optimize=True):
    """gmsh-fill the labelled shell; classify boundary faces by nearest shell
    patch (the TetBackend pattern with wall/inlet/outlet labels)."""
    try:
        import gmsh
    except ImportError as exc:
        raise RuntimeError(
            "the lumen tet fallback needs gmsh — install with: "
            "pip install 'meshforge[meshing]'"
        ) from exc
    from scipy.spatial import cKDTree

    gmsh.initialize()
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        gmsh.model.add("meshforge_lumen")
        node_tags = np.arange(1, len(shell_pts) + 1)
        gmsh.model.addDiscreteEntity(2, 1)
        gmsh.model.mesh.addNodes(2, 1, node_tags, shell_pts.ravel())
        gmsh.model.mesh.addElementsByType(1, 2, [], (shell_tris + 1).ravel())
        loop = gmsh.model.geo.addSurfaceLoop([1])
        gmsh.model.geo.addVolume([loop])
        gmsh.model.geo.synchronize()
        gmsh.option.setNumber("Mesh.MeshSizeMax", mesh_size)
        gmsh.model.mesh.generate(3)
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
        raise ValueError("gmsh produced no tetrahedra for the lumen shell")

    def build(tets):
        cells = np.column_stack(
            [np.full(len(tets), 4, dtype=np.int64), tets]
        ).ravel()
        ctypes = np.full(len(tets), pv.CellType.TETRA, dtype=np.uint8)
        g = pv.UnstructuredGrid(cells, ctypes, points)
        g.cell_data["region"] = np.ones(g.n_cells, dtype=np.int32)
        return g

    grid = build(tets)
    quality = volume_quality(grid)
    if quality.n_inverted:
        q = grid.cell_quality(["volume"])
        flip = np.asarray(q["volume"], dtype=float) <= 0
        tets[flip] = tets[flip][:, [0, 1, 3, 2]]
        grid = build(tets)
        quality = volume_quality(grid)
        quality.notes.append(
            f"reoriented {int(flip.sum())} negatively-wound tets from gmsh"
        )

    tree = cKDTree(shell_pts[shell_tris].mean(axis=1))
    surf = grid.extract_surface(algorithm="dataset_surface")
    faces = surf.faces.reshape(-1, 4)[:, 1:]
    centroids = surf.points[faces].mean(axis=1)
    _, nearest = tree.query(centroids)
    face_labels = patch_labels[nearest]
    orig_cell = np.asarray(surf.cell_data["vtkOriginalCellIds"], dtype=np.int64)
    orig_point = np.asarray(surf.point_data["vtkOriginalPointIds"], dtype=np.int64)
    face_nodes = orig_point[faces]
    boundary: dict[str, BoundaryGroup] = {}
    for label, name in _LABELS.items():
        sel = face_labels == label
        if not sel.any():
            continue
        boundary[name] = BoundaryGroup(
            name=name, cell_ids=orig_cell[sel], face_nodes=face_nodes[sel]
        )
    return grid, boundary, quality, opt_note


# --- public entry -------------------------------------------------------------


def fill_lumen(
    surface: pv.PolyData,
    *,
    method: str = "auto",
    n_theta: int = 32,
    n_axial: int = 40,
    core_fraction: float = 0.5,
    smooth: int = 4,
    mesh_size: float | None = None,
    centerline=None,
) -> WallMesh:
    """Fill the blood volume of a tubular surface (V47).

    ``method``: ``ogrid`` = structured swept O-grid hexes (loud failure where
    the geometry obstructs), ``tet`` = gmsh tetra fill of the station-ring
    shell, ``auto`` (default) = ogrid, falling back to tet with a loud
    fold-number note when the structured fill fails or sweeps inverted.
    """
    if method not in ("auto", "ogrid", "tet"):
        raise ValueError(
            f"lumen method must be auto | ogrid | tet, got {method!r}"
        )
    if n_theta < 8 or n_axial < 2:
        raise ValueError(
            f"lumen lattice too coarse: n_theta={n_theta}, n_axial={n_axial}"
        )
    if smooth < 0:
        raise ValueError(f"smooth iterations must be >= 0, got {smooth}")
    if mesh_size is not None and not mesh_size > 0:
        raise ValueError(f"mesh_size must be positive, got {mesh_size}")
    if centerline is None:
        from meshforge.geometry import centerline_from_surface, compute_centerline

        centerline = centerline_from_surface(surface)
        if centerline is None:
            centerline = compute_centerline(surface)

    stations, tangents, normals, binormals, radii, arc, cos_t, sin_t = (
        _station_geometry(surface, centerline, n_theta=n_theta, n_axial=n_axial)
    )
    F = _fold_numbers(tangents, arc, radii)
    fold = _fold_note(F)
    notes: list[str] = []

    def structured() -> WallMesh:
        disks, quads, rim, n_node = _station_disks(
            radii, cos_t, sin_t, core_fraction=core_fraction
        )
        pts, hexes = _sweep(stations, normals, binormals, disks, quads, n_node)
        grid = _hex_grid(pts, hexes)
        quality = volume_quality(grid)
        if quality.n_inverted:
            raise ValueError(
                f"structured lumen sweep has {quality.n_inverted} inverted "
                f"cell(s) ({fold}) — the section crosses the fold radius"
            )
        smooth_note = None
        if smooth > 0:
            bmask = _boundary_nodes(quads, rim, n_node, len(stations))
            new_pts, accepted = guarded_smooth(
                pts, hexes, bmask, iterations=smooth
            )
            if accepted:
                sj_before = quality.sj_min
                grid = _hex_grid(new_pts, hexes)
                quality = volume_quality(grid)
                smooth_note = (
                    f"guarded smoothing: {accepted} accepted moves in <= "
                    f"{smooth} sweep(s), sj_min {sj_before:.4g} -> "
                    f"{quality.sj_min:.4g} (do-no-harm guard)"
                )
                if quality.n_inverted:
                    raise ValueError(
                        "guarded smoothing produced inverted cells — the "
                        "guard metric disagrees with VTK on this mesh; "
                        "re-run with smooth=0 and report"
                    )
        boundary = _ogrid_boundary(quads, rim, n_node, len(stations))
        mesh_notes = [
            f"swept O-grid lumen: {len(stations)} stations x {len(quads)} "
            f"disk quads (pinned topology), n_theta {n_theta}",
            fold,
        ]
        if smooth_note:
            mesh_notes.append(smooth_note)
        mesh_notes.extend(notes)
        mesh_notes.extend(quality.notes)
        return WallMesh(
            grid=grid,
            boundary=boundary,
            backend="lumen-ogrid",
            quality=quality,
            notes=mesh_notes,
        )

    def tet(reason: str | None) -> WallMesh:
        shell_pts, shell_tris, labels = _ring_shell(
            stations, normals, binormals, radii, cos_t, sin_t
        )
        size = mesh_size
        if size is None:
            # Median ring edge — the boundary discretization scale. (A
            # median over ALL shell triangle edges mixes in the long axial
            # edges and produced 4x-coarser slivers on the real arches.)
            rings2d = np.stack([radii * cos_t, radii * sin_t], axis=2)
            ring_edges = np.linalg.norm(
                np.roll(rings2d, -1, axis=1) - rings2d, axis=2
            )
            size = float(np.median(ring_edges)) * 1.5
        grid, boundary, quality, opt_note = _tet_fill(
            shell_pts, shell_tris, labels, mesh_size=size
        )
        mesh_notes = [
            f"tet lumen fill on the station-ring shell ({len(shell_pts)} "
            f"shell nodes, mesh_size={size:.4g})",
            opt_note,
            fold,
        ]
        if reason:
            mesh_notes.append(reason)
        mesh_notes.extend(notes)
        mesh_notes.extend(quality.notes)
        return WallMesh(
            grid=grid,
            boundary=boundary,
            backend="lumen-tet",
            quality=quality,
            notes=mesh_notes,
        )

    if method == "ogrid":
        return structured()
    if method == "tet":
        return tet(None)
    try:
        return structured()
    except ValueError as exc:
        # The station geometry itself is fine (rays hit) — the STRUCTURED
        # fill is what failed; the tet fallback meshes the same shell.
        return tet(
            f"structured O-grid fill failed ({exc}); tet fallback ({fold})"
        )

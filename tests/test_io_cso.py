"""V38 MeVisLab Binary .cso ingest: a synthetic binary-CSO writer (mirrors the
verified grammar) drives watertight-loft + inverted==0 volmesh truths, the loud
degenerate branches (unknown tag / truncation / open / <3-pt / lone contour),
guide-track ordering, provenance, a replay round-trip, and a read-only test
against the real sample when present (skips cleanly otherwise — it lives outside
the repo and must never be copied in, rule 1)."""
from __future__ import annotations

import struct
from pathlib import Path

import numpy as np
import pytest
from conftest import cso_family_dir, dicom_dir, first_local_file

from meshforge.geometry import centerline_from_surface, embed_centerline
from meshforge.io.cso import (
    Contour,
    contour_centerline,
    CsoData,
    cso_input_sha256,
    cso_to_surface,
    load_cso,
)
from meshforge.pipeline import MeshingSession, auto_cso_surface, load_workfile, replay

# ---------------------------------------------------------------------------
# a minimal Binary CSO writer — exactly the tokens load_cso decodes
# ---------------------------------------------------------------------------
def _u32(v: int) -> bytes:
    return b"U32_" + struct.pack("<I", v)


def _i32(v: int) -> bytes:
    return b"I32_" + struct.pack("<i", v)


def _str(s: str) -> bytes:
    b = s.encode("utf-8")
    return b"STR_" + struct.pack("<Q", len(b)) + b


def _vec3(x: float, y: float, z: float) -> bytes:
    return b"VEC3" + struct.pack("<3d", x, y, z)


def _ring_bytes(points, normal, *, closed=True, module="SoCSOSplineEditor",
                for_uid="", series="", spline_k=2) -> bytes:
    """One CSO record: attribute block + header + interleaved control points +
    an interpolated-spline block (segments with ``spline_k`` raw f64 triples, to
    exercise the raw-skip path)."""
    P = np.asarray(points, dtype=float)
    n = len(P)
    out = _str("moduleCreatorString") + _str(module)
    if for_uid:
        out += _str("frameOfReferenceUID") + _str(for_uid)
    if series:
        out += _str("seriesInstanceUID") + _str(series)
    bmin, bmax = P.min(0), P.max(0)
    out += _vec3(*normal) + _vec3(*bmin) + _vec3(*bmax)
    out += _u32(1) + _u32(0) + _u32(n)
    for k, p in enumerate(P):
        out += _vec3(*p)
        prev = (k - 1) if k > 0 else (n - 1 if closed else -1)
        out += _i32(prev) + _i32(k)
    nseg = n if closed else max(n - 1, 0)
    out += _u32(nseg)
    for _ in range(nseg):
        out += _u32(spline_k) + b"\x00" * (24 * spline_k) + _i32(0) + _i32(0)
    return out


def _write_cso(path: Path, records: list[bytes], *, header_count=None,
               version=(1, 6, 0)) -> Path:
    hc = len(records) if header_count is None else header_count
    body = b"Binary CSO" + bytes(version) + _u32(hc) + b"".join(records)
    path.write_bytes(body)
    return path


def _circle(radius, z, npts=16, cx=0.0, cy=0.0):
    a = np.linspace(0, 2 * np.pi, npts, endpoint=False)
    return np.column_stack([cx + radius * np.cos(a), cy + radius * np.sin(a),
                            np.full(npts, float(z))])


def _straight_tube_cso(path: Path, *, n_rings=12, radius=3.0, dz=1.5,
                       npts=16, with_guide=False, shuffle=False) -> Path:
    zs = [i * dz for i in range(n_rings)]
    rings = [(_circle(radius, z, npts), (0.0, 0.0, 1.0)) for z in zs]
    order = list(range(n_rings))
    if shuffle:
        rng = np.random.default_rng(0)
        rng.shuffle(order)
    recs = [
        _ring_bytes(rings[i][0], rings[i][1], for_uid="FOR-1.2.3",
                    series="SER-4.5.6")
        for i in order
    ]
    if with_guide:
        guide = np.column_stack([np.zeros(n_rings), np.zeros(n_rings), zs])
        recs.insert(0, _ring_bytes(guide, (0.0, 0.0, 1.0), closed=False,
                                   module="CSOMarkerGenerator"))
    return _write_cso(path, recs)


def _arc(radius, z, npts=9, span=np.pi, cx=0.0, cy=0.0):
    """An open arc: ``span`` radians of a circle, so first->last gap is large."""
    a = np.linspace(0, span, npts)
    return np.column_stack([cx + radius * np.cos(a), cy + radius * np.sin(a),
                            np.full(npts, float(z))])


def _tube_with_open(path: Path, *, wide: bool, n_closed=10, radius=3.0, dz=1.5,
                    npts=16) -> Path:
    """A stack of closed rings with ONE open SoCSOSplineEditor contour inserted
    mid-stack.  ``wide`` -> a half-circle arc (gap ~5x mean edge, un-closeable);
    else a full circle sampled open (gap ~1x, a real un-snapped ring)."""
    zs = [i * dz for i in range(n_closed)]
    recs = [_ring_bytes(_circle(radius, z, npts), (0.0, 0.0, 1.0)) for z in zs]
    mid = n_closed // 2
    mid_z = (mid - 0.5) * dz  # a plane between two closed rings
    ring = (_arc(radius, mid_z, npts=9, span=np.pi) if wide
            else _circle(radius, mid_z, npts))
    recs.insert(mid, _ring_bytes(ring, (0.0, 0.0, 1.0), closed=False,
                                 module="SoCSOSplineEditor"))
    return _write_cso(path, recs)


# ---------------------------------------------------------------------------
# parsing
# ---------------------------------------------------------------------------
def test_load_straight_tube(tmp_path):
    p = _straight_tube_cso(tmp_path / "tube.cso")
    data = load_cso(p)
    assert isinstance(data, CsoData)
    assert data.version == (1, 6, 0)
    assert len(data.contours) == 12
    assert len(data.guides) == 0
    assert all(c.closed for c in data.contours)
    assert all(len(c.points) == 16 for c in data.contours)
    assert data.frame_of_reference_uid == "FOR-1.2.3"
    assert data.series_instance_uid == "SER-4.5.6"
    # world-mm coordinates preserved (float64)
    assert data.contours[0].points.dtype == np.float64


def test_interleaved_points_not_vec3_adjacency(tmp_path):
    """A ring's control points are interleaved with I32_ link tokens, yet all N
    points are recovered (segment by record, never by VEC3 adjacency)."""
    p = _straight_tube_cso(tmp_path / "tube.cso", npts=13)
    data = load_cso(p)
    assert all(len(c.points) == 13 for c in data.contours)


def test_guide_orders_shuffled_contours(tmp_path):
    p = _straight_tube_cso(tmp_path / "tube.cso", with_guide=True, shuffle=True)
    data = load_cso(p)
    assert len(data.guides) == 1
    z = [float(c.centroid[2]) for c in data.contours]
    assert z == sorted(z), f"contours not ordered along the guide: {z}"


def test_nearest_neighbour_orders_without_guide(tmp_path):
    p = _straight_tube_cso(tmp_path / "tube.cso", with_guide=False, shuffle=True)
    data = load_cso(p)
    z = [float(c.centroid[2]) for c in data.contours]
    # monotone (either direction) — the chain has no preferred end
    assert z == sorted(z) or z == sorted(z, reverse=True)


# ---------------------------------------------------------------------------
# display passthrough (session-34 workbench QC overlay)
# ---------------------------------------------------------------------------
def test_contours_polydata_is_exact_passthrough(tmp_path):
    """The overlay polydata carries the EXACT parsed control points (no
    resampling, no new geometry) — one closed polyline cell per ring, wrapped
    back to its first point."""
    from meshforge.io.cso import contours_polydata

    p = _straight_tube_cso(tmp_path / "tube.cso", n_rings=5, npts=11)
    data = load_cso(p)
    poly = contours_polydata(data)

    want = np.vstack([c.points for c in data.contours])
    assert poly.n_points == len(want)
    assert np.array_equal(np.asarray(poly.points, dtype=np.float64), want)
    assert poly.n_cells == len(data.contours)

    lines = np.asarray(poly.lines)
    off = 0
    first_id = 0
    for c in data.contours:
        n = int(lines[off])
        ids = lines[off + 1: off + 1 + n]
        assert n == len(c.points) + 1  # closed: wraps back to the first point
        assert ids[0] == first_id and ids[-1] == first_id
        off += 1 + n
        first_id += len(c.points)


def test_contours_polydata_refuses_empty():
    from meshforge.io.cso import CsoData, contours_polydata

    with pytest.raises(ValueError, match="no contours"):
        contours_polydata(CsoData(contours=[]))


# ---------------------------------------------------------------------------
# surface + downstream mesh
# ---------------------------------------------------------------------------
def test_loft_is_watertight_and_meshes_clean(tmp_path):
    p = _straight_tube_cso(tmp_path / "tube.cso")
    data = load_cso(p)
    surface, prov = cso_to_surface(data, method="loft")
    assert prov["watertight"] is True
    assert int(surface.n_open_edges) == 0

    s = MeshingSession()
    s.surface = surface
    s.compute_centerline()
    r = s.compute_volmesh(backend="swept", thickness=0.5, n_theta=16,
                          n_axial=20, n_layers=2)
    q = r.summary["quality"]
    assert q["n_inverted"] == 0
    assert q["sj_min"] > 0.5  # a clean straight tube


def test_voxel_fallback_is_watertight(tmp_path):
    p = _straight_tube_cso(tmp_path / "tube.cso", dz=1.0)
    data = load_cso(p)
    surface, prov = cso_to_surface(data, method="voxel")
    assert prov["method"] == "voxel"
    assert prov["watertight"] is True
    assert int(surface.n_open_edges) == 0


def test_cap_ends_false_leaves_open_surface(tmp_path):
    p = _straight_tube_cso(tmp_path / "tube.cso")
    data = load_cso(p)
    surface, prov = cso_to_surface(data, method="loft", cap_ends=False)
    assert int(surface.n_open_edges) > 0
    assert prov["watertight"] is False


# ---------------------------------------------------------------------------
# V40: interior contour centreline + auto surface/centreline strategy
# ---------------------------------------------------------------------------
def test_contour_centerline_is_interior_and_meshes(tmp_path):
    import pyvista as pv

    p = _straight_tube_cso(tmp_path / "tube.cso", with_guide=True)
    data = load_cso(p)
    surface, _ = cso_to_surface(data, method="loft")
    cl = contour_centerline(data)
    assert cl.closed is False
    assert len(cl.points) == 200
    # every station is inside the lumen (the clamp guarantees interiority)
    sel = pv.PolyData(cl.points).select_enclosed_points(surface, check_surface=False)
    assert int((~np.asarray(sel["SelectedPoints"]).astype(bool)).sum()) == 0
    # and it sweeps to a clean volmesh
    s = MeshingSession()
    s.surface = surface
    r = s.compute_volmesh(backend="swept", thickness=0.5, centerline=cl,
                          n_theta=16, n_axial=20, n_layers=2)
    assert r.summary["quality"]["n_inverted"] == 0


def test_contour_centerline_needs_two_contours(tmp_path):
    p = _straight_tube_cso(tmp_path / "tube.cso")
    data = load_cso(p)
    with pytest.raises(ValueError, match="at least 2 contours"):
        contour_centerline(data.contours[:1])


def test_embed_centerline_roundtrips_through_vtp(tmp_path):
    p = _straight_tube_cso(tmp_path / "tube.cso", with_guide=True)
    data = load_cso(p)
    surface, _ = cso_to_surface(data, method="loft")
    cl = contour_centerline(data)
    embed_centerline(surface, cl)
    out = tmp_path / "tube_surface.vtp"
    surface.save(out)

    import pyvista as pv

    restored = centerline_from_surface(pv.read(out))
    assert restored is not None
    assert np.allclose(restored.points, cl.points)
    assert np.allclose(restored.radii, cl.radii)
    assert restored.closed == cl.closed
    # a surface with no embedded centreline yields None
    assert centerline_from_surface(cso_to_surface(data, method="loft")[0]) is None


def test_import_cso_auto_is_default_and_embeds(tmp_path):
    p = _straight_tube_cso(tmp_path / "tube.cso", with_guide=True)
    s = MeshingSession()
    r = s.compute_import_cso(path=str(p))  # method defaults to "auto"
    strategy = r.summary["cso"]["strategy"]
    assert strategy.startswith("loft")  # a clean tube lofts
    # the validated centreline rides along on the surface + the session
    assert centerline_from_surface(s.surface) is not None
    assert s.centerline is not None
    v = s.compute_volmesh(backend="swept", thickness=0.5, n_theta=16,
                          n_axial=20, n_layers=2)
    assert v.summary["quality"]["n_inverted"] == 0


def test_auto_falls_back_to_voxel_when_loft_wont_sweep(tmp_path, monkeypatch):
    """When no loft centreline sweeps clean, auto returns the voxel surface with
    no embedded centreline (a fresh skeleton on the voxel surface is used)."""
    import meshforge.meshing as meshing_pkg

    class _RaisingBackend:
        def mesh_wall(self, *a, **k):
            raise ValueError("forced ray-miss for the test")

    monkeypatch.setattr(meshing_pkg, "get_backend", lambda name: _RaisingBackend())

    p = _straight_tube_cso(tmp_path / "tube.cso", with_guide=True)
    data = load_cso(p)
    surface, prov, cline, strategy = auto_cso_surface(
        data, n_ring=48, voxel_size=None, cap_ends=True
    )
    assert prov["method"] == "voxel"
    assert cline is None
    assert strategy.startswith("voxel")
    assert centerline_from_surface(surface) is None


def test_unknown_method_is_loud(tmp_path):
    p = _straight_tube_cso(tmp_path / "tube.cso")
    data = load_cso(p)
    with pytest.raises(ValueError, match="unknown method"):
        cso_to_surface(data, method="bogus")


# ---------------------------------------------------------------------------
# degenerate / loud branches
# ---------------------------------------------------------------------------
def test_not_a_binary_cso(tmp_path):
    p = tmp_path / "bad.cso"
    p.write_bytes(b"<?xml version='1.0'?><contours/>")
    with pytest.raises(ValueError, match="not a Binary CSO"):
        load_cso(p)


def test_unknown_tag_is_loud(tmp_path):
    # a valid ring then a garbage tag where the next record should start
    rec = _ring_bytes(_circle(3.0, 0.0), (0, 0, 1))
    body = b"Binary CSO" + bytes((1, 6, 0)) + _u32(2) + rec + b"XXXX" + b"\x00" * 8
    p = tmp_path / "unknown.cso"
    p.write_bytes(body)
    with pytest.raises(ValueError, match="unknown CSO tag"):
        load_cso(p)


def test_truncated_file_is_loud(tmp_path):
    full = _straight_tube_cso(tmp_path / "tube.cso").read_bytes()
    p = tmp_path / "trunc.cso"
    p.write_bytes(full[: len(full) - 40])  # cut mid-token
    with pytest.raises(ValueError, match="truncated|corrupt"):
        load_cso(p)


def test_open_contour_error_policy_is_loud(tmp_path):
    # on_open="error" refuses any open editor contour (the strict V38 behaviour)
    p = _tube_with_open(tmp_path / "openerr.cso", wide=True)
    with pytest.raises(ValueError, match="OPEN contour"):
        load_cso(p, on_open="error")


def test_open_contour_skip_drops_it(tmp_path):
    # default policy: a wide-open arc mid-stack is dropped with a loud note, the
    # remaining closed rings still loft to a watertight surface (the VAM997 case)
    p = _tube_with_open(tmp_path / "openskip.cso", wide=True, n_closed=10)
    data = load_cso(p)  # default on_open="skip"
    assert len(data.contours) == 10
    assert any("skipped OPEN contour" in n for n in data.notes)
    assert any("first->last gap" in n for n in data.notes)
    surface, prov = cso_to_surface(data, method="loft")
    assert prov["watertight"] is True


def test_open_contour_close_promotes_near_closed(tmp_path):
    # on_open="close": a near-closed ring (gap ~1x mean edge) is promoted
    p = _tube_with_open(tmp_path / "closeok.cso", wide=False, n_closed=10)
    data = load_cso(p, on_open="close")
    assert len(data.contours) == 11  # the open ring joined the stack
    assert any("auto-closed" in n for n in data.notes)
    assert all(c.closed for c in data.contours)


def test_open_contour_close_refuses_wide_gap(tmp_path):
    # on_open="close" must NOT fabricate a chord across a wide-open arc
    p = _tube_with_open(tmp_path / "closewide.cso", wide=True)
    with pytest.raises(ValueError, match="too large to auto-close"):
        load_cso(p, on_open="close")


def test_invalid_on_open_is_loud(tmp_path):
    p = _straight_tube_cso(tmp_path / "tube.cso")
    with pytest.raises(ValueError, match="on_open must be one of"):
        load_cso(p, on_open="bogus")


def test_too_few_points_is_loud(tmp_path):
    two = np.array([[0.0, 0, 0], [1.0, 0, 0]])
    rec = _ring_bytes(two, (0, 0, 1))
    p = _write_cso(tmp_path / "two.cso", [rec])
    with pytest.raises(ValueError, match="at least 3"):
        load_cso(p)


def test_no_records_is_loud(tmp_path):
    body = b"Binary CSO" + bytes((1, 6, 0)) + _u32(0)
    p = tmp_path / "empty.cso"
    p.write_bytes(body)
    with pytest.raises(ValueError, match="no CSO records"):
        load_cso(p)


def test_single_contour_cannot_surface(tmp_path):
    rec = _ring_bytes(_circle(3.0, 0.0), (0, 0, 1))
    p = _write_cso(tmp_path / "one.cso", [rec])
    data = load_cso(p)
    assert len(data.contours) == 1
    with pytest.raises(ValueError, match="at least 2 contours"):
        cso_to_surface(data, method="loft")


# ---------------------------------------------------------------------------
# session + replay
# ---------------------------------------------------------------------------
def test_session_step_and_replay_roundtrip(tmp_path):
    p = _straight_tube_cso(tmp_path / "tube.cso")
    s = MeshingSession()
    r = s.compute_import_cso(path=str(p), method="loft")
    assert r.summary["watertight"] is True
    assert s.input_sha256 == cso_input_sha256(p)

    wf = s.save_workfile(tmp_path / "w.mfwork")
    s2 = load_workfile(wf)
    results = replay(s2)
    assert results[0].summary["cso"]["n_contours"] == 12
    assert s2.surface.n_points == s.surface.n_points


def test_import_cso_requires_path():
    s = MeshingSession()
    with pytest.raises(ValueError, match="needs a .cso path"):
        s.compute_import_cso(path="")


# ---------------------------------------------------------------------------
# real sample (read-only; skips when absent)
# ---------------------------------------------------------------------------
_FAMILY_DIR = cso_family_dir()  # machine-local, resolved by conftest; may be None
_REAL = first_local_file(_FAMILY_DIR, "VAM775*.cso")


@pytest.mark.skipif(_REAL is None, reason="real .cso sample not present")
def test_real_sample_readonly_end_to_end():
    before = cso_input_sha256(_REAL)
    data = load_cso(_REAL)
    assert data.version == (1, 6, 0)
    assert len(data.contours) == 20
    assert len(data.guides) == 1
    surface, prov = cso_to_surface(data, method="loft")
    assert prov["watertight"] is True

    s = MeshingSession()
    s.surface = surface
    s.compute_centerline()
    r = s.compute_volmesh(backend="swept", thickness=1.0, n_theta=24,
                          n_axial=40, n_layers=3)
    assert r.summary["quality"]["n_inverted"] == 0
    # rule 1: the input is byte-for-byte unchanged after reading it
    assert cso_input_sha256(_REAL) == before


# ---------------------------------------------------------------------------
# real family (read-only; skips when the folder is absent — lives outside the
# repo, rule 1).  V39: every present real .cso must load under the default
# open-contour policy, loft watertight, and sweep to inverted==0.
# ---------------------------------------------------------------------------
_FAMILY = sorted(_FAMILY_DIR.glob("*.cso")) if _FAMILY_DIR is not None else []


@pytest.mark.skipif(not _FAMILY, reason="real .cso family folder not present")
@pytest.mark.parametrize("cso_path", _FAMILY, ids=lambda p: p.stem.split("_")[0])
def test_real_family_end_to_end(cso_path):
    before = cso_input_sha256(cso_path)

    # V40: the PRODUCT's default auto strategy picks the first surface+centreline
    # that sweeps to inverted==0 — every real arch meshes through the normal
    # import_cso -> volmesh path with no manual loft/voxel choice.  9/10 loft via
    # skeleton or the interior contour centreline; only the most tortuous arch
    # (VAM760) falls back to voxel (D60, proven by probe_cascade this session).
    s = MeshingSession()
    r = s.compute_import_cso(path=str(cso_path))  # method="auto"
    assert r.summary["cso"]["version"] == [1, 6, 0]
    strategy = r.summary["cso"]["strategy"]
    v = s.compute_volmesh(backend="swept", thickness=1.0, n_theta=24,
                          n_axial=40, n_layers=3)
    assert v.summary["quality"]["n_inverted"] == 0

    name = cso_path.stem.split("_")[0]
    if name == "VAM760":
        assert strategy.startswith("voxel")
    else:
        assert strategy.startswith("loft")
    assert cso_input_sha256(cso_path) == before  # rule 1: unchanged


# ---------------------------------------------------------------------------
# V39: image-anchored voxelisation — pin the label mask to an external grid
# ---------------------------------------------------------------------------
def test_cso_voxelise_on_grid_is_lattice_aligned(tmp_path):
    p = _straight_tube_cso(tmp_path / "tube.cso", radius=3.0, dz=1.5, n_rings=12)
    data = load_cso(p)
    # a grid that comfortably contains the tube (x,y in [-3,3], z in [0,16.5])
    origin = (-6.0, -6.0, -2.0)
    spacing = (0.5, 0.5, 0.5)
    dims = (28, 28, 44)
    surf, prov = cso_to_surface(data, method="voxel", grid=(dims, spacing, origin))
    assert prov["image_anchored"] is True
    assert prov["watertight"] is True
    # the ROI sub-origin is an INTEGER voxel offset from the grid origin
    off = (np.array(prov["origin"]) - np.array(origin)) / np.array(spacing)
    np.testing.assert_allclose(off, np.round(off), atol=1e-6)
    # surface lands where the contours are
    P = np.vstack([c.points for c in data.contours])
    b = surf.bounds
    assert b[0] < P[:, 0].max() and b[1] > P[:, 0].min()
    assert b[4] <= P[:, 2].min() + 2 and b[5] >= P[:, 2].max() - 2


def test_cso_grid_requires_voxel_method(tmp_path):
    p = _straight_tube_cso(tmp_path / "tube.cso")
    data = load_cso(p)
    with pytest.raises(ValueError, match="grid anchoring requires"):
        cso_to_surface(data, method="loft", grid=((10, 10, 10), (1, 1, 1), (0, 0, 0)))


def test_cso_grid_out_of_bounds_is_loud(tmp_path):
    p = _straight_tube_cso(tmp_path / "tube.cso", radius=3.0)
    data = load_cso(p)
    # a grid nowhere near the contours -> they do not share the frame
    with pytest.raises(ValueError, match="outside the image grid"):
        cso_to_surface(data, method="voxel",
                       grid=((10, 10, 10), (0.5, 0.5, 0.5), (500.0, 500.0, 500.0)))


_REAL_DCM = first_local_file(dicom_dir(), "VAM775*.dcm")


@pytest.mark.skipif(_REAL is None or _REAL_DCM is None,
                    reason="real .cso + .dcm pair not present")
def test_real_cso_anchored_to_dicom_grid():
    from meshforge.io.image import dicom_grid, volume_input_sha256

    dsha = volume_input_sha256(_REAL_DCM)
    csha = cso_input_sha256(_REAL)
    data = load_cso(_REAL)
    grid = dicom_grid(_REAL_DCM)
    surf, prov = cso_to_surface(data, method="voxel", grid=grid)
    assert prov["image_anchored"] is True and prov["watertight"] is True
    # anchored surface sits on the DICOM lattice, at the CSO's world location
    dims, spacing, origin = grid
    off = (np.array(prov["origin"]) - np.array(origin)) / np.array(spacing)
    np.testing.assert_allclose(off, np.round(off), atol=1e-6)
    P = np.vstack([c.points for c in data.contours])
    b = surf.bounds
    assert abs(b[0] - P[:, 0].min()) < 3 and abs(b[5] - P[:, 2].max()) < 3
    # rule 1: neither input touched
    assert volume_input_sha256(_REAL_DCM) == dsha
    assert cso_input_sha256(_REAL) == csha

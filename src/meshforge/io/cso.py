"""MeVisLab Contour Segmentation Object (.cso) ingest (V38).

Loads a **Binary CSO, version (1,6,0)** — the format MeVisLab's ``CSOSave``
writes — as a stack of closed cross-section contours, and lofts (or voxelises)
them into a watertight tube surface that flows into the normal
centerline -> wall -> volmesh -> solve pipeline. All geometry logic lives here
(rule 4); the pipeline/CLI/web layers only call a session step.

Verified format (decoded byte-for-byte from a real cardiac segmentation):

* bytes 0..9   ``b"Binary CSO"``; bytes 10..12 the version triple.
* byte 13..    a self-describing **tagged token stream**, little-endian: a
  4-char tag then its payload.  Scalar tags (``U32_ I32_ F32_ FLT_`` -> 4 bytes;
  ``U64_ I64_ F64_ DBL_`` -> 8 bytes; ``BOOL``/``U08_`` -> 1 byte); ``STR_`` ->
  8-byte length + UTF-8 bytes; ``VEC3`` -> 3 x float64.
* Each CSO **record** is a header (a plane normal ``VEC3`` + two bounding-box
  ``VEC3`` + three ``U32_``, the last of which is the ring point count ``npts``)
  followed by ``npts`` control points, each a ``VEC3`` interleaved with two
  ``I32_`` doubly-linked-list indices (``prev``, ``this``).  A closed ring's
  first point has ``prev == npts-1``; an open path (a ``CSOMarkerGenerator``
  seed track) has ``prev == -1``.  After the control ring comes an
  **interpolated-spline block** (``U32_`` segment count, then per segment a
  ``U32_`` point count + that many *raw* float64 triples + two ``I32_``); those
  raw doubles are the only non-tagged bytes, and they always follow a ring — so
  we consume the block explicitly and the rest of the stream stays a clean token
  walk.  Any genuinely unknown tag is a **loud** error (a silent skip would
  corrupt geometry — trap #6/rule 6).

TRAP baked in: a contour's control points are interleaved with those per-point
metadata tokens, so consecutive ``VEC3`` are *not* contiguous.  We segment by the
record structure (each record's ``npts``), never by ``VEC3`` adjacency.

Coordinates are 3-D world/patient millimetres.  ``frameOfReferenceUID`` and
``seriesInstanceUID`` are preserved as provenance (they tie the contours to a
DICOM series).  The user's CSOs are always a single closed ring per level (one
lumen boundary, never paired endo/epi) — the muscle wall is added downstream by
the ``wall`` step's thickness, exactly like the straight-tube demo.
"""
from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

_MAGIC = b"Binary CSO"

# tag -> (struct format, payload byte count) for the fixed-width scalar tokens.
_FIXED: dict[bytes, tuple[str, int]] = {
    b"U32_": ("<I", 4), b"I32_": ("<i", 4), b"F32_": ("<f", 4),
    b"FLT_": ("<f", 4), b"U64_": ("<Q", 8), b"I64_": ("<q", 8),
    b"F64_": ("<d", 8), b"DBL_": ("<d", 8), b"BOOL": ("<?", 1),
    b"U08_": ("<B", 1),
}
_KNOWN_TAGS = set(_FIXED) | {b"STR_", b"VEC3"}

# Bound the number of CSO records so a crafted file packed with many tiny
# contours cannot drive the O(n^2) centreline ordering (a real segmentation has
# a handful of rings; this is ~5000x that).  A DoS ceiling, not a format limit.
MAX_RECORDS = 100_000
# Upper bound on the per-ring loft resample count: a crafted .mfwork / CLI value
# must not reach the loft allocator (2 * n_ring * n_contours ints) unbounded.
MAX_N_RING = 100_000
# Magnitude ceiling for parsed geometry (coordinates AND plane normals).
# Real scanner coordinates are millimetres (|x| < ~1e4); values around 1e154,
# while byte-legal and FINITE (they pass every NaN/inf guard), overflow the
# downstream loft arithmetic (squared distances, cross products) into NaN —
# a fail-open "watertight" NaN mesh, F-CSO-1's pattern through the overflow
# door (session-34 fuzz finding). 1e9 mm = 1000 km: generous for any clinical
# or engineering use, and ~145 orders of magnitude below the overflow.
MAX_COORD = 1e9


@dataclass
class Contour:
    """One CSO record in world millimetres.

    ``points`` is the ordered ring (Nx3 float64); ``normal`` its unit plane
    normal; ``closed`` True for a segmentation ring, False for an open marker
    guide path.  ``module`` is the MeVisLab creator (``SoCSOSplineEditor`` for a
    contour, ``CSOMarkerGenerator`` for a seed track).
    """

    points: np.ndarray
    normal: np.ndarray
    closed: bool
    id: int
    label: str
    module: str
    frame_of_reference_uid: str = ""
    series_instance_uid: str = ""

    @property
    def centroid(self) -> np.ndarray:
        return self.points.mean(axis=0)


@dataclass
class CsoData:
    """Parsed CSO: the ordered closed ``contours`` (lumen cross-sections) plus
    any open ``guides`` (marker seed tracks), version, and provenance."""

    contours: list[Contour]
    guides: list[Contour] = field(default_factory=list)
    version: tuple[int, int, int] = (0, 0, 0)
    n_records: int = 0
    header_count: int = 0
    frame_of_reference_uid: str = ""
    series_instance_uid: str = ""
    notes: list[str] = field(default_factory=list)


def cso_input_sha256(path: str | Path) -> str:
    """Checksum of the input file (rule 1: user input is read-only)."""
    from meshforge.io.load import file_sha256

    return file_sha256(Path(path))


# ---------------------------------------------------------------------------
# low-level tokeniser
# ---------------------------------------------------------------------------
class _Reader:
    """Cursor over the tagged token stream; every read is bounds-checked so a
    truncated file fails loudly rather than reading past the buffer."""

    def __init__(self, buf: bytes, pos: int):
        self.buf = buf
        self.pos = pos

    def _need(self, n: int, what: str) -> None:
        if self.pos + n > len(self.buf):
            raise ValueError(
                f"truncated CSO: needed {n} more byte(s) for {what} at "
                f"offset {self.pos} (file is {len(self.buf)} bytes)"
            )

    def peek_tag(self) -> bytes:
        if self.pos + 4 > len(self.buf):
            return b""
        return self.buf[self.pos:self.pos + 4]

    def read_token(self):
        """Read one tagged token -> (tag:str, value). Unknown tag: loud."""
        tag = self.peek_tag()
        if tag in _FIXED:
            fmt, sz = _FIXED[tag]
            self._need(4 + sz, tag.decode())
            (val,) = struct.unpack(fmt, self.buf[self.pos + 4:self.pos + 4 + sz])
            self.pos += 4 + sz
            return tag.decode(), val
        if tag == b"STR_":
            self._need(12, "STR_ length")
            (ln,) = struct.unpack("<Q", self.buf[self.pos + 4:self.pos + 12])
            if ln > len(self.buf):
                raise ValueError(
                    f"corrupt CSO: STR_ length {ln} at offset {self.pos} "
                    f"exceeds the file size"
                )
            self._need(12 + ln, "STR_ bytes")
            s = self.buf[self.pos + 12:self.pos + 12 + ln].decode("utf-8", "replace")
            self.pos += 12 + ln
            return "STR_", s
        if tag == b"VEC3":
            self._need(28, "VEC3")
            xyz = struct.unpack("<3d", self.buf[self.pos + 4:self.pos + 28])
            self.pos += 28
            return "VEC3", xyz
        raise ValueError(
            f"unknown CSO tag {tag!r} at offset {self.pos} — refusing to skip "
            f"it (a silent skip would corrupt geometry). Context: "
            f"{self.buf[self.pos:self.pos + 16].hex(' ')}"
        )


# A ring header is three VEC3 (normal + bbox min/max) then three U32_, then the
# first control-point VEC3. The byte offsets are fixed by the tag/payload sizes.
def _looks_like_ring(buf: bytes, i: int) -> bool:
    if i + 116 > len(buf):
        return False
    return (
        buf[i:i + 4] == b"VEC3"
        and buf[i + 28:i + 32] == b"VEC3"
        and buf[i + 56:i + 60] == b"VEC3"
        and buf[i + 84:i + 88] == b"U32_"
        and buf[i + 92:i + 96] == b"U32_"
        and buf[i + 100:i + 104] == b"U32_"
        and buf[i + 108:i + 112] == b"VEC3"
    )


def _module_name(s: str) -> bool:
    return s.endswith(("Editor", "Generator"))


# An OPEN editor contour whose first->last gap is within this multiple of the
# mean edge is a real un-snapped ring (safe to auto-close); a larger gap is a
# genuine open arc that closing would bridge with a fabricated chord (refused).
_CLOSE_MAX_RATIO = 1.5
_ON_OPEN = ("skip", "close", "error")


def _first_last_gap(points: np.ndarray) -> tuple[float, float, float]:
    """(gap, mean_edge, ratio) for the first->last chord of an open polyline."""
    edges = np.linalg.norm(np.diff(points, axis=0), axis=1)
    mean_edge = float(edges.mean()) if len(edges) else 0.0
    gap = float(np.linalg.norm(points[0] - points[-1]))
    ratio = gap / mean_edge if mean_edge > 0 else float("inf")
    return gap, mean_edge, ratio


def load_cso_checked(path: str | Path, *, on_open: str = "skip") -> tuple[str, CsoData]:
    """Read the file ONCE and derive BOTH the rule-1 checksum and the parse from
    the same buffer, so there is no window for the file to change between the
    checksum read and the parse read (F-INFO-8 / TOCTOU).  Returns
    ``(sha256_hexdigest, CsoData)``; the digest equals ``sha256(file bytes)`` (the
    same value ``cso_input_sha256`` computes)."""
    buf = Path(path).read_bytes()
    sha = hashlib.sha256(buf).hexdigest()
    data = load_cso(path, on_open=on_open, buf=buf)
    return sha, data


def load_cso(path: str | Path, *, on_open: str = "skip",
             buf: bytes | None = None) -> CsoData:
    """Parse a Binary CSO into ordered closed contours + guide tracks.

    ``on_open`` sets the policy for an OPEN ``SoCSOSplineEditor`` contour (a lumen
    ring the operator left un-closed — distinct from a ``CSOMarkerGenerator``
    guide track, which is always open by design):
      * ``skip`` (default): drop it with a loud note (record id + gap ratio) and
        build the surface from the remaining closed rings.
      * ``close``: promote it to a closed ring ONLY when its first->last gap is
        within ``_CLOSE_MAX_RATIO`` x the mean edge (a real un-snapped ring);
        otherwise refuse loudly rather than fabricate a chord across the mouth.
      * ``error``: refuse any open contour (the strict V38 behaviour).

    Still fails loudly regardless of ``on_open`` on genuinely corrupt data: an
    unknown tag, a truncated/corrupt file, a < 3-point contour, or no rings at
    all.  Closed contours are ordered along the centreline (projected onto a
    marker guide track when one is present, else a nearest-neighbour chain).
    """
    if on_open not in _ON_OPEN:
        raise ValueError(
            f"on_open must be one of {_ON_OPEN}, got {on_open!r}"
        )
    path = Path(path)
    # ``buf`` lets a caller (load_cso_checked) hand in the already-read bytes so
    # the file is read exactly once — checksum + parse from the same buffer.
    if buf is None:
        buf = path.read_bytes()
    if buf[:10] != _MAGIC:
        raise ValueError(
            f"{path.name} is not a Binary CSO (magic {buf[:10]!r} != "
            f"{_MAGIC!r}); XML CSOs are not supported"
        )
    if len(buf) < 13:
        raise ValueError(f"truncated CSO: {path.name} is only {len(buf)} bytes")
    version = tuple(buf[10:13])

    rd = _Reader(buf, 13)
    header_count = 0
    records: list[Contour] = []
    cur_module = ""
    cur_for = cur_series = cur_label = ""
    expect = None  # which STR_ value we are waiting for
    first_u32 = True

    while rd.pos + 4 <= len(buf):
        if _looks_like_ring(buf, rd.pos):
            contour = _read_ring(rd, len(records), cur_module,
                                 cur_for, cur_series, cur_label)
            records.append(contour)
            if len(records) > MAX_RECORDS:
                raise ValueError(
                    f"{path.name}: more than {MAX_RECORDS} CSO records — "
                    f"refusing (a real segmentation has a handful of contours; "
                    f"this many looks crafted / would stall the ordering)"
                )
            cur_module = ""  # module string belongs to one record
            cur_for = cur_series = cur_label = ""
            continue
        tag, val = rd.read_token()
        if tag == "U32_" and first_u32:
            header_count = int(val)
            first_u32 = False
        elif tag == "STR_":
            if expect == "for":
                cur_for = val
            elif expect == "series":
                cur_series = val
            elif expect == "module":
                cur_module = val
            elif expect == "label":
                cur_label = val
            elif val == "frameOfReferenceUID":
                expect = "for"; continue
            elif val == "seriesInstanceUID":
                expect = "series"; continue
            elif val == "moduleCreatorString":
                expect = "module"; continue
            elif val == "labelPrefixString":
                expect = "label"; continue
            elif _module_name(val):
                cur_module = val
            expect = None

    if not records:
        raise ValueError(
            f"{path.name}: no CSO records found — the file may be empty, a "
            f"different CSO version, or not a segmentation export"
        )

    contours: list[Contour] = []
    guides: list[Contour] = []
    notes: list[str] = []
    n_skipped = 0
    for rec in records:
        is_guide = rec.module.endswith("Generator") and not rec.closed
        if is_guide:
            guides.append(rec)
            continue
        if not rec.closed:
            gap, mean_edge, ratio = _first_last_gap(rec.points)
            desc = (
                f"record {rec.id} ({rec.module or 'unknown module'}, "
                f"{len(rec.points)} pts): first->last gap {gap:.2f} mm = "
                f"{ratio:.2f}x mean edge {mean_edge:.2f} mm"
            )
            if on_open == "error":
                raise ValueError(
                    f"CSO {desc} is an OPEN contour — a lumen boundary must be a "
                    f"closed ring; refusing to guess a closure (on_open='error')"
                )
            if on_open == "skip":
                notes.append(
                    f"skipped OPEN contour {desc} — an unclosed editor contour, "
                    f"dropped (on_open='skip')"
                )
                n_skipped += 1
                continue
            # on_open == "close": only promote a genuinely near-closed ring.
            if ratio > _CLOSE_MAX_RATIO:
                raise ValueError(
                    f"CSO {desc} is an OPEN contour whose gap is {ratio:.2f}x the "
                    f"mean edge (> {_CLOSE_MAX_RATIO}x) — too large to auto-close "
                    f"without fabricating a chord across the mouth; use "
                    f"on_open='skip' to drop it instead (on_open='close')"
                )
            notes.append(
                f"auto-closed near-closed OPEN contour {desc} "
                f"(<= {_CLOSE_MAX_RATIO}x mean edge, on_open='close')"
            )
            rec.closed = True  # _resample_closed / voxel stamp treat it as closed
        if len(rec.points) < 3:
            raise ValueError(
                f"CSO record {rec.id} has {len(rec.points)} point(s); a contour "
                f"needs at least 3"
            )
        contours.append(rec)

    if not contours:
        raise ValueError(
            f"{path.name}: found {len(records)} record(s) but no closed "
            f"segmentation contours (only guide/marker tracks"
            f"{f'; {n_skipped} open contour(s) skipped' if n_skipped else ''})"
        )
    if n_skipped:
        notes.append(
            f"built surface from {len(contours)} closed ring(s) after dropping "
            f"{n_skipped} open contour(s) (on_open='skip')"
        )

    ordered = _order_contours(contours, guides)
    if header_count and header_count != len(records):
        notes.append(
            f"header record count {header_count} != {len(records)} records "
            f"decoded (kept all decoded records)"
        )
    for_uid = next((c.frame_of_reference_uid for c in ordered
                    if c.frame_of_reference_uid), "")
    series_uid = next((c.series_instance_uid for c in ordered
                       if c.series_instance_uid), "")
    return CsoData(
        contours=ordered,
        guides=guides,
        version=version,  # type: ignore[arg-type]
        n_records=len(records),
        header_count=header_count,
        frame_of_reference_uid=for_uid,
        series_instance_uid=series_uid,
        notes=notes,
    )


def _read_ring(rd: _Reader, rec_id: int, module: str, for_uid: str,
               series_uid: str, label: str) -> Contour:
    """Consume a ring header + ``npts`` interleaved control points + the trailing
    interpolated-spline block, from a cursor positioned at the header."""
    _, normal = rd.read_token()          # plane normal
    rd.read_token()                       # bbox min
    rd.read_token()                       # bbox max
    _, _a = rd.read_token()               # U32_ (sub-type / seed count)
    _, _b = rd.read_token()               # U32_
    _, npts = rd.read_token()             # U32_ ring point count
    npts = int(npts)
    if npts < 1 or npts > 1_000_000:
        raise ValueError(f"CSO record {rec_id}: implausible point count {npts}")

    pts = np.empty((npts, 3), dtype=np.float64)
    prev0 = None
    for k in range(npts):
        tag, xyz = rd.read_token()
        if tag != "VEC3":
            raise ValueError(
                f"CSO record {rec_id}: expected control point {k} to be VEC3, "
                f"got {tag}"
            )
        pts[k] = xyz
        tp, prev = rd.read_token()
        tc, _cur = rd.read_token()
        if tp != "I32_" or tc != "I32_":
            raise ValueError(
                f"CSO record {rec_id}: control point {k} lacks its two I32_ "
                f"link indices (got {tp}, {tc})"
            )
        if k == 0:
            prev0 = int(prev)
    closed = prev0 == npts - 1  # first point's predecessor wraps to the last

    # A NaN/inf coordinate is byte-legal in the token stream but poisons every
    # downstream degeneracy guard (they are `> 0` / `< 1e-9` comparisons, and
    # NaN is False in all of them) — so it fails OPEN into a "watertight" mesh
    # full of NaN points (rule 6).  Reject it loudly at the source; this makes
    # the loft/voxel/centreline paths finite by construction.
    if not np.isfinite(pts).all():
        raise ValueError(
            f"CSO record {rec_id}: non-finite control-point coordinate "
            f"(NaN/inf) — refusing to build geometry from corrupt data"
        )
    # Finite is not enough: ~1e154+ coordinates overflow the loft arithmetic
    # into a NaN surface that still reports watertight (fail-open) — refuse
    # implausible magnitudes at the source like the NaN case above.
    peak = float(np.abs(pts).max())
    if peak > MAX_COORD:
        raise ValueError(
            f"CSO record {rec_id}: control-point coordinate magnitude "
            f"{peak:.3g} exceeds {MAX_COORD:g} (coordinates are millimetres; "
            f"values this large overflow geometry arithmetic into NaN) — "
            f"refusing to build geometry from corrupt data"
        )

    _consume_spline_block(rd, npts, closed, rec_id)

    n = np.asarray(normal, dtype=np.float64)
    if not np.isfinite(n).all():
        raise ValueError(
            f"CSO record {rec_id}: non-finite plane normal (NaN/inf)"
        )
    if float(np.abs(n).max()) > MAX_COORD:
        raise ValueError(
            f"CSO record {rec_id}: plane-normal magnitude "
            f"{float(np.abs(n).max()):.3g} exceeds {MAX_COORD:g} — its norm "
            f"overflows to inf and the normalisation would silently zero the "
            f"normal; refusing corrupt data"
        )
    nn = float(np.linalg.norm(n))
    if nn > 1e-12:
        n = n / nn
    lbl = f"{label or 'CSO'}{rec_id}"
    return Contour(points=pts, normal=n, closed=closed, id=rec_id, label=lbl,
                   module=module, frame_of_reference_uid=for_uid,
                   series_instance_uid=series_uid)


def _consume_spline_block(rd: _Reader, npts: int, closed: bool, rec_id: int) -> None:
    """A ring is followed by its interpolated spline: ``U32_ nseg`` then per
    segment ``U32_ k`` + k raw float64 triples + two ``I32_``. ``nseg`` is
    ``npts`` for a closed ring, ``npts-1`` for an open one — a strong signature
    that this really is the spline block. Absent/other -> leave the cursor."""
    save = rd.pos
    if rd.peek_tag() != b"U32_":
        return
    _, nseg = rd.read_token()
    if int(nseg) not in (npts, max(npts - 1, 0)):
        rd.pos = save  # not the spline block we expect
        return
    for _ in range(int(nseg)):
        if rd.peek_tag() != b"U32_":
            raise ValueError(
                f"CSO record {rec_id}: malformed spline block (expected a "
                f"segment point count at offset {rd.pos})"
            )
        _, k = rd.read_token()
        k = int(k)
        rd._need(k * 24, "spline segment points")
        rd.pos += k * 24  # raw float64 triples (interpolated, not needed)
        t1, _ = rd.read_token()
        t2, _ = rd.read_token()
        if t1 != "I32_" or t2 != "I32_":
            raise ValueError(
                f"CSO record {rec_id}: spline segment lacks its two I32_ "
                f"indices (got {t1}, {t2})"
            )


# ---------------------------------------------------------------------------
# ordering along the centreline
# ---------------------------------------------------------------------------
def _order_contours(contours: list[Contour], guides: list[Contour]) -> list[Contour]:
    if len(contours) <= 2:
        return list(contours)
    centroids = np.array([c.centroid for c in contours])
    guide = _longest_guide(guides)
    if guide is not None and len(guide) >= 2:
        params = np.array([_project_param(p, guide) for p in centroids])
        order = np.argsort(params)
    else:
        order = _nearest_neighbour_chain(centroids)
    return [contours[i] for i in order]


def _longest_guide(guides: list[Contour]) -> np.ndarray | None:
    best = None
    for g in guides:
        if best is None or len(g.points) > len(best):
            best = g.points
    return best


def _project_param(point: np.ndarray, poly: np.ndarray) -> float:
    """Arc-length parameter of the nearest point on a polyline to ``point``."""
    seg = np.diff(poly, axis=0)
    seg_len = np.linalg.norm(seg, axis=1)
    cum = np.concatenate([[0.0], np.cumsum(seg_len)])
    best_d = np.inf
    best_s = 0.0
    for k in range(len(seg)):
        L2 = float(seg[k] @ seg[k])
        if L2 <= 0:
            continue
        t = float((point - poly[k]) @ seg[k]) / L2
        t = min(max(t, 0.0), 1.0)
        proj = poly[k] + t * seg[k]
        d = float(np.linalg.norm(point - proj))
        if d < best_d:
            best_d = d
            best_s = cum[k] + t * seg_len[k]
    return best_s


def _nearest_neighbour_chain(centroids: np.ndarray) -> list[int]:
    """Greedy chain seeded from the PCA-extreme centroid — reconstructs the
    order of a non-branching tube when there is no guide track."""
    d = centroids - centroids.mean(0)
    _, _, Vt = np.linalg.svd(d, full_matrices=False)
    proj = d @ Vt[0]
    start = int(np.argmin(proj))
    remaining = set(range(len(centroids)))
    order = [start]
    remaining.discard(start)
    while remaining:
        last = centroids[order[-1]]
        nxt = min(remaining, key=lambda i: float(np.linalg.norm(centroids[i] - last)))
        order.append(nxt)
        remaining.discard(nxt)
    return order


# ---------------------------------------------------------------------------
# interior centreline from the contours themselves (V40)
# ---------------------------------------------------------------------------
def contour_centerline(
    contours,
    *,
    beta: float = 0.8,
    iters: int = 300,
    n_samples: int = 200,
    standoff: float = 0.5,
):
    """A smooth, guaranteed-interior centreline built from the ordered contours.

    The lofted surface's *skeleton* centreline (``geometry.compute_centerline``)
    is smooth but **exits** the lumen on the concave side of a tight bend (the
    smoothing spline cuts the corner) — the swept mesher then ray-misses.  The
    raw contour-centroid path is interior but too jagged and the sweep inverts.
    This builds the missing middle: smooth *and* interior by construction.

    The ordered contour centroids ``P0`` (import_cso already orders them along
    the guide) are Laplacian-smoothed, but every point is clamped each iteration
    to stay within ``beta`` x the contour's **inscribed radius** (min centroid ->
    ring distance) of its original centroid.  A point within a cross-section's
    inscribed radius of that section's centroid lies inside the section, so the
    smoothed path can never leave the lumen (``beta`` < 1 keeps a margin).  The
    result is resampled uniformly in arc length with an end ``standoff`` (a
    fraction of the local mean radius) so station 0 is not the cap-apex vertex,
    which makes the first sweep ray degenerate.

    Returns a ``geometry.centerline.Centerline``.  Fails loud (rule 6) on fewer
    than two contours or a degenerate (zero-perimeter / zero-radius) ring.
    """
    from scipy.interpolate import interp1d

    from meshforge.geometry.centerline import Centerline

    if isinstance(contours, CsoData):
        contours = contours.contours
    contours = list(contours)
    if len(contours) < 2:
        raise ValueError(
            f"contour_centerline needs at least 2 contours, got {len(contours)}"
        )
    if not 0.0 < beta < 1.0:
        raise ValueError(f"beta must be in (0,1), got {beta}")

    P0 = np.array([c.centroid for c in contours], dtype=np.float64)
    dists = [np.linalg.norm(c.points - c.centroid, axis=1) for c in contours]
    r_ins = np.array([float(d.min()) for d in dists])
    r_mean = np.array([float(d.mean()) for d in dists])
    if np.any(r_mean <= 0) or np.any(r_ins <= 0):
        raise ValueError("degenerate contour (zero radius) — cannot build a centreline")

    # Laplacian smoothing with a per-point interior clamp (ends pinned).
    Q = P0.copy()
    cap = beta * r_ins
    for _ in range(max(int(iters), 1)):
        Qn = Q.copy()
        Qn[1:-1] = 0.5 * Q[1:-1] + 0.25 * (Q[:-2] + Q[2:])
        disp = Qn - P0
        d = np.linalg.norm(disp, axis=1)
        over = d > cap
        if over.any():
            Qn[over] = P0[over] + disp[over] / d[over, None] * cap[over, None]
        Q = Qn

    # Uniform arc-length resample, trimmed by a standoff at each cap.
    seg = np.linalg.norm(np.diff(Q, axis=0), axis=1)
    cum = np.concatenate([[0.0], np.cumsum(seg)])
    length = float(cum[-1])
    if length <= 0:
        raise ValueError("contour centroids are coincident — cannot build a centreline")
    s0 = min(standoff * r_mean[0], 0.25 * length)
    s1 = max(length - standoff * r_mean[-1], 0.75 * length)
    tt = np.linspace(s0, s1, n_samples)
    points = np.column_stack([interp1d(cum, Q[:, k])(tt) for k in range(3)])
    deriv = np.gradient(points, tt, axis=0)
    tangents = deriv / np.linalg.norm(deriv, axis=1)[:, None]
    radii = interp1d(
        cum, r_mean, bounds_error=False, fill_value=(r_mean[0], r_mean[-1])
    )(tt)
    arc = tt - tt[0]
    return Centerline(
        points=points,
        tangents=tangents,
        arc_lengths=arc,
        radii=np.asarray(radii),
        length=float(arc[-1]),
        closed=False,
        voxel_size=0.0,
        notes=[
            f"contour centreline: {len(contours)} sections, "
            f"clamp beta={beta}, smoothing iters={iters}"
        ],
    )


# ---------------------------------------------------------------------------
# display passthrough
# ---------------------------------------------------------------------------
def contours_polydata(data: CsoData):
    """The raw parsed contour rings as ONE polyline ``pv.PolyData`` — a display
    PASSTHROUGH for the workbench QC overlay.  The points are the exact parsed
    control points (no resampling, no new geometry): each ring becomes one
    polyline cell, closed rings wrap back to their first point so the drawn
    loop matches what the operator segmented."""
    import pyvista as pv

    if not data.contours:
        raise ValueError("no contours to overlay")
    cells: list[int] = []
    offset = 0
    for c in data.contours:
        n = len(c.points)
        ids = list(range(offset, offset + n))
        if c.closed:
            ids.append(offset)
        cells.extend([len(ids), *ids])
        offset += n
    return pv.PolyData(
        np.vstack([c.points for c in data.contours]),
        lines=np.asarray(cells, dtype=np.int64),
    )


# ---------------------------------------------------------------------------
# surface construction
# ---------------------------------------------------------------------------
def cso_to_surface(
    contours,
    *,
    method: str = "loft",
    n_ring: int = 48,
    voxel_size: float | None = None,
    cap_ends: bool = True,
    grid: tuple | None = None,
):
    """Build a CLOSED triangulated PolyData from ordered closed contours.

    ``loft`` (default) skins consecutive rings into a tube: it resamples each
    ring to ``n_ring`` points aligned by twist-free parallel-transport frames,
    stitches adjacent rings, and caps the ends -> watertight by construction and
    ideal for cross-sections along a centreline.  ``voxel`` rasterises the closed
    contours into a shared label volume and reuses the image marching-cubes path
    (orientation-agnostic; better for branching / very oblique planes).

    ``grid`` (V39, voxel only) pins the label volume to an EXTERNAL image grid
    ``(dims, spacing, origin)`` — e.g. a DICOM series' — so the resulting mask
    and iso-surface share that image's voxel lattice (image-anchored
    segmentation) instead of a contour-bounding-box grid.  Returns
    ``(pv.PolyData, provenance dict)``.
    """
    if isinstance(contours, CsoData):
        contours = contours.contours
    contours = list(contours)
    if len(contours) < 2:
        raise ValueError(
            f"need at least 2 contours to build a surface, got {len(contours)}"
        )
    if grid is not None and method != "voxel":
        raise ValueError("grid anchoring requires method='voxel'")
    if method == "loft":
        return _loft_surface(contours, n_ring=n_ring, cap_ends=cap_ends)
    if method == "voxel":
        return _voxel_surface(contours, voxel_size=voxel_size, grid=grid)
    raise ValueError(f"unknown method {method!r}; use 'loft' or 'voxel'")


def _loft_surface(contours: list[Contour], *, n_ring: int, cap_ends: bool):
    import pyvista as pv

    from meshforge.geometry.frames import parallel_transport_frames

    if n_ring < 3:
        raise ValueError(f"n_ring must be >= 3, got {n_ring}")
    if n_ring > MAX_N_RING:
        raise ValueError(
            f"n_ring {n_ring} exceeds the maximum {MAX_N_RING} — refusing to "
            f"allocate a loft of that width (a real ring needs tens of points; "
            f"an unbounded value from a crafted work-file / CLI is a DoS)"
        )
    C = np.array([c.centroid for c in contours], dtype=np.float64)
    R = len(C)

    # central-difference tangents (one-sided at the ends)
    t = np.empty_like(C)
    t[1:-1] = C[2:] - C[:-2]
    t[0] = C[1] - C[0]
    t[-1] = C[-1] - C[-2]
    tn = np.linalg.norm(t, axis=1)
    if np.any(tn < 1e-9):
        raise ValueError(
            "coincident contour centroids — cannot loft (two rings share a "
            "centre); use method='voxel'"
        )
    t = t / tn[:, None]
    normals, binormals = parallel_transport_frames(t, closed=False)

    rings = [
        _resample_closed(contours[i].points, n_ring, normals[i], binormals[i], C[i])
        for i in range(R)
    ]
    pts = np.vstack(rings)

    faces: list[list[int]] = []
    for i in range(R - 1):
        base0 = i * n_ring
        base1 = (i + 1) * n_ring
        for j in range(n_ring):
            j1 = (j + 1) % n_ring
            a, b = base0 + j, base0 + j1
            c, d = base1 + j1, base1 + j
            faces.append([3, a, b, c])
            faces.append([3, a, c, d])

    if cap_ends:
        cb = len(pts)
        ct = len(pts) + 1
        pts = np.vstack([pts, C[0][None, :], C[-1][None, :]])
        last = (R - 1) * n_ring
        for j in range(n_ring):
            j1 = (j + 1) % n_ring
            faces.append([3, cb, j1, j])           # bottom cap (outward = -t0)
            faces.append([3, ct, last + j, last + j1])  # top cap (outward = +t_end)

    surface = pv.PolyData(pts, np.array(faces, dtype=np.int64).ravel())
    surface = surface.clean()
    surface = _orient_outward(surface)

    watertight = int(surface.n_open_edges) == 0
    notes: list[str] = []
    if not watertight:
        notes.append(
            f"lofted surface is not watertight ({surface.n_open_edges} open "
            f"edge(s)); increase n_ring or use method='voxel'"
        )
    provenance = {
        "method": "loft",
        "n_contours": R,
        "n_ring": n_ring,
        "cap_ends": bool(cap_ends),
        "n_points": int(surface.n_points),
        "n_triangles": int(surface.n_cells),
        "watertight": watertight,
        "bounds": [float(v) for v in surface.bounds],
        "notes": notes,
    }
    return surface, provenance


def _resample_closed(points: np.ndarray, n: int, u: np.ndarray, v: np.ndarray,
                     centroid: np.ndarray) -> np.ndarray:
    """Resample a closed ring to ``n`` points by uniform arc length, with a
    consistent winding and a start point aligned to angle 0 in the (u, v) frame
    — so corresponding indices line up across rings (twist-free loft)."""
    P = np.asarray(points, dtype=np.float64)
    c = np.asarray(centroid, dtype=np.float64)
    d = P - c
    a = d @ u
    b = d @ v
    area = 0.5 * float(np.sum(a * np.roll(b, -1) - np.roll(a, -1) * b))
    if area < 0:  # enforce CCW in (u, v) so all rings share orientation
        P = P[::-1]
    loop = np.vstack([P, P[:1]])
    seg = np.linalg.norm(np.diff(loop, axis=0), axis=1)
    total = float(seg.sum())
    if total <= 0:
        raise ValueError("degenerate contour (zero perimeter)")
    cum = np.concatenate([[0.0], np.cumsum(seg)])
    targets = np.arange(n) / n * total
    idx = np.clip(np.searchsorted(cum, targets, side="right") - 1, 0, len(seg) - 1)
    frac = (targets - cum[idx]) / np.where(seg[idx] > 0, seg[idx], 1.0)
    out = loop[idx] * (1 - frac)[:, None] + loop[idx + 1] * frac[:, None]
    do = out - c
    ang = np.arctan2(do @ v, do @ u)
    j0 = int(np.argmin(np.abs(ang)))
    return np.roll(out, -j0, axis=0)


def _orient_outward(surface):
    """Flip faces so the signed enclosed volume is positive (outward normals),
    matching the image ingest's convention."""
    p = np.asarray(surface.points)
    tri = surface.faces.reshape(-1, 4)[:, 1:]
    signed = float(
        np.einsum("ij,ij->i", p[tri[:, 0]], np.cross(p[tri[:, 1]], p[tri[:, 2]])).sum()
        / 6.0
    )
    if signed < 0:
        if hasattr(surface, "flip_faces"):
            surface = surface.flip_faces()
        else:  # pragma: no cover - older pyvista
            surface.flip_normals()
    return surface


def _voxel_surface(contours: list[Contour], *, voxel_size: float | None,
                   grid: tuple | None = None):
    """Rasterise each closed contour as a filled polygon into a shared label
    volume (each slab thick enough to meet its neighbours) and reuse the image
    marching-cubes path.  Orientation-agnostic; the fallback for branching or
    strongly oblique planes.  When ``grid=(dims, spacing, origin)`` is given the
    volume is pinned to that external (e.g. DICOM) image lattice so the mask is
    image-anchored; otherwise a padded contour-bounding-box grid is built."""
    from meshforge.geometry.centerline import MAX_VOXELS
    from meshforge.io.image import volume_to_surface

    C = np.array([c.centroid for c in contours], dtype=np.float64)
    gaps = np.linalg.norm(np.diff(C, axis=0), axis=1)

    if grid is not None:
        dims_full, spacing, origin_full = grid
        spacing = np.asarray(spacing, dtype=np.float64)
        origin_full = np.asarray(origin_full, dtype=np.float64)
        dims_full = np.asarray([int(d) for d in dims_full])
        if dims_full.shape != (3,) or spacing.shape != (3,) or origin_full.shape != (3,):
            raise ValueError("grid must be (dims[3], spacing[3], origin[3])")
        if np.any(spacing <= 0):
            raise ValueError(f"grid spacing must be positive, got {list(spacing)}")
        # every contour point must fall inside the image grid (shared world frame)
        allpts = np.vstack([c.points for c in contours])
        idx = (allpts - origin_full) / spacing
        if idx.min() < -0.5 or np.any(idx.max(0) > dims_full - 0.5):
            raise ValueError(
                "CSO contours fall outside the image grid — they do not share "
                "the image's frame of reference; refusing to anchor"
            )
        slab = float(gaps.max()) if len(gaps) else 4 * float(spacing.max())
        # Crop to the segmentation ROI on the SAME lattice (a whole CT is far
        # over MAX_VOXELS): the sub-grid origin is an integer voxel offset so
        # the mask still lands on the image's voxel centres.
        pad = np.ceil(slab / spacing).astype(int) + 3
        i0 = np.maximum(np.floor(idx.min(0)).astype(int) - pad, 0)
        i1 = np.minimum(np.ceil(idx.max(0)).astype(int) + pad, dims_full - 1)
        lo = origin_full + i0 * spacing
        dims = tuple(int(d) for d in (i1 - i0 + 1))
        if int(np.prod(dims)) > MAX_VOXELS:
            raise ValueError(
                f"image ROI grid {dims} exceeds {MAX_VOXELS} voxels; the "
                f"segmentation spans too much of the scan"
            )
    else:
        allpts = np.vstack([c.points for c in contours])
        lo0, hi0 = allpts.min(0), allpts.max(0)
        diag = float(np.linalg.norm(hi0 - lo0))
        if voxel_size is None or voxel_size <= 0:
            voxel_size = diag / 120.0
        spacing = np.array([voxel_size] * 3, dtype=np.float64)
        # slab half-thickness reaches at least the nearest neighbouring ring so
        # consecutive filled disks overlap into a connected tube.
        slab = float(gaps.max()) if len(gaps) else 4 * voxel_size
        pad = 2 * voxel_size + slab
        lo = lo0 - pad
        hi = hi0 + pad
        dims = tuple(int(d) for d in (np.ceil((hi - lo) / voxel_size).astype(int) + 1))
        if int(np.prod(dims)) > MAX_VOXELS:
            raise ValueError(
                f"voxel grid {dims} exceeds {MAX_VOXELS} voxels; increase voxel_size"
            )

    vol = np.zeros(dims, dtype=bool)
    for i, cont in enumerate(contours):
        nb = gaps[max(i - 1, 0)] if len(gaps) else slab
        nf = gaps[min(i, len(gaps) - 1)] if len(gaps) else slab
        half = max(float(max(nb, nf)) * 0.6, float(spacing.max()))
        _stamp_contour(vol, cont, lo, spacing, half)

    if not vol.any():
        raise ValueError("voxelised contours produced an empty volume")
    surface, prov = volume_to_surface(
        vol.astype(np.float32), 0.5, spacing=tuple(spacing), origin=tuple(lo)
    )
    prov = {
        "method": "voxel",
        "n_contours": len(contours),
        "voxel_size": float(spacing[0]) if len(set(spacing.tolist())) == 1 else None,
        "spacing": [float(s) for s in spacing],
        "dims": [int(d) for d in dims],
        "origin": [float(v) for v in lo],
        "image_anchored": grid is not None,
        "n_points": int(surface.n_points),
        "n_triangles": int(surface.n_cells),
        "watertight": bool(prov["watertight"]),
        "notes": prov["notes"],
    }
    return surface, prov


def _stamp_contour(vol, cont: Contour, origin, spacing, half: float) -> None:
    """Mark every voxel within ``half`` mm of the contour plane whose in-plane
    projection is inside the (closed) polygon.  ``spacing`` is the per-axis voxel
    size (an image grid may be anisotropic)."""
    spacing = np.asarray(spacing, dtype=np.float64)
    origin = np.asarray(origin, dtype=np.float64)
    P = cont.points
    c = P.mean(0)
    nrm = cont.normal
    # in-plane basis
    a0 = np.eye(3)[int(np.argmin(np.abs(nrm)))]
    u = a0 - (a0 @ nrm) * nrm
    u = u / np.linalg.norm(u)
    w = np.cross(nrm, u)
    poly2d = np.column_stack([(P - c) @ u, (P - c) @ w])

    # voxel bbox around the contour, expanded by half + polygon radius
    rad = float(np.linalg.norm(P - c, axis=1).max())
    reach = half + rad + float(spacing.max())
    lo_i = np.floor((c - reach - origin) / spacing).astype(int)
    hi_i = np.ceil((c + reach - origin) / spacing).astype(int)
    lo_i = np.maximum(lo_i, 0)
    hi_i = np.minimum(hi_i, np.array(vol.shape) - 1)
    if np.any(hi_i < lo_i):
        return
    xs = (origin[0] + np.arange(lo_i[0], hi_i[0] + 1) * spacing[0])
    ys = (origin[1] + np.arange(lo_i[1], hi_i[1] + 1) * spacing[1])
    zs = (origin[2] + np.arange(lo_i[2], hi_i[2] + 1) * spacing[2])
    gx, gy, gz = np.meshgrid(xs, ys, zs, indexing="ij")
    pts = np.column_stack([gx.ravel(), gy.ravel(), gz.ravel()])
    rel = pts - c
    dist = rel @ nrm
    near = np.abs(dist) <= half
    if not near.any():
        return
    proj = np.column_stack([rel[near] @ u, rel[near] @ w])
    inside = _points_in_polygon(proj, poly2d)
    sel = np.zeros(len(pts), dtype=bool)
    sel[np.where(near)[0][inside]] = True
    if not sel.any():
        return
    ijk = pts[sel]
    ii = np.round((ijk[:, 0] - origin[0]) / spacing[0]).astype(int)
    jj = np.round((ijk[:, 1] - origin[1]) / spacing[1]).astype(int)
    kk = np.round((ijk[:, 2] - origin[2]) / spacing[2]).astype(int)
    vol[ii, jj, kk] = True


def _points_in_polygon(pts2d: np.ndarray, poly: np.ndarray) -> np.ndarray:
    """Even-odd ray-cast point-in-polygon for a batch of 2-D points."""
    x = pts2d[:, 0]
    y = pts2d[:, 1]
    inside = np.zeros(len(pts2d), dtype=bool)
    n = len(poly)
    j = n - 1
    for i in range(n):
        xi, yi = poly[i]
        xj, yj = poly[j]
        cond = ((yi > y) != (yj > y)) & (
            x < (xj - xi) * (y - yi) / np.where((yj - yi) == 0, 1e-30, (yj - yi)) + xi
        )
        inside ^= cond
        j = i
    return inside

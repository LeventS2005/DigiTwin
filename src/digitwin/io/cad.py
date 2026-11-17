"""CAD import with automatic clean-up: DXF/DWG drawings -> Profile2D.

TrueGrid-style "Eliminate Clean-up": supported entities are flattened to
polylines, loose/reversed/duplicated segments are chained into closed loops,
and loops are classified into outers + holes. Chains that fail to close are
reported loudly (free endpoints + gap distances), never silently dropped.

Optional deps: ezdxf (pip extra ``digitwin[cad]``) is lazy-imported; DWG
files additionally need the external ``dwg2dxf`` converter (GNU LibreDWG,
e.g. ``brew install libredwg``) on PATH.
"""
from __future__ import annotations

import json
import math
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from digitwin.io.load import file_sha256
from digitwin.textguard import scrub

SUPPORTED_DXFTYPES = ("LWPOLYLINE", "LINE", "ARC", "CIRCLE", "POLYLINE")
MODELSPACE = "<modelspace>"

# $INSUNITS code -> unit name. Values are carried as a string, never converted.
_INSUNITS_NAMES = {
    0: "unitless",
    1: "in",
    2: "ft",
    3: "mi",
    4: "mm",
    5: "cm",
    6: "m",
    7: "km",
    8: "microinch",
    9: "mil",
    10: "yd",
    11: "angstrom",
    12: "nm",
    13: "um",
    14: "dm",
    15: "dam",
    16: "hm",
    17: "gm",
}

_CHAIN_TOL_FACTOR = 1e-6  # default chain_tol = 1e-6 x bbox diagonal (spec)
_ARC_TOL_FACTOR = 1e-4  # default arc_tol = 1e-4 x bbox diagonal
_DEGEN_AREA_FACTOR = 1e-12  # loops with |area| < 1e-12 x diag^2 are dropped
_MAX_JUNCTION_NOTES = 20


@dataclass
class Profile2D:
    """Closed planar loops recovered from a 2-D CAD drawing.

    ``loops`` are (N, 2) float64 arrays with implicit closure (first vertex is
    NOT repeated at the end). Outers are CCW (signed area > 0), holes CW.
    ``components`` groups loops: one dict per disjoint outer with its holes,
    sorted by |outer area| descending. Units are native drawing units — never
    silently converted.
    """

    loops: list[np.ndarray]
    components: list[dict]
    units: str
    provenance: dict
    mesh_ready: bool

    @property
    def n_outers(self) -> int:
        return len(self.components)

    @property
    def n_holes(self) -> int:
        return sum(len(c["holes"]) for c in self.components)

    def bbox(self) -> tuple[np.ndarray, np.ndarray]:
        """(min_xy, max_xy) over all closed loops."""
        if not self.loops:
            raise ValueError("profile has no closed loops; no bbox")
        allpts = np.vstack(self.loops)
        return allpts.min(axis=0), allpts.max(axis=0)

    def to_json(self) -> str:
        return json.dumps(
            {
                "loops": [lp.tolist() for lp in self.loops],
                "components": self.components,
                "units": self.units,
                "mesh_ready": self.mesh_ready,
                "provenance": self.provenance,
            },
            indent=2,
        )

    def to_text(self) -> str:
        p = self.provenance
        lines = [
            f"profile     : {p.get('source', '?')}",
            f"block       : {p.get('block', '?')}  (format: {p.get('format', '?')})",
            f"units       : {self.units}  (INSUNITS={p.get('insunits')})",
            f"loops       : {len(self.loops)} closed "
            f"({self.n_outers} outer, {self.n_holes} hole)  "
            f"mode: {p.get('mode', '?')}",
        ]
        if self.loops:
            lo, hi = self.bbox()
            lines.append(
                f"bbox        : {hi[0] - lo[0]:.6g} x {hi[1] - lo[1]:.6g}  "
                f"[{lo[0]:.6g}, {lo[1]:.6g}] .. [{hi[0]:.6g}, {hi[1]:.6g}]"
            )
        lines.append(
            f"tolerances  : chain_tol={p.get('chain_tol'):.6g}  "
            f"arc_tol={p.get('arc_tol'):.6g}"
        )
        lines.append(f"mesh_ready  : {self.mesh_ready}")
        for note in p.get("notes", []):
            lines.append(f"note        : {note}")
        return "\n".join(lines) + "\n"


def load_profile(
    path: str | Path,
    *,
    block: str | None = None,
    chain_tol: float | None = None,
    arc_tol: float | None = None,
    layers: list[str] | None = None,
    mode: str = "auto",
) -> Profile2D:
    """Load a 2-D DXF/DWG drawing as a cleaned-up :class:`Profile2D`.

    ``block=None`` uses modelspace, falling back to the largest named block
    when modelspace has no supported entities. ``chain_tol`` defaults to
    1e-6 x bbox diagonal, ``arc_tol`` (max sagitta for curve flattening) to
    1e-4 x bbox diagonal. ``layers`` restricts import to the named layers.

    ``mode``:
      - ``"chain"``: endpoint chaining only — loose/reversed segments are
        joined into closed loops, classified outer/hole by even-odd depth.
        Right for dirty part outlines (each drawn line bounds one region).
      - ``"faces"``: planar-subdivision face extraction — segments are split
        at T-junctions and every enclosed region becomes a component (shared
        edges bound two regions). Right for architectural/tiled drawings.
      - ``"auto"`` (default): chaining first; if chains remain unclosed at
        junctions, face extraction is attempted and used when it succeeds.
    """
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"CAD file not found: {path}")
    if mode not in ("auto", "chain", "faces"):
        raise ValueError(f"mode must be 'auto', 'chain' or 'faces', got {mode!r}")
    sha256 = file_sha256(path)

    notes: list[str] = []
    counts: dict = {}
    doc, fmt = _open_document(path, notes, counts)
    units, insunits = _read_units(doc, notes)
    space, block_name = _select_space(doc, block, notes)
    paths = _gather_paths(space, layers, notes, counts)
    if not paths:
        raise ValueError(
            f"no supported entities to import from {block_name!r} in {path.name} "
            f"(supported: {', '.join(SUPPORTED_DXFTYPES)})"
        )

    # Tolerances derive from the pre-flattening bbox (control vertices).
    verts = np.array(
        [(v.x, v.y) for p in paths for v in p.control_vertices()], dtype=float
    )
    lo, hi = verts.min(axis=0), verts.max(axis=0)
    diag = float(np.hypot(hi[0] - lo[0], hi[1] - lo[1]))
    if diag <= 0.0:
        raise ValueError(f"degenerate geometry (zero-size bounding box) in {path.name}")
    if chain_tol is None:
        chain_tol = _CHAIN_TOL_FACTOR * diag
    if arc_tol is None:
        arc_tol = _ARC_TOL_FACTOR * diag
    if chain_tol <= 0 or arc_tol <= 0:
        raise ValueError(
            f"tolerances must be positive: chain_tol={chain_tol}, arc_tol={arc_tol}"
        )

    closed, open_chains = _flatten_paths(paths, arc_tol, diag, notes, counts)
    open_chains = _drop_duplicates(open_chains, notes, counts)

    mode_used = mode
    if mode == "faces":
        loops, components, open_records = _extract_faces(
            closed, open_chains, chain_tol, diag, notes, counts
        )
        if not loops:
            raise ValueError(
                "face extraction found no enclosed regions "
                f"in {block_name!r} of {path.name} (see notes)"
            )
    else:
        chain_notes: list[str] = []
        loops, open_records = _chain_segments(
            closed, open_chains, chain_tol, diag, chain_notes, counts
        )
        loops, components = _classify_loops(loops, diag, chain_notes, counts)
        mode_used = "chain"
        if mode == "auto" and open_records:
            f_notes: list[str] = []
            f_counts: dict = {}
            f_loops, f_components, f_open = _extract_faces(
                closed, open_chains, chain_tol, diag, f_notes, f_counts
            )
            if f_loops:
                notes.extend(f_notes)
                counts.update(f_counts)
                # Chain-phase diagnostics are superseded — summarize them.
                notes.append(
                    f"endpoint chaining left {len(open_records)} chain(s) "
                    "unclosed at junctions; switched to planar face "
                    f"extraction ({len(f_components)} region(s))"
                )
                loops, components, open_records = f_loops, f_components, f_open
                mode_used = "faces"
            else:
                notes.extend(chain_notes)
                notes.append(
                    "face extraction found no enclosed regions either; "
                    "keeping chained result"
                )
        else:
            notes.extend(chain_notes)
    counts["loops"] = len(loops)
    counts["mode"] = mode_used

    if not loops and not open_records:
        raise ValueError(
            f"no usable geometry in {block_name!r} of {path.name}: every entity "
            "was degenerate or dropped (see notes)"
        )

    mesh_ready = bool(loops) and not open_records
    provenance = {
        "source": str(path),
        "sha256": sha256,
        "format": fmt,
        "block": block_name,
        "insunits": insunits,
        "mode": mode_used,
        "chain_tol": float(chain_tol),
        "arc_tol": float(arc_tol),
        "notes": notes,
        "open_chains": open_records,
        "counts": counts,
    }
    return Profile2D(
        loops=loops,
        components=components,
        units=units,
        provenance=provenance,
        mesh_ready=mesh_ready,
    )


def list_blocks(path: str | Path) -> list[dict]:
    """List modelspace + named (non-anonymous) blocks with entity counts."""
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"CAD file not found: {path}")
    notes: list[str] = []
    counts: dict = {}
    doc, _fmt = _open_document(path, notes, counts)

    def entry(name: str, space) -> dict:
        types: dict[str, int] = {}
        n_supported = 0
        for e in space:
            t = e.dxftype()
            types[t] = types.get(t, 0) + 1
            if t in SUPPORTED_DXFTYPES:
                n_supported += 1
        return {
            "name": name,
            "n_entities": sum(types.values()),
            "n_supported": n_supported,
            "types": types,
        }

    entries = [entry(MODELSPACE, doc.modelspace())]
    for b in _named_blocks(doc):
        entries.append(entry(b.name, b))
    return entries


# ---------------------------------------------------------------------------
# document opening + repair


def _repair_dxf_text(text: str) -> tuple[str, int]:
    """Re-join value lines that were split by raw newlines.

    DXF is strictly alternating (group-code line, value line). A raw newline
    inside a value (dwg2dxf emits this for MTEXT in R2018 output) pushes text
    into a code position and ezdxf rejects the whole file. Any code-position
    line that does not parse as an integer is appended to the previous value
    line with the MTEXT paragraph separator ``\\P``.
    """
    out: list[str] = []
    merged = 0
    expect_code = True
    for line in text.splitlines():
        if expect_code:
            try:
                int(line.strip())
            except ValueError:
                if out:
                    out[-1] = out[-1] + r"\P" + line
                    merged += 1
                    continue  # next line should be the real group code
            out.append(line)
            expect_code = False
        else:
            out.append(line)
            expect_code = True
    return "\n".join(out) + "\n", merged


def _recover_read(dxf_path: Path, notes: list[str], counts: dict):
    from ezdxf import recover

    doc, auditor = recover.readfile(str(dxf_path))
    counts["recover_fixes"] = len(auditor.fixes)
    counts["recover_errors"] = len(auditor.errors)
    if auditor.has_errors:
        notes.append(
            f"recover reported {len(auditor.errors)} unrecoverable error(s); "
            "geometry may be incomplete"
        )
    return doc


def _open_document(path: Path, notes: list[str], counts: dict):
    import ezdxf
    from ezdxf.lldxf.const import DXFStructureError

    suffix = path.suffix.lower()
    if suffix == ".dwg":
        exe = shutil.which("dwg2dxf")
        if exe is None:
            raise RuntimeError(
                "DWG import needs the external converter 'dwg2dxf' (GNU LibreDWG); "
                "install it (e.g. `brew install libredwg`) or convert to DXF first"
            )
        with tempfile.TemporaryDirectory(prefix="digitwin_cad_") as td:
            out = Path(td) / (path.stem + ".dxf")
            # NEVER `--as r2000`: LibreDWG silently drops every entity (D14).
            # F3: cap the run — dwg2dxf drives GNU LibreDWG, a C parser of
            # hostile binary CAD (the highest-risk parser in the tree); a
            # malformed .dwg that hangs it would otherwise hang DigiTwin
            # forever. Mirror the solver runners' loud TimeoutExpired handling.
            try:
                proc = subprocess.run(
                    [exe, "-o", str(out), str(path)],
                    capture_output=True, text=True, timeout=120,
                )
            except subprocess.TimeoutExpired as exc:
                raise RuntimeError(
                    f"dwg2dxf exceeded the 120s timeout on {path.name} — the "
                    f"DWG may be malformed or adversarial (LibreDWG hung)"
                ) from exc
            if proc.returncode != 0 or not out.is_file():
                raise RuntimeError(
                    f"dwg2dxf failed (exit {proc.returncode}) on {path.name}: "
                    f"{scrub(proc.stderr.strip()[-500:])}"
                )
            repaired, merged = _repair_dxf_text(
                out.read_text(encoding="utf-8", errors="replace")
            )
            counts["merged_lines"] = merged
            if merged:
                notes.append(
                    f"repaired {merged} value line(s) broken by raw newlines "
                    "in the converted DXF"
                )
            fixed = Path(td) / (path.stem + "_repaired.dxf")
            fixed.write_text(repaired, encoding="utf-8")
            return _recover_read(fixed, notes, counts), "dwg"
    if suffix == ".dxf":
        try:
            return ezdxf.readfile(str(path)), "dxf"
        except DXFStructureError:
            with tempfile.TemporaryDirectory(prefix="digitwin_cad_") as td:
                repaired, merged = _repair_dxf_text(
                    path.read_text(encoding="utf-8", errors="replace")
                )
                counts["merged_lines"] = merged
                notes.append(
                    "plain DXF read failed (DXFStructureError); applied line-join "
                    f"repair ({merged} merged line(s)) + recover"
                )
                fixed = Path(td) / (path.stem + "_repaired.dxf")
                fixed.write_text(repaired, encoding="utf-8")
                return _recover_read(fixed, notes, counts), "dxf"
    raise ValueError(
        f"unsupported CAD format {path.suffix!r} (expected .dxf or .dwg): {path.name}"
    )


def _read_units(doc, notes: list[str]) -> tuple[str, int | None]:
    try:
        code = int(doc.header["$INSUNITS"])
    except KeyError:
        notes.append("no $INSUNITS header entry — units unknown, using 'unitless'")
        return "unitless", None
    name = _INSUNITS_NAMES.get(code)
    if name is None:
        notes.append(f"unrecognized $INSUNITS code {code} — carrying as-is")
        return f"unknown({code})", code
    if code == 0:
        notes.append("drawing declares no units ($INSUNITS=0)")
    return name, code


# ---------------------------------------------------------------------------
# block selection + entity flattening


def _named_blocks(doc) -> list:
    # Anonymous/internal blocks (*U.., *X.., *Model_Space, ...) all start
    # with "*" and duplicate content — excluded from listing and auto-select.
    return [b for b in doc.blocks if not b.name.startswith("*")]


def _supported_count(space) -> int:
    return sum(1 for e in space if e.dxftype() in SUPPORTED_DXFTYPES)


def _select_space(doc, block: str | None, notes: list[str]):
    msp = doc.modelspace()
    if block is None:
        if _supported_count(msp) > 0:
            return msp, MODELSPACE
        cands = [
            (b, n) for b in _named_blocks(doc) if (n := _supported_count(b)) > 0
        ]
        if not cands:
            raise ValueError(
                "no supported entities in modelspace or any named block "
                f"(supported: {', '.join(SUPPORTED_DXFTYPES)})"
            )
        best, n = max(cands, key=lambda t: (t[1], t[0].name))
        notes.append(
            "modelspace has no supported entities; auto-selected block "
            f"{best.name!r} ({n} supported entities)"
        )
        return best, best.name
    if block == MODELSPACE:
        return msp, MODELSPACE
    if block in doc.blocks:
        return doc.blocks.get(block), block
    candidates = [MODELSPACE] + [b.name for b in _named_blocks(doc)]
    raise ValueError(f"block {block!r} not found; available: {candidates}")


def _gather_paths(space, layers: list[str] | None, notes: list[str], counts: dict):
    from ezdxf.path import make_path

    layer_set = set(layers) if layers is not None else None
    unsupported: dict[str, int] = {}
    skipped_layers = 0
    failed = 0
    paths = []
    for e in space:
        t = e.dxftype()
        if t not in SUPPORTED_DXFTYPES:
            unsupported[t] = unsupported.get(t, 0) + 1
            continue
        if layer_set is not None and e.dxf.layer not in layer_set:
            skipped_layers += 1
            continue
        try:
            p = make_path(e)
        except Exception as exc:  # degenerate entity — skip loudly, keep going
            failed += 1
            notes.append(f"skipped {t}: make_path failed ({exc})")
            continue
        if len(p) == 0:
            failed += 1
            continue
        paths.append(p)
    counts["entities_supported"] = len(paths)
    counts["entities_unsupported"] = unsupported
    counts["entities_failed"] = failed
    if unsupported:
        summary = ", ".join(f"{k} x{v}" for k, v in sorted(unsupported.items()))
        notes.append(f"skipped unsupported entities: {summary}")
    if layer_set is not None:
        notes.append(
            f"layer filter {sorted(layer_set)}: skipped {skipped_layers} entities"
        )
    return paths


def _flatten_paths(paths, arc_tol: float, diag: float, notes: list[str], counts: dict):
    """Flatten ezdxf paths to 2-D polylines: (closed_loops, open_chains)."""
    closed: list[np.ndarray] = []
    open_chains: list[np.ndarray] = []
    degenerate = 0
    zmax = 0.0
    for p in paths:
        pts3 = np.array([(v.x, v.y, v.z) for v in p.flattening(arc_tol)], dtype=float)
        if len(pts3) == 0:
            degenerate += 1
            continue
        zmax = max(zmax, float(np.abs(pts3[:, 2]).max()))
        pts = pts3[:, :2]
        if len(pts) > 1:  # drop consecutive duplicates from flattening joins
            step = np.linalg.norm(np.diff(pts, axis=0), axis=1)
            keep = np.concatenate([[True], step > 1e-12 * diag])
            pts = pts[keep]
        if p.is_closed:
            if len(pts) > 1 and np.linalg.norm(pts[0] - pts[-1]) <= 1e-9 * diag:
                pts = pts[:-1]
            if len(pts) >= 3:
                closed.append(pts)
            else:
                degenerate += 1
        else:
            if len(pts) >= 2:
                open_chains.append(pts)
            else:
                degenerate += 1
    if zmax > 1e-9 * diag:
        notes.append(f"non-zero Z (max |z| = {zmax:.4g}) flattened onto the XY plane")
    counts["paths_closed"] = len(closed)
    counts["chains_open_input"] = len(open_chains)
    counts["degenerate_dropped"] = degenerate
    if degenerate:
        notes.append(f"dropped {degenerate} degenerate entity path(s)")
    return closed, open_chains


# ---------------------------------------------------------------------------
# clean-up chaining


def _cluster_endpoints(chains: list[np.ndarray], tol: float):
    """Union-find endpoint clusters within tol; returns (labels, centroids)."""
    from scipy.spatial import cKDTree

    endpoints = np.array([pt for c in chains for pt in (c[0], c[-1])], dtype=float)
    parent = np.arange(len(endpoints))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return int(i)

    for i, j in cKDTree(endpoints).query_pairs(tol):
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[max(ri, rj)] = min(ri, rj)

    roots = np.array([find(i) for i in range(len(endpoints))])
    unique, labels = np.unique(roots, return_inverse=True)
    centroids = np.array(
        [endpoints[roots == r].mean(axis=0) for r in unique], dtype=float
    )
    return labels, centroids


def _last_direction(pts_list: list[np.ndarray]) -> np.ndarray | None:
    """Unit direction of the final segment of an assembled point path."""
    flat = np.vstack(pts_list)
    for k in range(len(flat) - 1, 0, -1):
        d = flat[k] - flat[k - 1]
        n = float(np.linalg.norm(d))
        if n > 0:
            return d / n
    return None


def _first_direction(seg: np.ndarray) -> np.ndarray | None:
    for k in range(1, len(seg)):
        d = seg[k] - seg[0]
        n = float(np.linalg.norm(d))
        if n > 0:
            return d / n
    return None


def _extend(
    pts_list: list[np.ndarray],
    cur: int,
    target: int,
    adj: dict[int, list[tuple[int, int]]],
    chains: list[dict],
    used: list[bool],
    junction_notes: list[str],
) -> tuple[int, bool]:
    """Append unused chains from node ``cur`` until closure at ``target`` or a
    dead end. Returns (final_node, closed)."""
    while True:
        if cur == target:
            return cur, True
        cands = [(ci, e) for ci, e in adj.get(cur, []) if not used[ci]]
        if not cands:
            return cur, False
        if len(cands) == 1:
            ci, e = cands[0]
        else:
            # Junction (>2 incident chains): keep the best direction
            # continuation (max dot of unit in/out vectors).
            in_dir = _last_direction(pts_list)
            best, best_dot = cands[0], -np.inf
            for cand in cands:
                seg = chains[cand[0]]["pts"]
                seg = seg if cand[1] == 0 else seg[::-1]
                out_dir = _first_direction(seg)
                dot = (
                    float(np.dot(in_dir, out_dir))
                    if in_dir is not None and out_dir is not None
                    else -np.inf
                )
                if dot > best_dot:
                    best, best_dot = cand, dot
            ci, e = best
            node_xy = np.vstack(pts_list)[-1]
            junction_notes.append(
                f"junction at ({node_xy[0]:.6g}, {node_xy[1]:.6g}): "
                f"{len(cands)} candidates, took best direction continuation"
            )
        seg = chains[ci]["pts"] if e == 0 else chains[ci]["pts"][::-1]
        pts_list.append(seg[1:])
        used[ci] = True
        cur = chains[ci]["b"] if e == 0 else chains[ci]["a"]


def _drop_duplicates(
    open_chains: list[np.ndarray], notes: list[str], counts: dict
) -> list[np.ndarray]:
    """Exact duplicate open chains (forward or reversed) are drawing noise."""
    seen: set[bytes] = set()
    uniq: list[np.ndarray] = []
    dropped = 0
    for pts in open_chains:
        k1, k2 = pts.tobytes(), pts[::-1].tobytes()
        if k1 in seen or k2 in seen:
            dropped += 1
            continue
        seen.add(k1)
        uniq.append(pts)
    counts["duplicates_dropped"] = dropped
    if dropped:
        notes.append(f"dropped {dropped} exact duplicate segment(s)")
    return uniq


def _open_chain_records(
    open_paths: list[np.ndarray], notes: list[str]
) -> list[dict]:
    """LOUD open-chain report: free endpoints, own gap, nearest other free end."""
    records: list[dict] = []
    free_ends = np.array(
        [pt for p in open_paths for pt in (p[0], p[-1])], dtype=float
    ).reshape(-1, 2)
    for pi, p in enumerate(open_paths):
        gap = float(np.linalg.norm(p[0] - p[-1]))
        nearest = None
        if len(open_paths) > 1:
            others = np.delete(free_ends, [2 * pi, 2 * pi + 1], axis=0)
            d0 = np.linalg.norm(others - p[0], axis=1).min()
            d1 = np.linalg.norm(others - p[-1], axis=1).min()
            nearest = float(min(d0, d1))
        records.append(
            {
                "n_points": int(len(p)),
                "start": [float(p[0][0]), float(p[0][1])],
                "end": [float(p[-1][0]), float(p[-1][1])],
                "gap": gap,
                "nearest_other_free_endpoint": nearest,
            }
        )
        if pi < _MAX_JUNCTION_NOTES:
            near_txt = f"{nearest:.6g}" if nearest is not None else "none"
            notes.append(
                f"UNCLOSED chain ({len(p)} pts): "
                f"start ({p[0][0]:.6g}, {p[0][1]:.6g}) "
                f"<-> end ({p[-1][0]:.6g}, {p[-1][1]:.6g}), gap {gap:.6g}, "
                f"nearest other free endpoint {near_txt}"
            )
    if len(open_paths) > _MAX_JUNCTION_NOTES:
        notes.append(
            f"... {len(open_paths) - _MAX_JUNCTION_NOTES} more unclosed "
            "chain(s) — full list in provenance['open_chains']"
        )
    return records


def _chain_segments(
    closed: list[np.ndarray],
    uniq: list[np.ndarray],
    chain_tol: float,
    diag: float,
    notes: list[str],
    counts: dict,
) -> tuple[list[np.ndarray], list[dict]]:
    """Chain open segments into closed loops; report unclosable chains loudly."""
    loops = list(closed)
    open_records: list[dict] = []
    if not uniq:
        return loops, open_records

    labels, centroids = _cluster_endpoints(uniq, chain_tol)
    chains: list[dict] = []
    degenerate_selfclosed = 0
    for idx, pts in enumerate(uniq):
        a, b = int(labels[2 * idx]), int(labels[2 * idx + 1])
        pts = pts.copy()
        pts[0], pts[-1] = centroids[a], centroids[b]  # snap: loops close exactly
        if a == b:
            if len(pts) >= 4:  # self-closed chain; first == last after snap
                loops.append(pts[:-1])
            else:
                degenerate_selfclosed += 1
            continue
        chains.append({"id": idx, "pts": pts, "a": a, "b": b})
    if degenerate_selfclosed:
        notes.append(
            f"dropped {degenerate_selfclosed} chain(s) that collapsed within "
            "chain_tol (both endpoints in one cluster, too short to be a loop)"
        )
        counts["degenerate_dropped"] = (
            counts.get("degenerate_dropped", 0) + degenerate_selfclosed
        )

    adj: dict[int, list[tuple[int, int]]] = {}
    for ci, c in enumerate(chains):
        adj.setdefault(c["a"], []).append((ci, 0))
        adj.setdefault(c["b"], []).append((ci, 1))

    junction_notes: list[str] = []
    used = [False] * len(chains)
    open_paths: list[np.ndarray] = []
    for start in range(len(chains)):
        if used[start]:
            continue
        used[start] = True
        c = chains[start]
        pts_list = [c["pts"]]
        end, is_closed = _extend(
            pts_list, c["b"], c["a"], adj, chains, used, junction_notes
        )
        if not is_closed:
            # Dead end forward: reverse and try to grow the other way too.
            whole = np.vstack(pts_list)[::-1]
            pts_list = [whole]
            end2, is_closed = _extend(
                pts_list, c["a"], end, adj, chains, used, junction_notes
            )
        loop = np.vstack(pts_list)
        if is_closed:
            loops.append(loop[:-1])  # snapped closure: last point == first
        else:
            open_paths.append(loop)

    for jn in junction_notes[:_MAX_JUNCTION_NOTES]:
        notes.append(jn)
    if len(junction_notes) > _MAX_JUNCTION_NOTES:
        notes.append(f"... {len(junction_notes) - _MAX_JUNCTION_NOTES} more junctions")

    open_records = _open_chain_records(open_paths, notes)
    counts["chains_unclosed"] = len(open_records)
    return loops, open_records


# ---------------------------------------------------------------------------
# planar-subdivision face extraction (junction- and T-junction-aware)


def _split_at_t_junctions(
    chains: list[np.ndarray],
    labels: np.ndarray,
    centroids: np.ndarray,
    tol: float,
) -> tuple[list[np.ndarray], int]:
    """Split chains where another endpoint cluster touches their interior."""
    out: list[np.ndarray] = []
    n_splits = 0
    for idx, pts in enumerate(chains):
        own = {int(labels[2 * idx]), int(labels[2 * idx + 1])}
        hits: list[tuple[int, float, int]] = []  # (sub-segment, t, cluster)
        for ci in range(len(centroids)):
            if ci in own:
                continue
            c = centroids[ci]
            for k in range(len(pts) - 1):
                a, b = pts[k], pts[k + 1]
                ab = b - a
                denom = float(ab @ ab)
                if denom == 0.0:
                    continue
                t = float(np.clip((c - a) @ ab / denom, 0.0, 1.0))
                proj = a + t * ab
                if np.linalg.norm(c - proj) >= tol:
                    continue
                # Interior only: near the chain's own ends it is a junction
                # the endpoint clustering already handles.
                if (
                    np.linalg.norm(proj - pts[0]) < tol
                    or np.linalg.norm(proj - pts[-1]) < tol
                ):
                    continue
                hits.append((k, t, ci))
                break
        if not hits:
            out.append(pts)
            continue
        hits.sort()
        n_splits += len(hits)
        cur: list[np.ndarray] = [pts[0]]
        hit_iter = iter(hits + [(len(pts) - 1, 2.0, -1)])  # sentinel past end
        k_next, t_next, ci_next = next(hit_iter)
        for k in range(len(pts) - 1):
            while k == k_next:
                cut = centroids[ci_next].copy()
                cur.append(cut)
                out.append(np.array(cur))
                cur = [cut]
                k_next, t_next, ci_next = next(hit_iter)
            cur.append(pts[k + 1])
        out.append(np.array(cur))
    return out, n_splits


def _extract_faces(
    closed: list[np.ndarray],
    open_chains: list[np.ndarray],
    chain_tol: float,
    diag: float,
    notes: list[str],
    counts: dict,
) -> tuple[list[np.ndarray], list[dict], list[dict]]:
    """Extract every enclosed region of the planar subdivision as a face.

    Segments are split at T-junctions, dangling chains (which can bound no
    face) are pruned and reported, then faces are traced by angular half-edge
    traversal: each chain bounds up to two regions (once per direction).
    Returns (loops, components, open_records); empty loops means no faces.
    """
    raw = [np.asarray(c, dtype=float) for c in open_chains]
    raw += [np.vstack([c, c[:1]]) for c in closed]  # closed paths = self-loops
    if not raw:
        return [], [], []

    labels, centroids = _cluster_endpoints(raw, chain_tol)
    raw, n_splits = _split_at_t_junctions(raw, labels, centroids, chain_tol)
    if n_splits:
        notes.append(f"split segments at {n_splits} T-junction point(s)")
        counts["t_splits"] = n_splits
        labels, centroids = _cluster_endpoints(raw, chain_tol)

    chains: list[dict] = []
    for idx, pts in enumerate(raw):
        a, b = int(labels[2 * idx]), int(labels[2 * idx + 1])
        pts = pts.copy()
        pts[0], pts[-1] = centroids[a], centroids[b]
        chains.append({"pts": pts, "a": a, "b": b})

    # Dangling chains (a degree-1 endpoint) can bound no face: prune + report.
    alive = set(range(len(chains)))
    while True:
        degree: dict[int, int] = {}
        for ci in alive:
            c = chains[ci]
            if c["a"] == c["b"]:
                degree[c["a"]] = degree.get(c["a"], 0) + 2
            else:
                for node in (c["a"], c["b"]):
                    degree[node] = degree.get(node, 0) + 1
        drop = {
            ci
            for ci in alive
            if degree[chains[ci]["a"]] < 2 or degree[chains[ci]["b"]] < 2
        }
        if not drop:
            break
        alive -= drop
    pruned = [chains[ci]["pts"] for ci in sorted(set(range(len(chains))) - alive)]
    open_records = _open_chain_records(pruned, notes) if pruned else []
    counts["chains_unclosed"] = len(open_records)
    if not alive:
        return [], [], open_records

    # Half-edges: (chain, dir); departures per node sorted by initial angle.
    # A chain contributes both directions or neither (the twin lookup below
    # must never miss).
    departures: dict[int, list[tuple[float, int, int]]] = {}
    order = sorted(alive)
    for ci in order:
        c = chains[ci]
        u0 = _first_direction(c["pts"])
        u1 = _first_direction(c["pts"][::-1])
        if u0 is None or u1 is None:
            continue
        for d, u in ((0, u0), (1, u1)):
            node = c["a"] if d == 0 else c["b"]
            departures.setdefault(node, []).append(
                (math.atan2(float(u[1]), float(u[0])), ci, d)
            )
    pos: dict[tuple[int, int], tuple[int, int]] = {}  # (chain, dir) -> (node, idx)
    for node, deps in departures.items():
        deps.sort()
        for i, (_ang, ci, d) in enumerate(deps):
            pos[(ci, d)] = (node, i)

    # Trace: next half-edge after arriving at v = the rotational predecessor
    # of the twin departure at v (keeps the enclosed face on the left; bounded
    # faces come out CCW).
    visited: set[tuple[int, int]] = set()
    cycles: list[list[tuple[int, int]]] = []
    for ci in order:
        for d in (0, 1):
            if (ci, d) in visited or (ci, d) not in pos:
                continue
            cycle: list[tuple[int, int]] = []
            cur = (ci, d)
            while cur not in visited:
                visited.add(cur)
                cycle.append(cur)
                cci, cd = cur
                twin = (cci, 1 - cd)
                node, i = pos[twin]
                deps = departures[node]
                _ang, nci, nd = deps[(i - 1) % len(deps)]
                cur = (nci, nd)
            cycles.append(cycle)

    outers: list[np.ndarray] = []
    inners: list[np.ndarray] = []
    n_degen = 0
    for cycle in cycles:
        parts = []
        for cci, cd in cycle:
            seg = chains[cci]["pts"] if cd == 0 else chains[cci]["pts"][::-1]
            parts.append(seg[:-1])
        loop = np.vstack(parts)
        area = _signed_area(loop)
        if abs(area) < _DEGEN_AREA_FACTOR * diag * diag:
            n_degen += 1
            continue
        (outers if area > 0 else inners).append(loop)
    if n_degen:
        notes.append(f"dropped {n_degen} degenerate face cycle(s) (~zero area)")

    # CW cycles: the boundary of an enclosing region's cavity. Assign each to
    # the smallest CCW face containing it; unassigned ones face the unbounded
    # plane and are discarded. Containment must be tested from a cycle vertex
    # OFF the candidate's boundary — a cycle and its own reversed twin share
    # every vertex, and PNPOLY on a boundary point is undefined.
    outer_areas = [_signed_area(o) for o in outers]
    holes_of: dict[int, list[np.ndarray]] = {}
    for cyc in inners:
        best, best_area = None, np.inf
        for oi, o in enumerate(outers):
            if outer_areas[oi] >= best_area:
                continue
            rep = None
            for v in cyc:
                if _boundary_distance(v, o) > chain_tol:
                    rep = v
                    break
            if rep is None:  # shares its whole boundary: the twin, not a host
                continue
            if _point_in_loop(rep, o):
                best, best_area = oi, outer_areas[oi]
        if best is not None:
            holes_of.setdefault(best, []).append(cyc)

    order_idx = sorted(range(len(outers)), key=lambda i: -outer_areas[i])
    loops: list[np.ndarray] = []
    components: list[dict] = []
    for oi in order_idx:
        comp_outer = len(loops)
        loops.append(outers[oi])
        hole_ids = []
        for h in sorted(holes_of.get(oi, []), key=lambda c: _signed_area(c)):
            hole_ids.append(len(loops))
            loops.append(h)  # already CW
        components.append({"outer": comp_outer, "holes": hole_ids})
    counts["faces"] = len(components)
    return loops, components, open_records


# ---------------------------------------------------------------------------
# loop classification


def _signed_area(loop: np.ndarray) -> float:
    x, y = loop[:, 0], loop[:, 1]
    return 0.5 * float(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1)))


def _boundary_distance(p: np.ndarray, loop: np.ndarray) -> float:
    """Distance from a point to the closed polyline boundary of ``loop``."""
    q = np.vstack([loop, loop[:1]])
    a, b = q[:-1], q[1:]
    ab = b - a
    denom = np.einsum("ij,ij->i", ab, ab)
    denom = np.where(denom == 0.0, 1.0, denom)
    t = np.clip(np.einsum("ij,ij->i", p - a, ab) / denom, 0.0, 1.0)
    proj = a + t[:, None] * ab
    return float(np.linalg.norm(p - proj, axis=1).min())


def _point_in_loop(pt: np.ndarray, loop: np.ndarray) -> bool:
    """Even-odd ray cast (PNPOLY); valid for points not on the boundary."""
    x, y = float(pt[0]), float(pt[1])
    x1, y1 = loop[:, 0], loop[:, 1]
    x2, y2 = np.roll(x1, -1), np.roll(y1, -1)
    cond = (y1 > y) != (y2 > y)
    with np.errstate(divide="ignore", invalid="ignore"):
        xint = x1 + (y - y1) * (x2 - x1) / (y2 - y1)
    hits = cond & (x < xint)
    return int(np.count_nonzero(hits)) % 2 == 1


def _classify_loops(
    raw_loops: list[np.ndarray], diag: float, notes: list[str], counts: dict
) -> tuple[list[np.ndarray], list[dict]]:
    """Drop degenerates, nest by even-odd depth, orient outers CCW / holes CW."""
    loops = []
    dropped = 0
    for lp in raw_loops:
        if len(lp) < 3 or abs(_signed_area(lp)) < _DEGEN_AREA_FACTOR * diag * diag:
            dropped += 1
            continue
        loops.append(np.asarray(lp, dtype=float))
    if dropped:
        notes.append(f"dropped {dropped} degenerate loop(s) (<3 pts or ~zero area)")
        counts["degenerate_dropped"] = counts.get("degenerate_dropped", 0) + dropped
    if not loops:
        return [], []

    areas = [abs(_signed_area(lp)) for lp in loops]
    containers: list[list[int]] = []
    for i, lp in enumerate(loops):
        rep = lp[0]
        containers.append(
            [j for j in range(len(loops)) if j != i and _point_in_loop(rep, loops[j])]
        )

    comp_map: dict[int, list[int]] = {}  # outer index -> hole indices
    for i in range(len(loops)):
        depth = len(containers[i])
        if depth % 2 == 0:
            comp_map.setdefault(i, [])
        else:
            parent = min(containers[i], key=lambda j: areas[j])
            if len(containers[parent]) % 2 != 0:
                notes.append(
                    f"loop {i}: smallest container is itself a hole "
                    "(unusual nesting) — attached anyway"
                )
            comp_map.setdefault(parent, []).append(i)

    ordered_outers = sorted(comp_map, key=lambda i: -areas[i])
    new_loops: list[np.ndarray] = []
    components: list[dict] = []
    for outer in ordered_outers:
        o = loops[outer]
        if _signed_area(o) < 0:
            o = o[::-1].copy()  # outers CCW
        oi = len(new_loops)
        new_loops.append(o)
        hole_ids = []
        for h in sorted(comp_map[outer], key=lambda i: -areas[i]):
            hl = loops[h]
            if _signed_area(hl) > 0:
                hl = hl[::-1].copy()  # holes CW
            hole_ids.append(len(new_loops))
            new_loops.append(hl)
        components.append({"outer": oi, "holes": hole_ids})
    return new_loops, components

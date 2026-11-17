"""MYSTRAN runner + F06 read-back (V25): solve-level NASTRAN validation.

MYSTRAN (a free NASTRAN-compatible solver) runs the V15 .bdf decks.
Probe-verified traps this module handles:

- MYSTRAN exits 0 even when the run died with FATAL messages — success
  is judged by scanning stdout AND the F06, never the exit code alone.
- It scatters scratch files (.L1*, fort.14, .ANS, .ERR) next to the
  deck, so the deck is staged into a temp dir (ccx-runner discipline)
  and only the F06 is persisted.
- Displacements appear in the F06 only when the case control says
  ``DISPLACEMENT(PRINT)`` (the V15 writer emits that since V25).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from digitwin.textguard import scrub

_MYSTRAN_FALLBACK = Path.home() / "miniconda3/envs/mystran/bin/mystran"


def find_mystran() -> str | None:
    """MESHFORGE_MYSTRAN env var, then PATH, then the known conda env."""
    for candidate in (
        os.environ.get("MESHFORGE_MYSTRAN"),
        shutil.which("mystran"),
        str(_MYSTRAN_FALLBACK),
    ):
        if candidate and Path(candidate).is_file():
            return candidate
    return None


def parse_f06_displacements(text: str) -> dict[int, np.ndarray]:
    """``D I S P L A C E M E N T S`` blocks -> {1-based grid: (3,) T1..T3}.

    Rows are ``GRID SYS T1 T2 T3 R1 R2 R3`` (8 columns); rotations are
    ignored (solids). Later blocks (subcases) overwrite earlier ids.
    """
    u: dict[int, np.ndarray] = {}
    in_block = False
    for line in text.splitlines():
        if "D I S P L A C E M E N T S" in line:
            in_block = True
            continue
        if not in_block:
            continue
        parts = line.split()
        if len(parts) == 8 and parts[0].isdigit():
            try:
                u[int(parts[0])] = np.array([float(v) for v in parts[2:5]])
            except ValueError:
                in_block = False
        elif parts and not (
            "GRID" in line or "SYS" in line or "coord" in line.lower()
        ):
            in_block = False  # left the data table (next header/section)
    return u


@dataclass
class MystranResult:
    bdf: Path
    mystran: str
    elapsed: float
    f06_path: Path
    grid_ids: np.ndarray  # sorted, 1-based
    displacements: np.ndarray  # aligned with grid_ids, (n, 3)
    u_max: float
    notes: list[str] = field(default_factory=list)

    def summary(self) -> dict:
        return {
            "bdf": str(self.bdf),
            "mystran": self.mystran,
            "elapsed_s": round(self.elapsed, 3),
            "n_grids": int(len(self.grid_ids)),
            "u_max": self.u_max,
            "f06": str(self.f06_path),
            "notes": self.notes,
        }

    def to_text(self) -> str:
        text = (
            f"solve[mystran]: {len(self.grid_ids)} grids, "
            f"{self.elapsed:.1f}s\n"
            f"  |U| max     : {self.u_max:.6g}\n"
        )
        for note in self.notes:
            text += f"  note: {note}\n"
        return text


def solve_bdf(
    bdf: str | Path,
    out_dir: str | Path,
    *,
    mystran: str | None = None,
    timeout: float = 600.0,
) -> MystranResult:
    """Run MYSTRAN on ``bdf`` and read the F06 displacements back."""
    from digitwin.export.neutral import guard_output_path

    bdf = Path(bdf)
    if not bdf.is_file():
        raise FileNotFoundError(f"bdf deck not found: {bdf}")
    exe = mystran or find_mystran()
    if exe is None:
        raise RuntimeError(
            "MYSTRAN not found: set MESHFORGE_MYSTRAN, add mystran to PATH, "
            "or create the conda env "
            "(conda create -n mystran -c conda-forge mystran)"
        )
    if not Path(exe).is_file():
        raise RuntimeError(f"MYSTRAN executable not found: {exe}")

    out_dir = Path(out_dir)
    guard_output_path(out_dir / f"{bdf.stem}.F06", input_paths=[bdf])

    t0 = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="meshforge_mystran_") as tmp:
        work = Path(tmp)
        staged = work / bdf.name
        shutil.copyfile(bdf, staged)
        try:
            proc = subprocess.run(
                [exe, bdf.name],
                cwd=work,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                f"mystran exceeded the {timeout:.0f}s timeout on {bdf.name}"
            ) from exc
        elapsed = time.perf_counter() - t0
        output = (proc.stdout or "") + (proc.stderr or "")
        f06_src = work / f"{bdf.stem}.F06"
        f06_text = f06_src.read_text() if f06_src.is_file() else ""
        # MYSTRAN exits 0 on fatal errors — judge by content, loudly.
        if proc.returncode != 0 or "FATAL" in output or "FATAL" in f06_text:
            raise RuntimeError(
                f"mystran failed on {bdf.name} (exit {proc.returncode}; "
                f"MYSTRAN reports fatals in the F06, not the exit code):\n"
                f"{scrub((f06_text or output)[-2000:])}"
            )
        if not f06_src.is_file():
            raise RuntimeError(
                f"mystran produced no F06 for {bdf.name}:\n{scrub(output[-2000:])}"
            )
        out_dir.mkdir(parents=True, exist_ok=True)
        f06_path = out_dir / f06_src.name
        shutil.copyfile(f06_src, f06_path)

    u = parse_f06_displacements(f06_text)
    if not u:
        raise RuntimeError(
            f"no displacement block in {f06_path} — the case control needs "
            f"DISPLACEMENT(PRINT) = ALL (V15 decks written before V25 used "
            f"PLOT; re-export the deck)"
        )
    grid_ids = np.array(sorted(u), dtype=np.int64)
    disp = np.array([u[int(i)] for i in grid_ids])
    return MystranResult(
        bdf=bdf,
        mystran=exe,
        elapsed=elapsed,
        f06_path=f06_path,
        grid_ids=grid_ids,
        displacements=disp,
        u_max=float(np.linalg.norm(disp, axis=1).max()),
    )

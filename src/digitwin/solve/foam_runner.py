"""OpenFOAM runner + results read-back (V23), ccx-runner discipline.

OpenFOAM on this platform ships as an app with a launcher
(``openfoam2506``); solver binaries are NOT on PATH. The verified
non-interactive form is ``openfoam2506 -- <application> <args...>`` —
plain list argv, never a shell string. ``find_foam`` returns the argv
PREFIX for whichever install is present (sourced env → no prefix).

OpenFOAM refuses to run when ANY path component contains a space
(``fileName::stripInvalid`` aborts on the working directory itself) and
this repo lives under ".../Real Real Grid" — so every app call stages a
COPY of the case into a temp directory, runs there, and syncs the
results (time dirs, logs) back into the real case dir, exactly the ccx
runner's discipline. Every failure mode is loud: no OpenFOAM, timeout,
nonzero exit, ``FOAM FATAL`` in the output, no time directory written,
missing/failed checkMesh.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from digitwin.envvars import env_lookup
from digitwin.textguard import scrub

_LAUNCHERS = ("openfoam", "openfoam2506", "openfoam2512", "openfoam2606")

# The launcher will happily exec ANY command on PATH; an external case's
# controlDict chooses the application, so only known OpenFOAM apps are
# ever run (extend deliberately per milestone, never fail-open).
_ALLOWED_APPS = ("checkMesh", "simpleFoam", "icoFoam")


def find_foam() -> list[str] | None:
    """Argv prefix that runs an OpenFOAM application, or None.

    Order: DIGITWIN_OPENFOAM (launcher path) → a sourced environment
    (solvers directly on PATH → empty prefix) → known brew launchers.
    """
    name, env = env_lookup("OPENFOAM")
    if env:
        if not Path(env).is_file():
            raise RuntimeError(
                f"{name} points to a missing file: {env}"
            )
        return [env, "--"]
    if shutil.which("simpleFoam"):
        return []
    for launcher in _LAUNCHERS:
        exe = shutil.which(launcher)
        if exe:
            return [exe, "--"]
    return None


def run_foam_app(
    app: str,
    case_dir: str | Path,
    *args: str,
    timeout: float = 600.0,
    foam: list[str] | None = None,
) -> str:
    """Run one OpenFOAM application on a staged copy of the case, sync
    the results back, and return the output (also persisted as
    ``log.<app>`` in the case dir)."""
    if app not in _ALLOWED_APPS:
        raise ValueError(
            f"application {app!r} is not on the allowed list "
            f"{_ALLOWED_APPS} — the case's controlDict chooses what runs, "
            f"so unknown applications are refused"
        )
    case = Path(case_dir)
    if not case.is_dir():
        raise FileNotFoundError(f"case directory not found: {case}")
    prefix = foam if foam is not None else find_foam()
    if prefix is None:
        raise RuntimeError(
            "OpenFOAM not found: set DIGITWIN_OPENFOAM to the launcher, "
            "put the solvers on PATH, or install it "
            "(brew install gerlero/openfoam/openfoam@2506)"
        )
    with tempfile.TemporaryDirectory(prefix="digitwin_foam_") as tmp:
        if " " in tmp:
            raise RuntimeError(
                f"temp dir {tmp!r} contains a space — OpenFOAM cannot run "
                f"there; point TMPDIR at a space-free path"
            )
        work = Path(tmp) / "case"
        shutil.copytree(case, work)
        argv = [*prefix, app, "-case", str(work), *args]
        try:
            proc = subprocess.run(
                argv, cwd=tmp, capture_output=True, text=True, timeout=timeout
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                f"{app} exceeded the {timeout:.0f}s timeout on {case}"
            ) from exc
        output = (proc.stdout or "") + (proc.stderr or "")
        (work / f"log.{app}").write_text(output)
        # Sync everything the app produced (time dirs, logs, processor
        # dirs) back into the real case, then judge success.
        shutil.copytree(work, case, dirs_exist_ok=True)
    if proc.returncode != 0 or "FOAM FATAL" in output:
        raise RuntimeError(
            f"{app} failed (exit {proc.returncode}) on {case}:\n{scrub(output[-2000:])}"
        )
    return output


_MINIMAL_CONTROL = """FoamFile
{
    version     2.0;
    format      ascii;
    class       dictionary;
    object      %s;
}
"""


def check_mesh(
    case_dir: str | Path,
    *,
    timeout: float = 300.0,
    foam: list[str] | None = None,
) -> dict:
    """Run checkMesh; loud unless it reports "Mesh OK".

    checkMesh refuses to start without system/ dictionaries (and wants
    writeInterval + the scheme sub-dicts present), so minimal stubs are
    written when absent (polyMesh-only case dirs from V14).
    """
    case = Path(case_dir)
    system = case / "system"
    if not (system / "controlDict").is_file():
        system.mkdir(parents=True, exist_ok=True)
        (system / "controlDict").write_text(
            _MINIMAL_CONTROL % "controlDict"
            + "application checkMesh;\nstartFrom startTime;\nstartTime 0;\n"
              "stopAt endTime;\nendTime 1;\ndeltaT 1;\n"
              "writeControl timeStep;\nwriteInterval 1;\n"
        )
        if not (system / "fvSchemes").is_file():
            (system / "fvSchemes").write_text(
                _MINIMAL_CONTROL % "fvSchemes"
                + "ddtSchemes { default steadyState; }\n"
                  "gradSchemes { default Gauss linear; }\n"
                  "divSchemes { default none; }\n"
                  "laplacianSchemes { default Gauss linear corrected; }\n"
                  "interpolationSchemes { default linear; }\n"
                  "snGradSchemes { default corrected; }\n"
            )
        if not (system / "fvSolution").is_file():
            (system / "fvSolution").write_text(_MINIMAL_CONTROL % "fvSolution")
    output = run_foam_app("checkMesh", case, timeout=timeout, foam=foam)
    if "Mesh OK" not in output:
        raise RuntimeError(
            f"checkMesh did not report 'Mesh OK' on {case}:\n{scrub(output[-2000:])}"
        )
    report = {"ok": True}
    m = re.search(r"non-orthogonality Max:\s*([\d.eE+-]+)", output)
    if m:
        report["max_non_orthogonality"] = float(m.group(1))
    m = re.search(r"Max skewness =\s*([\d.eE+-]+)", output)
    if m:
        report["max_skewness"] = float(m.group(1))
    return report


def _attach_u_mag(grid, case: Path, time: float):
    import numpy as np

    missing = [f for f in ("U", "p") if f not in grid.point_data]
    if missing:
        raise RuntimeError(
            f"case {case} at t={time:g} has no {'/'.join(missing)} point "
            f"data — did the solver write this time?"
        )
    grid.point_data["U_mag"] = np.linalg.norm(grid.point_data["U"], axis=1)
    grid.point_data.active_scalars_name = "U_mag"
    return grid


def read_case(case_dir: str | Path):
    """Latest-time internalMesh with U/p (+ derived U_mag) as a pyvista
    grid; returns (grid, latest_time)."""
    import pyvista as pv

    case = Path(case_dir)
    stub = case / "case.foam"
    if not stub.exists():
        stub.touch()
    reader = pv.OpenFOAMReader(str(stub))
    times = [float(t) for t in reader.time_values]
    if not times:
        raise RuntimeError(f"no time steps readable in {case}")
    latest = max(times)
    reader.set_active_time_value(latest)
    grid = reader.read()["internalMesh"]
    return _attach_u_mag(grid, case, latest), latest


def read_case_series(case_dir: str | Path):
    """ALL time steps of a solved case as (times, grids), oldest first.

    Every check is loud: a time series with a silently missing frame
    (reader skipping an unreadable dir), a frame without U/p, or a
    changing point count would poison playback and any frame-wise
    post-processing downstream.
    """
    import pyvista as pv

    case = Path(case_dir)
    stub = case / "case.foam"
    if not stub.exists():
        stub.touch()
    reader = pv.OpenFOAMReader(str(stub))
    times = [float(t) for t in reader.time_values]
    if len(times) < 2:
        raise RuntimeError(
            f"case {case} has {len(times)} readable time step(s) — a time "
            f"series needs at least 2 (set write_interval below end_time)"
        )
    if any(b <= a for a, b in zip(times, times[1:])):
        raise RuntimeError(f"reader times not strictly increasing: {times}")
    on_disk = []
    for entry in case.iterdir():
        if entry.is_dir():
            try:
                on_disk.append(float(entry.name))
            except ValueError:
                continue
    if sorted(on_disk) != times:
        raise RuntimeError(
            f"reader times {times} do not match the time directories on "
            f"disk {sorted(on_disk)} in {case} — a frame was skipped or "
            f"left unreadable"
        )
    grids = []
    n_points = None
    for t in times:
        reader.set_active_time_value(t)
        grid = _attach_u_mag(reader.read()["internalMesh"], case, t)
        if n_points is None:
            n_points = grid.n_points
        elif grid.n_points != n_points:
            raise RuntimeError(
                f"case {case}: point count changed at t={t:g} "
                f"({grid.n_points} vs {n_points}) — frames are not on one mesh"
            )
        grids.append(grid)
    return times, grids


@dataclass
class CfdResult:
    case: Path
    solver: str
    elapsed: float
    latest_time: float
    grid: object  # pv.UnstructuredGrid
    n_cells: int
    u_max: float
    p_min: float
    p_max: float
    notes: list[str] = field(default_factory=list)
    times: list[float] | None = None  # V29: full series, oldest first
    frames: list | None = None  # V29: one grid per time, same order

    def summary(self) -> dict:
        out = {
            "case": str(self.case),
            "solver": self.solver,
            "elapsed_s": round(self.elapsed, 3),
            "latest_time": self.latest_time,
            "n_cells": self.n_cells,
            "u_max": self.u_max,
            "p_min": self.p_min,
            "p_max": self.p_max,
            "notes": self.notes,
        }
        if self.times is not None:
            out["n_frames"] = len(self.times)
            out["times"] = list(self.times)
        return out

    def to_text(self) -> str:
        text = (
            f"cfd[{self.solver}]: {self.n_cells} cells, t={self.latest_time:g}, "
            f"{self.elapsed:.1f}s\n"
            f"  |U| max     : {self.u_max:.6g}\n"
            f"  p range     : {self.p_min:.6g} .. {self.p_max:.6g}\n"
        )
        if self.times is not None:
            text += (
                f"  frames      : {len(self.times)} "
                f"(t={self.times[0]:g} .. {self.times[-1]:g})\n"
            )
        for note in self.notes:
            text += f"  note: {note}\n"
        return text


def _controldict_application(case: Path) -> str:
    control = case / "system" / "controlDict"
    if not control.is_file():
        raise FileNotFoundError(
            f"no system/controlDict in {case} — build the case first "
            f"(write_case_files / the cfd command's --inlet/--outlet form)"
        )
    m = re.search(r"^\s*application\s+(\w+)\s*;", control.read_text(), re.M)
    if not m:
        raise ValueError(f"no 'application' entry in {control}")
    return m.group(1)


def solve_case(
    case_dir: str | Path,
    *,
    solver: str | None = None,
    timeout: float = 600.0,
    foam: list[str] | None = None,
    series: bool = False,
) -> CfdResult:
    """Run the case's solver and read the results back; with ``series``
    every written time step comes back as a frame (V29)."""
    import numpy as np

    case = Path(case_dir)
    app = solver or _controldict_application(case)
    t0 = time.perf_counter()
    output = run_foam_app(app, case, timeout=timeout, foam=foam)
    elapsed = time.perf_counter() - t0

    def _is_time_dir(name: str) -> bool:
        try:
            return float(name) > 0.0
        except ValueError:
            return False

    # Only dirs beyond the start time count — 0/ existing proves nothing.
    if not any(
        d.is_dir() and _is_time_dir(d.name) for d in case.iterdir()
    ):
        raise RuntimeError(
            f"{app} exited cleanly but wrote no time directory in {case}:\n"
            f"{output[-2000:]}"
        )

    notes = []
    m = re.search(r"SIMPLE solution converged in (\d+) iterations", output)
    if m:
        notes.append(f"SIMPLE converged in {m.group(1)} iterations")
    elif app == "simpleFoam":
        notes.append(
            "simpleFoam hit endTime without meeting residualControl — "
            "treat the fields as unconverged"
        )

    times = frames = None
    if series:
        times, frames = read_case_series(case)
        grid, latest = frames[-1], times[-1]
    else:
        grid, latest = read_case(case)
    p = np.asarray(grid.point_data["p"], dtype=float)
    return CfdResult(
        case=case,
        solver=app,
        elapsed=elapsed,
        latest_time=latest,
        grid=grid,
        n_cells=grid.n_cells,
        u_max=float(grid.point_data["U_mag"].max()),
        p_min=float(p.min()),
        p_max=float(p.max()),
        notes=notes,
        times=times,
        frames=frames,
    )

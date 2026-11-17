from __future__ import annotations

from pathlib import Path

import pyvista as pv

# Neutral formats only; solver decks (Abaqus) are a separate exporter.
SUPPORTED_FORMATS: dict[str, str] = {
    "vtu": ".vtu",
    "vtk": ".vtk",
    "ply": ".ply",
    "obj": ".obj",
}
DEFAULT_FORMAT = "vtu"


def guard_output_path(out_path: str | Path, *, input_paths: list[str | Path | None]) -> None:
    """Refuse to write over any input file (hard rule: inputs are read-only)."""
    out = Path(out_path).resolve()
    for p in input_paths:
        if p is not None and out == Path(p).resolve():
            raise ValueError(f"refusing to overwrite input file: {p}")


def export_surface(
    mesh: pv.PolyData,
    out_path: str | Path,
    *,
    fmt: str | None = None,
    input_path: str | Path | None = None,
) -> Path:
    """Write ``mesh`` to a neutral format via meshio.

    ``fmt`` defaults to the output suffix, falling back to VTU; an explicit
    ``fmt`` wins and the suffix is corrected to match. Unknown formats are
    rejected, never guessed.
    """
    out = Path(out_path)
    if fmt is None:
        fmt = out.suffix.lower().lstrip(".") or DEFAULT_FORMAT
    fmt = fmt.lower()
    if fmt not in SUPPORTED_FORMATS:
        raise ValueError(
            f"unknown export format {fmt!r}; supported: {', '.join(sorted(SUPPORTED_FORMATS))}"
        )
    out = out.with_suffix(SUPPORTED_FORMATS[fmt])
    guard_output_path(out, input_paths=[input_path])
    out.parent.mkdir(parents=True, exist_ok=True)
    pv.save_meshio(out, mesh)
    return out

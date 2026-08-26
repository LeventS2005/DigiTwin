from __future__ import annotations

import hashlib
from pathlib import Path

import pyvista as pv

from meshforge.io.paths import normalize_path

# Suffixes meshforge DOES handle, but through a dedicated importer rather than
# pyvista's reader. "Load surface" is the wrong front door for all of them, so
# name the right one instead of letting the library's own error through
# ("This file was not able to be automatically read by pyvista." tells a user
# nothing about the import_cso panel that would have worked).
#
# One table: {suffix: (what it is, Workbench panel, CLI subcommand)}.
_DEDICATED_IMPORTERS: dict[str, tuple[str, str, str]] = {
    ".cso": ("a MeVisLab contour file", "import_cso", "import-cso"),
    ".dxf": ("a 2-D CAD drawing", "import_cad", "import-cad"),
    ".dwg": ("a 2-D CAD drawing", "import_cad", "import-cad"),
    ".step": ("a 3-D CAD solid", "import_cad3d", "import-cad3d"),
    ".stp": ("a 3-D CAD solid", "import_cad3d", "import-cad3d"),
    ".iges": ("a 3-D CAD solid", "import_cad3d", "import-cad3d"),
    ".igs": ("a 3-D CAD solid", "import_cad3d", "import-cad3d"),
    ".brep": ("a 3-D CAD solid", "import_cad3d", "import-cad3d"),
    ".npy": ("a voxel volume", "import_image", "import-image"),
    ".nii": ("a NIfTI volume", "import_image", "import-image"),
    ".nii.gz": ("a NIfTI volume", "import_image", "import-image"),
    ".dcm": ("a DICOM slice", "import_image", "import-image"),
}


def file_sha256(path: str | Path) -> str:
    """SHA-256 of the file bytes; used to prove inputs stay byte-for-byte unchanged.

    The existence guard is here rather than in each caller because every
    rule-1 bracket (``checksum -> work -> checksum``) hashes FIRST: without it
    a mistyped path surfaces as a bare ``[Errno 2] No such file or directory``
    from ``open()``, and the caller's own friendly message never runs. A file
    that does not exist has nothing to protect, so checking before the first
    hash cannot weaken the read-only proof.
    """
    path = normalize_path(path)
    if not path.is_file():
        raise FileNotFoundError(f"file not found: {path}")
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def dedicated_importer(path: str | Path) -> tuple[str, str, str, str] | None:
    """``(suffix, description, panel, subcommand)`` if this suffix has its own
    importer, else ``None``.

    Longest suffix wins so ``.nii.gz`` never resolves as a bare ``.nii``.
    """
    name = Path(path).name.lower()
    for suffix in sorted(_DEDICATED_IMPORTERS, key=len, reverse=True):
        if name.endswith(suffix):
            return (suffix, *_DEDICATED_IMPORTERS[suffix])
    return None


def load_surface(path: str | Path) -> pv.PolyData:
    """Load a triangulated surface (.stl/.ply/.vtp/.obj/...) read-only.

    Rejects non-surface data (volume grids, point clouds) rather than
    guessing a conversion — the caller must know what they loaded. A format
    with its own importer (.cso, CAD, volumes) is refused here by SUFFIX,
    before the file is opened, and told which panel/subcommand to use.
    """
    path = normalize_path(path)
    match = dedicated_importer(path)
    if match is not None:
        suffix, what, panel, subcommand = match
        raise ValueError(
            f"{suffix} is {what} — load it with the {panel} panel "
            f"(CLI: meshforge {subcommand} <file>), not 'Load surface': "
            f"{path.name}"
        )
    if not path.is_file():
        raise FileNotFoundError(f"surface file not found: {path}")
    mesh = pv.read(path)
    if not isinstance(mesh, pv.PolyData):
        raise TypeError(
            f"expected a surface (PolyData), got {type(mesh).__name__} from {path.name}; "
            "meshforge inspects surfaces, not volume grids"
        )
    if mesh.n_points == 0 or mesh.n_cells == 0:
        raise ValueError(f"surface is empty (0 points or 0 cells): {path.name}")
    return mesh

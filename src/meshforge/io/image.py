"""Biomedical image ingest (V18): 3-D volumes -> iso-surfaces.

The numpy core (``volume_to_surface``) needs no new dependency; the
medical loaders are lazy — nibabel for NIfTI, pydicom for DICOM series —
and their absence raises with an install hint. ``level`` is always
required: a segmentation threshold is a modelling decision, never a
default. Orientation safety over convenience: NIfTI affines with rotation
terms are refused loudly rather than silently mis-orienting anatomy.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import numpy as np

_NIFTI_SUFFIXES = (".nii", ".nii.gz")


def volume_to_surface(
    array: np.ndarray,
    level: float,
    *,
    spacing: tuple[float, float, float] = (1.0, 1.0, 1.0),
    origin: tuple[float, float, float] = (0.0, 0.0, 0.0),
):
    """Marching-cubes iso-surface of a 3-D scalar volume.

    Index (i, j, k) maps to physical ``origin + index * spacing``.
    Returns (all-triangle outward-oriented pv.PolyData, provenance dict);
    a leaky (non-watertight) surface is a provenance note, not an error —
    downstream ``check`` gates meshing.
    """
    import pyvista as pv

    array = np.asarray(array)
    if array.ndim != 3 or min(array.shape) < 2:
        raise ValueError(
            f"volume must be 3-D with every axis >= 2, got shape {array.shape}"
        )
    # F4: bound the voxel count exactly as the geometry paths do
    # (centerline.py/sdf.py) — the V18 image ingest otherwise flows an
    # oversized/crafted volume straight into pv.ImageData + marching cubes
    # with no ceiling, a memory-exhaustion DoS. Refuse before scanning it.
    from meshforge.geometry.centerline import MAX_VOXELS

    if int(np.prod(array.shape)) > MAX_VOXELS:
        raise ValueError(
            f"volume {tuple(array.shape)} exceeds {MAX_VOXELS} voxels; "
            f"downsample before ingest"
        )
    if not np.isfinite(array).all():
        raise ValueError("volume contains non-finite values — clean it first")
    if not all(s > 0 for s in spacing):
        raise ValueError(f"spacing must be positive, got {spacing}")
    lo, hi = float(array.min()), float(array.max())
    if not lo < level < hi:
        raise ValueError(
            f"iso level {level} is outside the data range [{lo:.6g}, {hi:.6g}]"
        )

    grid = pv.ImageData(
        dimensions=array.shape, spacing=tuple(spacing), origin=tuple(origin)
    )
    grid.point_data["v"] = array.ravel(order="F")
    surface = grid.contour([float(level)], scalars="v").triangulate().clean()
    if surface.n_cells == 0:
        raise ValueError(
            f"contour at level {level} produced no triangles — the iso-surface "
            f"may clip the volume bounds"
        )

    # Enforce OUTWARD normals (contour can emit inward — the V11 lesson):
    # the signed volume of a closed outward surface is positive.
    p = np.asarray(surface.points)
    tri = surface.faces.reshape(-1, 4)[:, 1:]
    signed = float(
        np.einsum("ij,ij->i", p[tri[:, 0]], np.cross(p[tri[:, 1]], p[tri[:, 2]])).sum()
        / 6.0
    )
    if signed < 0:
        if hasattr(surface, "flip_faces"):
            surface = surface.flip_faces()
        else:  # older pyvista: in-place
            surface.flip_normals()
        signed = -signed

    notes = []
    watertight = int(surface.n_open_edges) == 0
    if not watertight:
        notes.append(
            f"iso-surface is not watertight ({surface.n_open_edges} open "
            f"edge(s)) — it likely clips the volume bounds; pad the volume "
            f"or repair before meshing"
        )
    provenance = {
        "shape": [int(s) for s in array.shape],
        "spacing": [float(s) for s in spacing],
        "origin": [float(v) for v in origin],
        "level": float(level),
        "data_range": [lo, hi],
        "n_points": int(surface.n_points),
        "n_triangles": int(surface.n_cells),
        "enclosed_volume": abs(signed) if watertight else None,
        "watertight": watertight,
        "notes": notes,
    }
    return surface, provenance


def load_volume(path: str | Path):
    """Load a volume by kind: .npy array, .nii/.nii.gz (nibabel), a DICOM series
    directory, or a single multi-frame .dcm file (both pydicom).  Returns
    (array, spacing, origin) with index (i, j, k) at ``origin + index*spacing``."""
    path = Path(path)
    if path.is_dir():
        return _load_dicom_series(path)
    if not path.is_file():
        raise FileNotFoundError(f"volume not found: {path}")
    name = path.name.lower()
    if name.endswith(".npy"):
        array = np.load(path, allow_pickle=False)
        return array, (1.0, 1.0, 1.0), (0.0, 0.0, 0.0)
    if name.endswith(_NIFTI_SUFFIXES):
        return _load_nifti(path)
    if name.endswith(".dcm"):
        array, spacing, origin, _ = _read_dicom_multiframe(path, pixels=True)
        return array, spacing, origin
    raise ValueError(
        f"unsupported volume input {path.name!r}; supported: .npy, "
        f".nii/.nii.gz, a single multi-frame .dcm, or a DICOM series directory"
    )


# Axial patient orientation (row = +x, col = +y, slice normal = +z).  These GE
# cardiac CT reconstructions ship as ONE multi-frame file with axis-aligned axial
# frames; an oblique/rotated orientation is refused rather than silently
# mis-oriented (the same posture as the NIfTI affine check).
_AXIAL_IOP = np.array([1.0, 0.0, 0.0, 0.0, 1.0, 0.0])


def dicom_grid(path: str | Path):
    """Geometry-only ``(dims, spacing, origin)`` of a multi-frame .dcm, WITHOUT
    reading the pixel data — for voxelising a segmentation onto the image grid."""
    _, spacing, origin, dims = _read_dicom_multiframe(Path(path), pixels=False)
    return dims, spacing, origin


def dicom_grid_checked(path: str | Path):
    """Read the .dcm ONCE and derive BOTH the rule-1 checksum and the
    geometry-only grid from the same buffer, so there is no window for the
    file to change between the checksum read and the parse read (the
    F-INFO-8 posture, applied to the ``--dicom`` anchor — closes the D62
    BACKLOG two-read). Returns ``(sha256_hexdigest, (dims, spacing, origin))``;
    the digest equals ``sha256(file bytes)`` (the same value
    ``volume_input_sha256`` computes)."""
    import hashlib

    p = Path(path)
    buf = p.read_bytes()
    sha = hashlib.sha256(buf).hexdigest()
    _, spacing, origin, dims = _read_dicom_multiframe(p, pixels=False, buf=buf)
    return sha, (dims, spacing, origin)


def _read_dicom_multiframe(path: Path, *, pixels: bool, buf: bytes | None = None):
    """Read a single multi-frame DICOM (classic SOP class + NumberOfFrames, no
    functional groups — the GE cardiac CT layout).  Frames march +z from
    ImagePositionPatient; the inter-frame step is SpacingBetweenSlices when
    present (the reconstruction interval — SliceThickness is the slab width and
    can differ), else SliceThickness.  Returns (array|None, spacing, origin,
    dims); ``array`` is (x, y, z) HU when ``pixels`` else None."""
    try:
        import pydicom
    except ImportError as exc:
        raise RuntimeError(
            "DICOM input needs pydicom — install with: pip install pydicom"
        ) from exc

    # ``buf`` lets a caller (dicom_grid_checked) hand in the already-read
    # bytes so the file is read exactly once — checksum + parse from the
    # same buffer. ``path`` is still used for error messages.
    if buf is None:
        ds = pydicom.dcmread(str(path), stop_before_pixels=not pixels)
    else:
        import io as _io

        ds = pydicom.dcmread(_io.BytesIO(buf), stop_before_pixels=not pixels)
    nf = int(getattr(ds, "NumberOfFrames", 1) or 1)
    if nf < 2:
        raise ValueError(
            f"{path.name} has {nf} frame(s); a single-frame .dcm is not a "
            f"volume — point at the DICOM series directory instead"
        )
    if "PerFrameFunctionalGroupsSequence" in ds:
        raise ValueError(
            f"{path.name} is an enhanced multi-frame DICOM (per-frame functional "
            f"groups) — this loader handles the classic axial layout only"
        )
    iop = np.asarray([float(v) for v in getattr(ds, "ImageOrientationPatient",
                                                _AXIAL_IOP)], dtype=float)
    if iop.shape != (6,) or float(np.abs(iop - _AXIAL_IOP).max()) > 1e-4:
        raise ValueError(
            f"{path.name} has a non-axial ImageOrientationPatient {list(iop)} — "
            f"re-orient to axial (row=+x, col=+y) first; refusing to silently "
            f"mis-orient anatomy"
        )
    rows, cols = int(ds.Rows), int(ds.Columns)
    px_row, px_col = (float(v) for v in ds.PixelSpacing)  # (row=y, col=x)
    z_sp = float(getattr(ds, "SpacingBetweenSlices", 0.0) or 0.0)
    if z_sp <= 0:
        z_sp = float(getattr(ds, "SliceThickness", 0.0) or 0.0)
    if z_sp <= 0:
        raise ValueError(
            f"{path.name} lacks a usable slice spacing (SpacingBetweenSlices / "
            f"SliceThickness) — cannot place frames in space"
        )
    origin = tuple(float(v) for v in ds.ImagePositionPatient)
    spacing = (px_col, px_row, z_sp)  # (x, y, z)
    # PixelSpacing/origin are attacker-controlled header fields: a non-finite or
    # non-positive pixel pitch survives the `spacing <= 0` check on the anchoring
    # path (NaN is False) and voxelises a segmentation onto a degenerate lattice.
    # Validate at the source so dicom_grid itself fails loud (only z was guarded).
    if not all(np.isfinite(s) and s > 0 for s in spacing):
        raise ValueError(
            f"{path.name} has a non-finite or non-positive pixel spacing "
            f"{spacing} — refusing to place voxels on a degenerate grid"
        )
    if not all(np.isfinite(v) for v in origin):
        raise ValueError(
            f"{path.name} has a non-finite ImagePositionPatient {origin}"
        )
    dims = (cols, rows, nf)
    if not pixels:
        # geometry only (for grid-anchoring a segmentation) — nothing allocated,
        # so no voxel ceiling; the ROI crop bounds the actual mask.
        return None, spacing, origin, dims
    from meshforge.geometry.centerline import MAX_VOXELS

    if cols * rows * nf > MAX_VOXELS:
        raise ValueError(
            f"volume {dims} exceeds {MAX_VOXELS} voxels; downsample before ingest"
        )
    slope = float(getattr(ds, "RescaleSlope", 1.0))
    intercept = float(getattr(ds, "RescaleIntercept", 0.0))
    # pixel_array is (frame=z, row=y, col=x); -> (x, y, z) to match the
    # origin + index*spacing convention used by volume_to_surface.
    hu = ds.pixel_array.astype(np.float32) * slope + intercept
    array = np.ascontiguousarray(hu.transpose(2, 1, 0))
    return array, spacing, origin, dims


def _load_nifti(path: Path):
    try:
        import nibabel as nib
    except ImportError as exc:
        raise RuntimeError(
            "NIfTI input needs nibabel — install with: pip install nibabel"
        ) from exc
    img = nib.load(str(path))
    # Bound the voxel count from the HEADER shape, without allocating: get_fdata()
    # materialises the whole array (and scales it to float64), so an oversized or
    # crafted NIfTI would exhaust memory before volume_to_surface's MAX_VOXELS gate
    # ever runs.  Refuse loud first (F-IMG-7).
    from meshforge.geometry.centerline import MAX_VOXELS

    shape = tuple(int(s) for s in img.header.get_data_shape())
    if int(np.prod(shape)) > MAX_VOXELS:
        raise ValueError(
            f"{path.name} volume {shape} exceeds {MAX_VOXELS} voxels; "
            f"downsample before ingest"
        )
    affine = np.asarray(img.affine, dtype=float)
    rot = affine[:3, :3]
    off_diag = rot - np.diag(np.diag(rot))
    scale = max(1.0, float(np.abs(np.diag(rot)).max()))
    if float(np.abs(off_diag).max()) > 1e-6 * scale:
        raise ValueError(
            f"{path.name} has a rotated/sheared affine — re-orient the volume "
            f"to axis-aligned (e.g. nibabel as_closest_canonical) first; "
            f"refusing to silently mis-orient anatomy"
        )
    if np.any(np.diag(rot) <= 0):
        raise ValueError(
            f"{path.name} has non-positive affine scales {np.diag(rot)} — "
            f"re-orient to a positive axis-aligned affine first"
        )
    array = np.asarray(img.get_fdata(), dtype=float)
    spacing = tuple(float(v) for v in np.diag(rot))
    origin = tuple(float(v) for v in affine[:3, 3])
    return array, spacing, origin


def _load_dicom_series(dirpath: Path):
    try:
        import pydicom
        from pydicom.errors import InvalidDicomError
    except ImportError as exc:
        raise RuntimeError(
            "DICOM input needs pydicom — install with: pip install pydicom"
        ) from exc
    files = sorted(p for p in dirpath.iterdir() if p.is_file())
    slices = []
    for f in files:
        try:
            ds = pydicom.dcmread(str(f))
        except InvalidDicomError:
            # A genuine non-DICOM straggler (no 'DICM' header) — skip it.  The
            # except is NARROW on purpose: a file that IS a DICOM yet is corrupt
            # raises a *different* error, which must SURFACE rather than being
            # swallowed into a silently-truncated series (F-IMG-6).
            continue
        if hasattr(ds, "PixelData"):
            slices.append(ds)
    if not slices:
        raise ValueError(f"no DICOM slices with pixel data found in {dirpath}")
    # Bound the stacked voxel count BEFORE reading any pixel data — dcmread is
    # lazy, so nothing large is allocated yet.  The multi-frame loader has this
    # ceiling; the directory-series path did not, so a crafted/oversized series
    # could np.stack straight into memory (a DoS).  Refuse loud once the declared
    # dims are known (F-IMG-6).
    from meshforge.geometry.centerline import MAX_VOXELS

    rows, cols = int(slices[0].Rows), int(slices[0].Columns)
    n_vox = rows * cols * len(slices)
    if n_vox > MAX_VOXELS:
        raise ValueError(
            f"DICOM series in {dirpath} declares {cols}x{rows}x{len(slices)} = "
            f"{n_vox} voxels, exceeding {MAX_VOXELS}; downsample before ingest"
        )
    try:
        slices.sort(key=lambda ds: float(ds.ImagePositionPatient[2]))
        zs = np.array([float(ds.ImagePositionPatient[2]) for ds in slices])
    except AttributeError as exc:
        raise ValueError(
            f"DICOM series in {dirpath} lacks ImagePositionPatient — cannot "
            f"order slices safely"
        ) from exc
    gaps = np.diff(zs)
    if len(gaps) == 0:
        raise ValueError(f"DICOM series in {dirpath} has a single slice")
    if gaps.min() <= 0 or (gaps.max() - gaps.min()) > 0.01 * gaps.mean():
        raise ValueError(
            f"DICOM series in {dirpath} has non-uniform slice spacing "
            f"({gaps.min():.6g}..{gaps.max():.6g}) — resample first"
        )
    first = slices[0]
    row_sp, col_sp = (float(v) for v in first.PixelSpacing)
    stack = []
    for ds in slices:
        arr = ds.pixel_array.astype(float)
        slope = float(getattr(ds, "RescaleSlope", 1.0))
        intercept = float(getattr(ds, "RescaleIntercept", 0.0))
        stack.append(arr * slope + intercept)
    # pixel_array is (row, col) = (y, x); build (x, y, z).
    array = np.stack(stack, axis=-1).transpose(1, 0, 2)
    spacing = (col_sp, row_sp, float(gaps.mean()))
    origin = tuple(float(v) for v in first.ImagePositionPatient)
    return array, spacing, origin


def volume_input_sha256(path: str | Path) -> str:
    """Checksum for rule-1 bookkeeping: file sha256, or for a DICOM
    directory the sha256 of the sorted per-file digests."""
    from meshforge.io.load import file_sha256

    path = Path(path)
    if path.is_file():
        return file_sha256(path)
    if path.is_dir():
        digest = hashlib.sha256()
        for f in sorted(p for p in path.iterdir() if p.is_file()):
            digest.update(f.name.encode())
            digest.update(bytes.fromhex(file_sha256(f)))
        return digest.hexdigest()
    raise FileNotFoundError(f"volume not found: {path}")

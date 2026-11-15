"""Scan upload (D115, the Lab): a CT sent as bytes -> a read-only copy.

Every other Workbench input is read by path on this machine (the server is
127.0.0.1-only). The Lab's "Upload scan" button sends the file's bytes
instead, and they land here: in a temp folder made on first use and removed
when the server stops (CLAUDE.md rule 2: never the repo; a patient scan does
not linger beside the code), one subfolder per content hash so the same scan
sent twice is one file, the file keeping its own name (made safe) so the
step's label and the journal read like the original. Chunks are written as
they arrive (nothing is held in memory) under a size ceiling; the name must
carry a CT suffix and the first bytes must look like that format, so a wrong
file is refused at once instead of minutes later inside TotalSegmentator.
The copy is made read-only, and its sha256 is returned so it can be checked
against the original (rule 1).
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import tempfile
import uuid
from pathlib import Path, PurePosixPath
from typing import Any

#: The CT formats the segment steps read (``io/segment.CT_SUFFIXES``).
SCAN_SUFFIXES = (".nrrd", ".nii", ".nii.gz")
#: The CT ceiling of the segment steps is 512 x 512 x 2048 voxels; as float32
#: that is 2 GiB, so no CT those steps accept is refused here.
MAX_SCAN_BYTES = 2 * 1024**3
_MAX_NAME = 120
_HEAD = 4
SUFFIX_HELP = (
    "Upload scan takes one CT volume file: .nii, .nii.gz or .nrrd (a DICOM "
    "series is a folder of files: convert it to one NIfTI first, e.g. with "
    "dcm2niix)"
)


def scan_name(raw: str) -> str:
    """The file's own name, made safe to keep: the last path component only,
    every character outside ``A-Za-z0-9._-`` replaced, no leading dot, at
    most ``_MAX_NAME`` characters (the suffix kept). Loud when it is not a
    CT file name."""
    base = PurePosixPath(str(raw).replace("\\", "/")).name.strip()
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", base).lstrip(".")
    suffix = next((s for s in sorted(SCAN_SUFFIXES, key=len, reverse=True)
                   if safe.lower().endswith(s)), None)
    if suffix is None or len(safe) <= len(suffix):
        raise ValueError(f"{base or '(no name)'!r}: {SUFFIX_HELP}")
    if len(safe) > _MAX_NAME:
        safe = safe[: _MAX_NAME - len(suffix)] + safe[-len(suffix):]
    return safe


def _check_head(name: str, head: bytes) -> None:
    """The first bytes must be the declared format's: ``NRRD`` magic, the
    gzip magic for .nii.gz, a NIfTI-1/2 header size for .nii."""
    low = name.lower()
    if low.endswith(".nrrd"):
        ok = head.startswith(b"NRRD")
        what = "an NRRD file (it does not start with 'NRRD')"
    elif low.endswith(".nii.gz"):
        ok = head.startswith(b"\x1f\x8b")
        what = "a gzip-compressed NIfTI (it is not gzip data)"
    else:
        sizes = {int.from_bytes(head[:4], "little"), int.from_bytes(head[:4], "big")}
        ok = len(head) >= 4 and bool(sizes & {348, 540})
        what = "a NIfTI file (no NIfTI-1/2 header)"
    if not ok:
        raise ValueError(f"{name!r} is not {what}: {SUFFIX_HELP}")


class ScanUpload:
    """One upload in flight: ``write`` each chunk, then ``finish`` (or
    ``abort``, which removes the partial file)."""

    def __init__(self, store: "UploadStore", name: str) -> None:
        self.name = scan_name(name)
        self._store = store
        self._part = store.root / f".incoming-{uuid.uuid4().hex}.part"
        self._fh = self._part.open("wb")
        self._sha = hashlib.sha256()
        self._head = b""
        self.n_bytes = 0

    def write(self, chunk: bytes) -> None:
        if not chunk:
            return
        self.n_bytes += len(chunk)
        if self.n_bytes > MAX_SCAN_BYTES:
            self.abort()
            raise ValueError(
                f"{self.name}: over the {MAX_SCAN_BYTES // 1024**3} GiB scan ceiling"
            )
        if len(self._head) < _HEAD:
            self._head += chunk[: _HEAD - len(self._head)]
        self._sha.update(chunk)
        self._fh.write(chunk)

    def finish(self) -> dict[str, Any]:
        self._fh.close()
        try:
            if self.n_bytes == 0:
                raise ValueError(f"{self.name}: the upload is empty")
            _check_head(self.name, self._head)
        except ValueError:
            self.abort()
            raise
        digest = self._sha.hexdigest()
        final = self._store.root / digest[:16] / self.name
        reused = final.is_file()
        if reused:
            self._part.unlink(missing_ok=True)
        else:
            final.parent.mkdir(exist_ok=True)
            os.replace(self._part, final)
            final.chmod(0o444)  # an input: read-only (rule 1)
        return {
            "path": str(final),
            "name": self.name,
            "bytes": self.n_bytes,
            "sha256": digest,
            "reused": reused,
        }

    def abort(self) -> None:
        if not self._fh.closed:
            self._fh.close()
        self._part.unlink(missing_ok=True)


class UploadStore:
    """The server's upload folder: a temp dir made on first use (``root``),
    removed by ``cleanup`` when the server stops."""

    def __init__(self) -> None:
        self._root: Path | None = None

    @property
    def root(self) -> Path:
        if self._root is None or not self._root.is_dir():
            self._root = Path(tempfile.mkdtemp(prefix="meshforge_upload_"))
        return self._root

    def begin(self, name: str) -> ScanUpload:
        return ScanUpload(self, name)

    def cleanup(self) -> None:
        if self._root is None:
            return
        for p in self._root.rglob("*"):  # the copies are read-only (Windows refuses those)
            try:
                p.chmod(0o600 if p.is_file() else 0o700)
            except OSError:
                pass
        shutil.rmtree(self._root, ignore_errors=True)
        self._root = None

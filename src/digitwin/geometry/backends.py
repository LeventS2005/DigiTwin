from __future__ import annotations

from typing import Callable

from digitwin.geometry.centerline import compute_centerline
from digitwin.geometry.frames import parallel_transport_frames
from digitwin.geometry.sections import compute_sections, detect_constrictions
from digitwin.geometry.wall import offset_wall


class NativeGeometryBackend:
    """Pure VTK/scipy/scikit-image/networkx geometry — no fragile deps."""

    name = "native"
    compute_centerline = staticmethod(compute_centerline)
    parallel_transport_frames = staticmethod(parallel_transport_frames)
    compute_sections = staticmethod(compute_sections)
    detect_constrictions = staticmethod(detect_constrictions)
    offset_wall = staticmethod(offset_wall)


_REGISTRY: dict[str, Callable[[], object]] = {
    "native": NativeGeometryBackend,
}


def get_backend(name: str = "native"):
    try:
        return _REGISTRY[name]()
    except KeyError:
        raise ValueError(
            f"unknown geometry backend {name!r}; available: {', '.join(sorted(_REGISTRY))}"
        ) from None

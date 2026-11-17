"""Environment-variable settings: ``DIGITWIN_<X>`` first, then ``MESHFORGE_<X>``.

The product was renamed from meshforge to DigiTwin in Nov 2026. Shell
profiles, scripts and CI jobs set up before the rename still export
``MESHFORGE_*`` names, so the old prefix is still read (after the new one)
rather than silently ignored. Every settings read in the package goes through
here so the two prefixes are honoured the same way everywhere.

An empty string counts as unset (the next prefix is tried); values are
returned as set, never stripped. Pure stdlib.
"""

from __future__ import annotations

import os

PREFIXES = ("DIGITWIN_", "MESHFORGE_")


def env_lookup(key: str) -> tuple[str | None, str | None]:
    """``(variable_name, raw_value)`` of the first non-empty ``<PREFIX><key>``.

    Prefixes are tried in order (``DIGITWIN_`` then ``MESHFORGE_``). Returns
    ``(None, None)`` when neither is set to a non-empty value. The name lets a
    caller's error message say which variable supplied the value.
    """
    for prefix in PREFIXES:
        name = prefix + key
        value = os.environ.get(name)
        if value:
            return name, value
    return None, None


def env_value(key: str, default: str | None = None) -> str | None:
    """The value from :func:`env_lookup`, else ``default``."""
    _name, value = env_lookup(key)
    return value if value is not None else default

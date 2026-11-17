#!/bin/bash
# conda-build script for the digitwin noarch package.
set -euo pipefail

# Install the pure-Python package (deps are declared in meta.yaml `run:` and
# resolved by conda; --no-deps keeps pip from re-pulling them from PyPI).
$PYTHON -m pip install . --no-deps --no-build-isolation -vv

# Ship the menuinst launcher so constructor/menuinst creates a double-click .app
# that opens the Workbench (runs `digitwin-desktop`).
mkdir -p "$PREFIX/Menu"
cp "$RECIPE_DIR/menu.json" "$PREFIX/Menu/digitwin.json"

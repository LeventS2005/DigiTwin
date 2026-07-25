#!/bin/bash
# Session 30 — reproducible build of the self-contained meshforge LINUX
# installer. Runs natively on a linux-64 machine or CI runner (ubuntu-latest):
#
#   bash packaging/build_linux.sh   ->   dist/meshforge-<ver>-Linux-x86_64.sh
#
# Prerequisites (base conda env): constructor + conda-build — on CI the
# installer-linux workflow sets them up. Mirrors build.sh (the proven V37
# macOS path) step for step; the only differences are the construct-linux
# recipe (sh installer only) and that this script is expected to run ON
# linux-64 (constructor cannot cross-build; probe, session 30). The solvers
# (CalculiX/OpenFOAM/MYSTRAN) are NOT bundled — external/optional, runtime-
# found. No built binaries are committed: dist/ and the generated build dirs
# are git-ignored (hard rule 2).
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
CONDA="${CONDA_EXE:-$(command -v conda)}"
SRC="$HERE/_build_src"
LOCAL_CH="$HERE/local-channel"
DIST="$REPO/dist"

# conda-build's finalize solve needs the libmamba solver (D57 — the classic
# solver rejects the system virtual packages on conda 26 and dies).
export CONDA_SOLVER=libmamba

echo "==> 0/3  stage a clean source tree (tracked files + working-tree edits; no outputs/, .git, DWG)"
rm -rf "$SRC"; mkdir -p "$SRC"
REV="$(git -C "$REPO" stash create)"; REV="${REV:-HEAD}"
git -C "$REPO" archive "$REV" | tar -x -C "$SRC"

echo "==> 1/3  build the meshforge noarch conda package"
rm -rf "$LOCAL_CH"; mkdir -p "$LOCAL_CH"
"$CONDA" run -n base conda-build "$HERE/conda-recipe" -c conda-forge \
    --no-test --no-anaconda-upload --output-folder "$LOCAL_CH"
"$CONDA" run -n base conda index "$LOCAL_CH"

echo "==> 2/3  build the .sh installer with constructor"
mkdir -p "$DIST"
# constructor resolves the relative ../local-channel against the CWD (D57
# trap) — run it from the construct-linux recipe dir.
( cd "$HERE/construct-linux" && "$CONDA" run -n base constructor . --output-dir "$DIST" )

echo "==> 3/3  artifacts:"
ls -la "$DIST"/meshforge-*.sh

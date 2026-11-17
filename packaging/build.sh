#!/bin/bash
# V37 — reproducible build of the self-contained digitwin macOS installer.
#
#   bash packaging/build.sh   ->   dist/digitwin-<ver>-MacOSX-arm64.pkg (+ .sh)
#
# Dev-machine prerequisites (base conda env): constructor + conda-build. Target
# is osx-arm64 (the whole conda-forge Qt/VTK stack is arm64). The solvers
# (CalculiX/OpenFOAM/MYSTRAN) are NOT bundled — they stay external/optional and
# are auto-discovered at runtime. No built binaries are committed: dist/ and the
# generated build dirs are git-ignored (hard rule 2).
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
CONDA="${CONDA:-$(command -v conda || echo "$HOME/miniconda3/bin/conda")}"
SRC="$HERE/_build_src"
LOCAL_CH="$HERE/local-channel"
DIST="$REPO/dist"

# conda-build's finalize solve needs the libmamba solver — the classic solver
# (this machine's condarc default) rejects the system virtual packages
# (__osx/__unix/__archspec/__conda) on conda 26 and dies. libmamba injects them.
export CONDA_SOLVER=libmamba

echo "==> 0/3  stage a clean source tree (tracked files + working-tree edits; no outputs/, .git, DWG)"
rm -rf "$SRC"; mkdir -p "$SRC"
# `git stash create` snapshots the working tree (tracked modifications included)
# without touching it; empty when the tree is clean -> fall back to HEAD. Either
# way only tracked files are staged (outputs/, dist/, the DWG stay out).
REV="$(git -C "$REPO" stash create)"; REV="${REV:-HEAD}"
git -C "$REPO" archive "$REV" | tar -x -C "$SRC"

echo "==> 1/3  build the digitwin noarch conda package"
rm -rf "$LOCAL_CH"; mkdir -p "$LOCAL_CH"
"$CONDA" run -n base conda-build "$HERE/conda-recipe" -c conda-forge \
    --no-test --no-anaconda-upload --output-folder "$LOCAL_CH"
"$CONDA" run -n base conda index "$LOCAL_CH"

echo "==> 2/3  build the installer(s) with constructor"
mkdir -p "$DIST"
# constructor resolves the relative `./local-channel` against the CWD, so run it
# from packaging/ (a subshell keeps this script's CWD untouched).
( cd "$HERE" && "$CONDA" run -n base constructor . --output-dir "$DIST" )

echo "==> 3/3  artifacts:"
ls -la "$DIST"/digitwin-*.pkg "$DIST"/digitwin-*.sh 2>/dev/null || true

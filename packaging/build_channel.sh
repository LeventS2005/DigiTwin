#!/bin/bash
# Session 31 — build the digitwin NOARCH conda package into the local
# channel (steps 0-1 of build.sh / build_linux.sh, extracted so the Windows
# CI flow can build the package on ubuntu and hand the channel to a
# windows-latest constructor job: the package is noarch = platform-portable,
# and conda-build's generated cmd wrapper failed with a parse error on the
# first windows-latest run — D69).
#
#   bash packaging/build_channel.sh   ->   packaging/local-channel/noarch/*.conda
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
CONDA="${CONDA_EXE:-$(command -v conda)}"
SRC="$HERE/_build_src"
LOCAL_CH="$HERE/local-channel"

# conda-build's finalize solve needs the libmamba solver (D57 — the classic
# solver rejects the system virtual packages on conda 26 and dies).
export CONDA_SOLVER=libmamba

echo "==> stage a clean source tree (tracked files + working-tree edits; no outputs/, .git, DWG)"
rm -rf "$SRC"; mkdir -p "$SRC"
REV="$(git -C "$REPO" stash create)"; REV="${REV:-HEAD}"
git -C "$REPO" archive "$REV" | tar -x -C "$SRC"

echo "==> build the digitwin noarch conda package"
rm -rf "$LOCAL_CH"; mkdir -p "$LOCAL_CH"
"$CONDA" run -n base conda-build "$HERE/conda-recipe" -c conda-forge \
    --no-test --no-anaconda-upload --output-folder "$LOCAL_CH"
"$CONDA" run -n base conda index "$LOCAL_CH"

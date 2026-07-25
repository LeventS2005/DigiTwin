#!/bin/bash
# Session 31 — reproducible build of the self-contained meshforge WINDOWS
# installer (.exe). Runs natively on a win-64 machine or runner under
# Git-Bash — constructor's NSIS tooling cannot cross-build (probe, session 30):
#
#   bash packaging/build_windows.sh  ->  dist/meshforge-<ver>-Windows-x86_64.exe
#
# Two modes:
#   default          — full build: noarch package (build_channel.sh) + exe.
#                      NOTE: conda-build's generated cmd wrapper failed with a
#                      parse error on the first windows-latest run (D69), so
#                      the native-Windows full build is UNTESTED; the CI path
#                      below is the verified one.
#   SKIP_PKG_BUILD=1 — CI path: packaging/local-channel/ already holds the
#                      noarch package (built on ubuntu — noarch is portable);
#                      only ensure the win-64 subdir exists + re-index, then
#                      run constructor.
#
# The exe is UNSIGNED — SmartScreen warns until a code-signing cert is bought
# (user decision, D69). No built binaries are committed: dist/ and the
# generated build dirs are git-ignored (hard rule 2).
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
CONDA="${CONDA_EXE:-$(command -v conda)}"
LOCAL_CH="$HERE/local-channel"
DIST="$REPO/dist"

export CONDA_SOLVER=libmamba

if [ "${SKIP_PKG_BUILD:-0}" = "1" ]; then
    echo "==> 1/3  SKIP_PKG_BUILD=1: using the prebuilt noarch channel"
    ls "$LOCAL_CH"/noarch/meshforge-*.conda >/dev/null \
        || { echo "ERROR: no meshforge package in $LOCAL_CH/noarch — build it first (build_channel.sh)"; exit 1; }
    # the solve targets win-64: make sure the subdir exists and is indexed
    mkdir -p "$LOCAL_CH/win-64"
    "$CONDA" run -n base conda index "$LOCAL_CH"
else
    echo "==> 1/3  build the noarch package into the local channel"
    bash "$HERE/build_channel.sh"
fi

echo "==> 2/3  build the .exe installer with constructor"
mkdir -p "$DIST"
# constructor resolves the relative ../local-channel against the CWD (D57
# trap) — run it from the construct-windows recipe dir.
( cd "$HERE/construct-windows" && "$CONDA" run -n base constructor . --output-dir "$DIST" )

echo "==> 3/3  artifacts:"
ls -la "$DIST"/meshforge-*.exe

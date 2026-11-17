# packaging/ — build the self-contained DigiTwin installer (V37 + Linux session 30 + Windows session 31)

Package DigiTwin so a user can launch the Workbench **without knowing conda**.
The deliverable is a native macOS installer that carries its own environment;
after installing, a double-click `digitwin` app opens the Workbench (a native
QtWebEngine window over the local 127.0.0.1 web UI). A **Linux `.sh`
installer** builds from the same noarch package (see below).

## What / why
The app rests on a tightly-matched **conda-forge / osx-arm64** native stack
(`qt6-main` + `qt6-webengine` 6.11, `pyside6`, `vtk`, `trame`). Pip freezers
(PyInstaller/py2app/briefcase) fight those native libs (the exit-134 "two Qts"
trap, VTK dylib hunting). **conda `constructor`** sidesteps all of it by
reproducing the exact packages that already work. The probe evidence behind that choice is in the project's design record.

## Build
```bash
bash packaging/build.sh
# -> dist/digitwin-1.0.0-MacOSX-arm64.pkg   (double-click installer)
# -> dist/digitwin-1.0.0-MacOSX-arm64.sh    (CLI / CI installer)
```
Prerequisites on the **build** machine (base conda env): `constructor` and
`conda-build`
(`conda install -n base -c conda-forge constructor conda-build`). Target is
**osx-arm64**. `dist/` and the generated `_build_src/` + `local-channel/` are
git-ignored — no built binaries are committed.

### How it works
1. `_build_src/` — a clean `git archive` of the tracked tree (no `outputs/`).
2. `conda-recipe/` — builds DigiTwin as a **noarch** conda package whose *run*
   deps are the full runtime, and which ships `Menu/digitwin.json` (the
   menuinst launcher).
3. `construct.yaml` — constructor installs `python 3.12` + `digitwin` from the
   local channel + conda-forge, and `menu_packages` makes menuinst create the
   `digitwin` **.app**.

## Linux installer (session 30)
```bash
bash packaging/build_linux.sh        # on a linux-64 machine
# -> dist/digitwin-1.0.0-Linux-x86_64.sh
```
Or trigger the **"Linux installer"** GitHub Actions workflow
(`.github/workflows/installer-linux.yml`, manual `workflow_dispatch`) — it
builds on `ubuntu-latest`, smoke-tests the install (CLI synth→volmesh + web
`/health`; **no GUI/WebGL assertion is possible headlessly** — Linux users
should prefer `digitwin web` until the desktop shell is validated on a real
Linux desktop) and uploads the `.sh` as a build artifact. Constructor cannot
cross-build (probe, session 30): `.sh` needs a linux runner, `.exe` a Windows
runner, `.pkg` macOS. The linux-64 conda-forge stack resolves with the same
matched `qt6-main`/`qt6-webengine` 6.11.1 pair (D54 rule holds).

## Windows installer (session 31)
```bash
bash packaging/build_windows.sh      # on a win-64 machine (Git-Bash)
# -> dist/digitwin-1.0.0-Windows-x86_64.exe
```
Or trigger the **"Windows installer"** GitHub Actions workflow
(`.github/workflows/installer-windows.yml`, manual `workflow_dispatch`) —
**two jobs**: the noarch DigiTwin package builds on `ubuntu-latest` (the
proven Linux path — conda-build's generated cmd wrapper failed with a parse
error on the first windows-latest run, D69; the package is
platform-portable), then a `windows-latest` job (constructor cannot
cross-build; NSIS `.exe` needs a Windows runner) runs constructor from the
handed-over channel, smoke-tests the **silent** install (`/S
/InstallationType=JustMe`) + CLI synth→volmesh + web `/health` (**no
GUI/WebGL or Start-Menu assertion is possible headlessly**) and uploads the
`.exe` as a build artifact. The win-64 conda-forge stack resolves with the
same matched `qt6-main`/`qt6-webengine` 6.11.1 pair (D54 rule holds;
session-30 probe). The Start-Menu shortcut comes from `menu.json`'s `win`
platform block (`Scripts\digitwin-desktop.exe` — entry points do NOT live
in `bin/` on Windows); `conda-recipe/bld.bat` remains for native-Windows
full builds (untested on CI).

**Unsigned exe — SmartScreen WILL warn** on first run: "Windows protected
your PC" → **More info** → **Run anyway**. Buying a Windows code-signing
certificate (EV cert removes the warning immediately; OV builds reputation
over time) is a user decision, exactly like Apple notarization.

## Install & launch (end user)
- Double-click `digitwin-1.0.0-MacOSX-arm64.pkg` → follow the wizard.
- Launch the **DigiTwin** app (created in Applications) → the Workbench opens.
- **Unsigned MVP:** Gatekeeper will warn. Right-click the `.pkg` → *Open*, or
  `xattr -dr com.apple.quarantine <file>`. Developer-ID signing + notarization
  is a later pull.

## Scope (honest)
- **Bundled:** python + the DigiTwin stack (Qt/QtWebEngine, VTK, trame, FastAPI,
  gmsh, ezdxf) — everything the mesh/export/viewport paths need.
- **NOT bundled:** the solvers **CalculiX** (`ccxsolve`), **OpenFOAM** (brew),
  **MYSTRAN** (`mystran`). They stay external and are auto-discovered at runtime
  (`DIGITWIN_CCX` / `DIGITWIN_OPENFOAM` / `DIGITWIN_MYSTRAN` → PATH → conda/brew).
  Meshing, export, and the 3-D viewport work without them; solve/cfd degrade
  loudly if a solver is absent. (The older `MESHFORGE_<X>` names are still read.)
- **osx-arm64 only** (the stack is arm64). Installed size ~1.5–2.5 GB — inherent
  to shipping Qt6 + QtWebEngine + VTK.

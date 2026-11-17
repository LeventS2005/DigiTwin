:: conda-build script for the digitwin noarch package — WINDOWS twin of
:: build.sh, kept for NATIVE Windows full builds only. UNTESTED on CI: the
:: first windows-latest conda-build run died with a cmd parse error in
:: conda-build's own generated wrapper (". was unexpected at this time.",
:: D69), so the CI flow builds the noarch package on ubuntu instead
:: (installer-windows.yml job 1) — the package is platform-portable.
%PYTHON% -m pip install . --no-deps --no-build-isolation -vv
if errorlevel 1 exit 1

:: Ship the menuinst launcher so constructor/menuinst creates a Start-Menu
:: shortcut that opens the Workbench (runs Scripts\digitwin-desktop.exe).
if not exist "%PREFIX%\Menu" mkdir "%PREFIX%\Menu"
copy "%RECIPE_DIR%\menu.json" "%PREFIX%\Menu\digitwin.json"
if errorlevel 1 exit 1

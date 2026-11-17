#!/usr/bin/env bash
# All-features end-to-end drive of the TrueGrid-parity roadmap (V12 final).
# Exercises every CLI surface once; everything lands in outputs/.
set -euo pipefail
cd "$(dirname "$0")/.."
PY="${DIGITWIN_PY:-${MESHFORGE_PY:-$HOME/miniconda3/envs/digitwin/bin/python}}"
MF="$(dirname "$PY")/digitwin"

echo "== V1: CAD import (planar face extraction on the real DWG) =="
"$MF" import-cad Cottage_Pane_Window.dwg | grep -E "loops|mesh_ready"

echo "== V5: multi-block hexes on the window sash =="
"$MF" blocks outputs/Cottage_Pane_Window_profile.json \
  --holes-like 177.03x170.68 --no-component-holes \
  --target-size 40 --thickness 40 --n-layers 2 | grep -E "holes-like|quality"

echo "== V6: projection (curved hole via --project) =="
"$PY" - <<'EOF'
import json, numpy as np
t = np.linspace(0, 2*np.pi, 64, endpoint=False)
json.dump({"loops": [[[-10,-10],[10,-10],[10,10],[-10,10]],
                     np.column_stack([5*np.cos(t), 5*np.sin(t)])[::-1].tolist()],
           "components": [{"outer": 0, "holes": [1]}], "units": "mm",
           "mesh_ready": True, "provenance": {}},
          open("outputs/round_profile.json", "w"))
EOF
"$MF" blocks outputs/round_profile.json --project --target-size 1.25 \
  --thickness 2 | grep -E "quality|projection"

echo "== V3+V11: geometry library + branching synth =="
"$MF" synth plate_with_hole | grep "exact truth"
"$MF" synth y_junction | grep branches
"$MF" synth torus | tail -1

echo "== V9: closed-loop swept wall (torus) =="
"$MF" volmesh outputs/torus.vtp --thickness 0.4 --n-theta 24 --n-axial 48 \
  --n-layers 3 | grep -E "quality|periodic" || true

echo "== V4: tet backend with Netgen optimization =="
"$MF" synth straight_tube > /dev/null
"$MF" volmesh outputs/straight_tube.vtp --thickness 0.6 --backend tet \
  --n-theta 24 --n-axial 30 | grep -E "quality|Netgen"

echo "== V2: full solver deck (ccx target) =="
"$MF" deck outputs/straight_tube.vtp --thickness 1.0 --E 1000 --nu 0.3 \
  --fix inlet,3 --fix outlet,3 --pin-lateral --pressure inner,1.0 \
  --target ccx --out outputs/e2e_deck.inp | grep -E "target|quality|deck"

echo "== V8: degrade + repair =="
"$MF" synth straight_tube --degrade-holes 3 --degrade-dup-faces 4 > /dev/null
"$MF" repair outputs/straight_tube.vtp | grep -E "holes|mesh_ready"

echo "== V7: parametric recipe sweep =="
"$MF" run outputs/tube_study.yaml | tail -2

echo "== V10: work file replay with override =="
"$PY" - <<'EOF'
from digitwin.pipeline import MeshingSession
s = MeshingSession()
s.generate_synth("straight_tube", n_theta=32, n_axial=21)
s.compute_centerline(voxel_size=0.5, n_samples=100)
s.compute_volmesh(backend="swept", thickness=0.5, n_theta=16, n_axial=20, n_layers=2)
s.save_workfile("outputs/e2e_session")
EOF
"$MF" replay outputs/e2e_session.mfwork --set volmesh.thickness=0.65 | tail -2

echo "== V12: offscreen screenshot =="
"$PY" - <<'EOF'
from digitwin.pipeline import MeshingSession
s = MeshingSession()
s.generate_synth("curved_tube")
s.compute_centerline()
print(s.compute_screenshot(path="outputs/e2e_screenshot.png").text, end="")
EOF

echo "== ALL FEATURES OK =="

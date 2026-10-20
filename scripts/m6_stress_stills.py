"""M6 video stills: the s1364 wall-stress maps, thrombus included.

Reuses ``scripts/m3_stress_maps.py`` unchanged (same views, colours and end-
zone lines) and only swaps its caption, which says "no thrombus modelled":
the caption here is built from the summary itself (thrombus modelled or not,
its modulus, the wall thickness).

Run from the repository root:

    python -I scripts/m6_stress_stills.py outputs/m6/stress/session_aaa_stress.vtu \
        outputs/m6/stress/s1364_stress --label "TotalSegmentator s1364 (Wasserthal et al.), CC BY 4.0"
"""

from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path


def _m3():
    path = Path(__file__).with_name("m3_stress_maps.py")
    spec = importlib.util.spec_from_file_location("m3_stress_maps", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def caption(summ: dict) -> str:
    t = summ.get("thrombus") or {}
    ilt = (f"intraluminal thrombus modelled (E {t['E_mpa']:g} MPa, tied to the wall)"
           if t.get("modelled") else "no thrombus modelled")
    return (f"investigational, not a diagnosis - wall thickness assumed {summ['wall']['thickness_mm']:g} mm"
            f" - {ilt}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("vtu", type=Path)
    ap.add_argument("out_stem", type=Path)
    ap.add_argument("--label", default="")
    a = ap.parse_args(argv)
    if not str(a.out_stem).startswith("outputs"):
        raise SystemExit("write under outputs/ only")
    m3 = _m3()
    m3.CAPTION = caption(json.loads(a.vtu.with_suffix(".json").read_text()))
    a.out_stem.parent.mkdir(parents=True, exist_ok=True)
    for path in m3.render(a.vtu, a.out_stem, a.label):
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

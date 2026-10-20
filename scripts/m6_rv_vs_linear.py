"""M6 chart: the Raghavan-Vorp AAA wall law vs the stiff-linear wall the stress step uses.

Why the stress step's wall is 100x stiffer than aneurysm tissue, on one page.
The CT shows the wall already pressurised, so the stress step solves a small-
strain linear model on that loaded geometry (BioPARR: Joldes & Miller): the
stiff wall barely moves (keeps the imaged shape) and the stress it carries is
set by equilibrium (pressure, geometry), not by the modulus. The tissue's own
law (Raghavan & Vorp) needs several per cent of stretch to carry the same
stress (the chart prints how much).

Curves are Cauchy stress vs stretch for an incompressible (R-V) or linear
isotropic membrane, equibiaxial (a pressurised wall) and uniaxial (a strip
test):

* Raghavan-Vorp, W = alpha (I1 - 3) + beta (I1 - 3)^2, incompressible:
  sigma = 2 (alpha + 2 beta (I1 - 3)) (lambda^2 - lambda_3^2), with
  I1 = 2 lambda^2 + lambda^-4 (equibiaxial; lambda_3^2 = lambda^-4) or
  I1 = lambda^2 + 2/lambda (uniaxial; lambda_3^2 = 1/lambda).
  alpha = 17.4 N/cm^2, beta = 188.1 N/cm^2 (population means; Raghavan ML,
  Vorp DA. J Biomech 2000;33(4):475-482).
* Linear, small strain (the stress step): sigma = E eps / (1 - nu)
  (equibiaxial, plane stress) or E eps (uniaxial), eps = lambda - 1, with E
  and nu read from meshforge.solve.aaa_metrics (DEFAULT_E_MPA, DEFAULT_NU).

The demo PWS and the wall's largest displacement come from a saved
``aaa_stress`` summary (``--summary``), never typed in.

Run from the repository root (this checkout's src/ goes first on the path):

    python -I scripts/m6_rv_vs_linear.py \
        outputs/m5/scripted_natural_faces/session_aaa_stress.json outputs/m6/rv_vs_linear
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))  # this checkout's solve defaults, never another install

N_PER_CM2_TO_MPA = 0.01  # 1 N/cm^2 = 1e4 Pa
RV_ALPHA_MPA = 17.4 * N_PER_CM2_TO_MPA
RV_BETA_MPA = 188.1 * N_PER_CM2_TO_MPA
RV_SOURCE = "Raghavan ML, Vorp DA. J Biomech 2000;33(4):475-482 (population means)"


def rv_stress(lam: np.ndarray, mode: str, alpha: float = RV_ALPHA_MPA, beta: float = RV_BETA_MPA) -> np.ndarray:
    """Raghavan-Vorp Cauchy stress (MPa) at stretch ``lam``."""
    if mode == "equibiaxial":
        i1, l3sq = 2 * lam**2 + lam**-4, lam**-4
    elif mode == "uniaxial":
        i1, l3sq = lam**2 + 2 / lam, 1 / lam
    else:
        raise ValueError(mode)
    return 2 * (alpha + 2 * beta * (i1 - 3)) * (lam**2 - l3sq)


def linear_stress(lam: np.ndarray, mode: str, E: float, nu: float) -> np.ndarray:
    eps = lam - 1.0
    return E * eps / (1 - nu) if mode == "equibiaxial" else E * eps


def stretch_at(stress_mpa: float, f, lo: float = 1.0, hi: float = 2.0) -> float:
    """The stretch at which the monotone curve ``f`` reaches ``stress_mpa``."""
    for _ in range(80):
        mid = 0.5 * (lo + hi)
        lo, hi = (mid, hi) if f(mid) < stress_mpa else (lo, mid)
    return 0.5 * (lo + hi)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("summary", type=Path, help="an aaa_stress summary .json (PWS, displacement, material)")
    ap.add_argument("out", type=Path, help="output stem")
    a = ap.parse_args(argv)

    from meshforge.solve.aaa_metrics import DEFAULT_E_MPA, DEFAULT_NU
    from meshforge.solve.ilt import DEFAULT_E_ILT_MPA

    summ = json.loads(a.summary.read_text())
    E, nu = float(summ["material"]["E_mpa"]), float(summ["material"]["nu"])
    if (E, nu) != (DEFAULT_E_MPA, DEFAULT_NU):
        raise SystemExit(f"the summary's wall ({E} MPa, nu {nu}) is not the stress step's default "
                         f"({DEFAULT_E_MPA} MPa, nu {DEFAULT_NU})")
    pws = float(summ["systolic"]["von_mises_p99_mpa"])
    p_mmhg = float(summ["systolic"]["pressure_mmhg"])
    umax = float(summ["systolic"]["u_max_mm"])

    lam_rv_eq = stretch_at(pws, lambda l: rv_stress(np.array(l), "equibiaxial"))
    lam_rv_ua = stretch_at(pws, lambda l: rv_stress(np.array(l), "uniaxial"))
    lam_lin_eq = 1 + pws * (1 - nu) / E
    e0_rv = 6 * RV_ALPHA_MPA  # small-strain Young's modulus of the R-V law (3 x shear modulus 2 alpha)
    h = 1e-6
    tangent_eq = (rv_stress(np.array(lam_rv_eq + h), "equibiaxial") - rv_stress(np.array(lam_rv_eq - h), "equibiaxial")) / (2 * h)
    numbers = {
        "pws_kpa": round(1000 * pws, 1), "pressure_mmhg": p_mmhg, "u_max_mm": round(umax, 3),
        "linear": {"E_mpa": E, "nu": nu, "source": "meshforge.solve.aaa_metrics DEFAULT_E_MPA / DEFAULT_NU "
                   "(the Zenodo AAA deck's wall, *Elastic 1e5 kPa)",
                   "strain_at_pws_equibiaxial_percent": round(100 * (lam_lin_eq - 1), 3)},
        "raghavan_vorp": {"alpha_mpa": RV_ALPHA_MPA, "beta_mpa": RV_BETA_MPA, "source": RV_SOURCE,
                          "small_strain_E_mpa": round(e0_rv, 3),
                          "strain_at_pws_equibiaxial_percent": round(100 * (lam_rv_eq - 1), 1),
                          "strain_at_pws_uniaxial_percent": round(100 * (lam_rv_ua - 1), 1),
                          "tangent_E_at_pws_equibiaxial_mpa": round(float(tangent_eq) * (1 - 0.5), 2)},
        "thrombus_E_mpa": DEFAULT_E_ILT_MPA,
        "note": "tangent_E is d(sigma)/d(lambda) x (1 - 0.5), the equibiaxial-membrane equivalent Young's "
                "modulus of an incompressible material",
    }

    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plt.rcParams.update({"font.family": "sans-serif", "font.size": 13})
    red, ink, grey, muted = "#c8312b", "#1a1f26", "#5d6672", "#8a939e"
    fig = plt.figure(figsize=(16, 9), dpi=120, facecolor="white")
    fig.text(0.05, 0.93, "Why the stress model's wall is stiffer than aneurysm tissue", fontsize=24,
             weight="bold", color=ink)
    fig.text(0.05, 0.895, "Cauchy stress vs strain: the Raghavan–Vorp AAA law vs the stiff-linear wall the "
             "stress step solves", fontsize=13, color=grey)

    ax = fig.add_axes([0.06, 0.15, 0.50, 0.68])
    lam = np.linspace(1.0, 1.20, 600)
    eps = 100 * (lam - 1)
    ax.plot(eps, 1000 * rv_stress(lam, "equibiaxial"), color=red, lw=3,
            label="Raghavan–Vorp, equibiaxial (pressurised wall)")
    ax.plot(eps, 1000 * rv_stress(lam, "uniaxial"), color=red, lw=2, ls="--",
            label="Raghavan–Vorp, uniaxial (strip test)")
    lin = 1000 * linear_stress(lam, "equibiaxial", E, nu)
    ax.plot(eps[lin <= 900], lin[lin <= 900], color=ink, lw=3,
            label=f"stiff-linear wall, E {E:g} MPa, ν {nu:g} (equibiaxial)")
    ax.axhline(1000 * pws, color=grey, lw=1.2, ls=":")
    ax.text(19.8, 1000 * pws + 16, f"demo peak wall stress\n{1000 * pws:.1f} kPa at {p_mmhg:g} mmHg",
            ha="right", va="bottom", fontsize=12, color=grey)
    ax.plot([100 * (lam_rv_eq - 1)], [1000 * pws], "o", color=red, ms=9, zorder=5)
    ax.annotate(f"tissue law: {100 * (lam_rv_eq - 1):.0f} % stretch", (100 * (lam_rv_eq - 1), 1000 * pws),
                xytext=(12.6, 175), fontsize=13, color=red,
                arrowprops={"arrowstyle": "-", "color": red, "lw": 1})
    ax.plot([100 * (lam_lin_eq - 1)], [1000 * pws], "o", color=ink, ms=9, zorder=5)
    ax.annotate(f"stiff wall: {100 * (lam_lin_eq - 1):.2f} % stretch", (100 * (lam_lin_eq - 1), 1000 * pws),
                xytext=(2.0, 1000 * pws + 170), fontsize=13, color=ink,
                arrowprops={"arrowstyle": "-", "color": ink, "lw": 1})
    ax.set_xlim(0, 20)
    ax.set_ylim(0, 800)
    ax.set_xlabel("strain (stretch − 1), %")
    ax.set_ylabel("Cauchy stress (kPa)")
    ax.spines[["top", "right"]].set_visible(False)
    ax.legend(frameon=False, loc="lower right", fontsize=12)

    x0 = 0.62
    rows = [
        ("Raghavan–Vorp (tissue)", f"α {RV_ALPHA_MPA * 1000:.0f} kPa, β {RV_BETA_MPA * 1000:.0f} kPa", red),
        ("", f"small-strain E ≈ 6α = {e0_rv:.2f} MPa", red),
        ("Stress step (solver)", f"E {E:g} MPa, ν {nu:g}; thrombus E {DEFAULT_E_ILT_MPA:g} MPa", ink),
    ]
    fig.text(x0, 0.80, "Parameters", fontsize=15, weight="bold", color=ink)
    for i, (k, v, c) in enumerate(rows):
        y = 0.755 - i * 0.04
        fig.text(x0, y, k, fontsize=13, color=grey)
        fig.text(x0 + 0.15, y, v, fontsize=13, color=c)
    fig.text(x0, 0.60, "Why stiff on purpose", fontsize=15, weight="bold", color=ink)
    why = [
        "The CT shows the wall already under blood pressure,",
        "so the solver keeps that imaged shape: at the demo",
        f"pressure the wall moves at most {umax:.2f} mm.",
        "On the loaded shape the stress is set by equilibrium",
        "(pressure, radius, thickness), not by the stiffness:",
        "ten times stiffer leaves the peak stress unchanged",
        "(meshforge M3 check, D88).",
        f"The tissue law would need ~{100 * (lam_rv_eq - 1):.0f} % stretch to carry",
        "the same stress, and a growth-from-zero-pressure",
        "solve the CT cannot give.",
    ]
    for i, line in enumerate(why):
        fig.text(x0, 0.56 - i * 0.034, line, fontsize=13, color=ink)
    fig.text(0.05, 0.05, f"Tissue law: {RV_SOURCE}; α 17.4, β 188.1 N/cm².", fontsize=10.5, color=muted)
    fig.text(0.05, 0.03, "Stiff-linear method on the loaded geometry: Joldes & Miller (BioPARR). Solver wall and "
             "thrombus moduli: the Zenodo AAA FE deck (meshforge.solve defaults).", fontsize=10.5, color=muted)
    fig.text(0.05, 0.01, f"Demo numbers from {a.summary.name} (s1364, copilot natural path). Investigational, "
             "not a diagnosis.", fontsize=10.5, color=muted)
    a.out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(a.out.with_suffix(".png"), dpi=120, facecolor="white")
    plt.close(fig)
    a.out.with_suffix(".json").write_text(json.dumps(numbers, indent=1) + "\n")
    print(json.dumps(numbers, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

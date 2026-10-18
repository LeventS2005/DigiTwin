"""Axial overlay PNGs of a recovered sac (D86): CT at W500/L100, the lumen
mask contour in red, the sac mask (written by ``meshforge sac``) in green.

Heights are millimetres above the lumen mask's caudal end (lowest world z),
the same origin the sac summary uses. Displayed radiologically: patient
right on the image left, anterior up. The sac mask may sit on a sub-box of
the CT grid; it must share the CT's voxel axes (integer offset), else refused.

    python -I scripts/sac_overlay.py CT LUMEN_MASK SAC_MASK OUT_DIR TAG [H ...]
"""
import sys
from pathlib import Path

import matplotlib
import nibabel as nib
import numpy as np

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

WINDOW, LEVEL = 500.0, 100.0
HALF_MM = 60.0


def main(ct_p, lumen_p, sac_p, out_dir, tag, heights):
    ct, lum, sac = nib.load(ct_p), nib.load(lumen_p), nib.load(sac_p)
    a = ct.affine
    if lum.shape != ct.shape or not np.allclose(lum.affine, a, atol=1e-3):
        raise SystemExit("refusing: the lumen mask is not on the CT grid")
    off = np.linalg.inv(a) @ sac.affine
    if not np.allclose(off[:3, :3], np.eye(3), atol=1e-4) or not np.allclose(off[:3, 3], np.rint(off[:3, 3]), atol=1e-3):
        raise SystemExit("refusing: the sac mask is not an integer sub-box of the CT grid")
    lo = np.rint(off[:3, 3]).astype(int)
    lumen = np.asanyarray(lum.dataobj) > 0
    sac_m = np.asanyarray(sac.dataobj) > 0
    zk = a[2, 2] * np.arange(ct.shape[2]) + a[2, 3]
    z_low = float(zk[lumen.any(axis=(0, 1))].min())
    sp = np.linalg.norm(a[:3, :2], axis=0)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    for h in heights:
        k = int(np.argmin(np.abs(zk - (z_low + h))))
        L = lumen[:, :, k]
        if not L.any():
            raise SystemExit(f"no lumen at h={h} mm")
        ci, cj = (int(v.mean()) for v in np.nonzero(L))
        hi_, hj = int(HALF_MM / sp[0]), int(HALF_MM / sp[1])
        i0, i1 = max(ci - hi_, 0), min(ci + hi_, ct.shape[0])
        j0, j1 = max(cj - hj, 0), min(cj + hj, ct.shape[1])
        hu = np.asarray(ct.dataobj[i0:i1, j0:j1, k], dtype=np.float32)
        S = np.zeros_like(L)
        ks = k - lo[2]
        if 0 <= ks < sac_m.shape[2]:
            S[lo[0]:lo[0] + sac_m.shape[0], lo[1]:lo[1] + sac_m.shape[1]] = sac_m[:, :, ks]
        views = [hu, L[i0:i1, j0:j1], S[i0:i1, j0:j1]]
        # radiological display: +i must point to patient LEFT, +j to ANTERIOR
        if a[0, 0] > 0:
            views = [v[::-1] for v in views]
        if a[1, 1] < 0:
            views = [v[:, ::-1] for v in views]
        hu, Lv, Sv = views
        fig, ax = plt.subplots(figsize=(7, 7))
        ax.imshow(np.clip(hu.T, LEVEL - WINDOW / 2, LEVEL + WINDOW / 2), cmap="gray",
                  origin="lower", interpolation="nearest")
        ax.contour(Lv.T.astype(float), [0.5], colors="red", linewidths=1.0)
        if Sv.any():
            ax.contour(Sv.T.astype(float), [0.5], colors="lime", linewidths=1.2)
        ax.set_xticks([])
        ax.set_yticks([])
        ax.text(0.01, 0.5, "R", color="yellow", transform=ax.transAxes, fontsize=12)
        ax.text(0.97, 0.5, "L", color="yellow", transform=ax.transAxes, fontsize=12)
        ax.text(0.5, 0.96, "A", color="yellow", transform=ax.transAxes, fontsize=12)
        ax.set_title(f"{tag}  h = {zk[k] - z_low:.1f} mm above the lumen's lower end  "
                     f"(W{WINDOW:g}/L{LEVEL:g})\nred = lumen mask, green = recovered sac",
                     fontsize=9)
        fig.tight_layout()
        path = out_dir / f"{tag}_sac_h{int(round(h)):02d}.png"
        fig.savefig(path, dpi=110)
        plt.close(fig)
        print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:6], [float(h) for h in (sys.argv[6:] or ["12", "25", "40"])]))

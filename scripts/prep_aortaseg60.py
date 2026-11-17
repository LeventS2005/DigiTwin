"""Prepare an AortaSeg-60 CT for digitwin: correct its head-foot header sign.

AortaSeg-60 (doi 10.5281/zenodo.18147026, CC BY 4.0, El Rahal, Rotzinger &
Fahrni) ships each CT with a header whose z axis points the wrong way: slice
0 is the lung apex, yet the affine says z increases toward the head. The
shipped TotalSegmentator mask on the same grid stores its slices in the
opposite order with an anatomically correct header. Left as-is, every
surface built from the CT is a head-foot mirror image.

This script does not assume the defect. It uses the shipped mask as a
witness: the mean HU under the mask's voxels must be contrast-bright aorta
when the mask is laid on the CT in the corrected slice order, and not when
it is laid on as-is. Only then is the CT's z column negated (origin kept, so
slice 0 stays where it is in the world and the world extent becomes the
mask's). The voxel data are copied unchanged. Anything else is refused.

    python -I scripts/prep_aortaseg60.py CT.nii.gz SHIPPED_MASK.nii.gz OUT.nii.gz
"""

import json
import sys
from pathlib import Path

import nibabel as nib
import numpy as np

#: contrast-enhanced aortic blood; unenhanced tissue sits far below
_BRIGHT_HU = 150.0


def main(ct_path: str, mask_path: str, out_path: str) -> int:
    ct = nib.load(ct_path)
    mk = nib.load(mask_path)
    if ct.shape != mk.shape or ct.ndim != 3:
        raise SystemExit(f"refusing: CT {ct.shape} and mask {mk.shape} grids differ")
    a_ct, a_mk = ct.affine, mk.affine
    n = ct.shape[2]
    dz = a_ct[2, 2]
    # the defect: same in-plane frame, same slice spacing, z extents mirrored
    mirrored_origin = a_ct[2, 3] - (n - 1) * dz
    if not (
        np.allclose(a_ct[:2, :], a_mk[:2, :], atol=1e-3)
        and np.isclose(a_mk[2, 2], dz, atol=1e-4)
        and np.isclose(a_mk[2, 3], mirrored_origin, atol=0.05)
    ):
        raise SystemExit("refusing: the headers do not show the known head-foot mirror")

    hu = np.asanyarray(ct.dataobj)
    mask = np.asanyarray(mk.dataobj) > 0.5
    as_is = float(hu[mask].mean())
    flipped = float(hu[mask[:, :, ::-1]].mean())
    print(f"mean HU under the shipped mask: as-is {as_is:.0f}, slice-reversed {flipped:.0f}")
    if not (flipped >= _BRIGHT_HU and as_is < _BRIGHT_HU):
        raise SystemExit(
            "refusing: the shipped mask does not confirm a reversed CT header "
            f"(as-is {as_is:.0f} HU, reversed {flipped:.0f} HU)"
        )

    fixed = a_ct.copy()
    fixed[:3, 2] = -a_ct[:3, 2]  # slice k now steps toward the feet
    out = nib.Nifti1Image(ct.dataobj, fixed, header=ct.header)
    out.set_qform(fixed, code=1)
    out.set_sform(fixed, code=1)
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    nib.save(out, out_path)
    note = {
        "source": str(ct_path),
        "witness_mask": str(mask_path),
        "correction": "negated the z column of the affine; voxel data unchanged",
        "mean_hu_under_mask": {"as_is": round(as_is, 1), "slice_reversed": round(flipped, 1)},
        "affine_before": np.round(a_ct, 6).tolist(),
        "affine_after": np.round(fixed, 6).tolist(),
        "z_extent_after_mm": [float(fixed[2, 3]), float(fixed[2, 3] + (n - 1) * fixed[2, 2])],
    }
    Path(out_path + ".prep.json").write_text(json.dumps(note, indent=1))
    print(f"wrote {out_path} (z {note['z_extent_after_mm'][0]:.1f} -> {note['z_extent_after_mm'][1]:.1f} mm)")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 4:
        raise SystemExit(__doc__)
    sys.exit(main(*sys.argv[1:]))

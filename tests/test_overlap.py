"""D83 validation metrics on analytic shapes (closed-form Dice, volumes and
surface distances) and the Zenodo AAA reference adapter on a synthetic case
folder that mimics the dataset's SurfID patch layout."""
from __future__ import annotations

import numpy as np
import pytest
import pyvista as pv

from meshforge.quality.overlap import (
    compare_to_regions,
    dice,
    sample_surface,
    surface_distance,
    voxelize_surface,
)


def _rot(axis: str, deg: float) -> np.ndarray:
    t = np.radians(deg)
    c, s = np.cos(t), np.sin(t)
    if axis == "z":
        return np.array([[c, -s, 0], [s, c, 0], [0, 0, 1.0]])
    return np.array([[1.0, 0, 0], [0, c, -s], [0, s, c]])


def _grid(lin, center, shape):
    a = np.eye(4)
    a[:3, :3] = lin
    a[:3, 3] = np.asarray(center, float) - lin @ (np.asarray(shape) / 2.0)
    return a


def _sphere(r, c=(0, 0, 0)):
    return pv.Sphere(radius=r, center=c, theta_resolution=160, phi_resolution=160)


@pytest.mark.parametrize(
    "lin",
    [
        np.diag([0.25, 0.25, 0.25]),
        _rot("z", 30) @ _rot("x", 20) * 0.25,  # oblique grid
        np.diag([-0.3, -0.3, 0.4]),  # LPS-like flips
    ],
    ids=["axis", "oblique", "flipped"],
)
def test_offset_spheres_dice_matches_the_lens_formula(lin):
    r, d = 10.0, 5.0
    a_s, b_s = _sphere(r), _sphere(r, (d, 0, 0))
    shape = (170, 170, 170)
    aff = _grid(lin, (d / 2, 0, 0), shape)
    va, vb = voxelize_surface(a_s, shape, aff), voxelize_surface(b_s, shape, aff)
    vvox = abs(np.linalg.det(lin))
    sphere = 4.0 / 3.0 * np.pi * r**3
    assert va.sum() * vvox == pytest.approx(sphere, rel=0.005)
    lens = np.pi * (4 * r + d) * (2 * r - d) ** 2 / 12.0  # two-sphere intersection
    assert dice(va, vb) == pytest.approx(lens / sphere, abs=0.004)


def test_concentric_spheres_surface_distance_is_the_radius_gap():
    a, b = _sphere(10.0), _sphere(12.0)
    d = surface_distance(a, b, n_samples=8000)
    for key in ("assd", "hd95", "mean_a_to_b", "mean_b_to_a"):
        assert d[key] == pytest.approx(2.0, abs=0.02)
    assert d["n_a"] == d["n_b"] == 8000
    # Dice of concentric spheres: 2 r1^3 / (r1^3 + r2^3)
    shape = (120, 120, 120)
    aff = _grid(np.eye(3) * 0.25, (0, 0, 0), shape)
    va, vb = voxelize_surface(a, shape, aff), voxelize_surface(b, shape, aff)
    assert dice(va, vb) == pytest.approx(2 * 1000 / (1000 + 1728), abs=0.004)


def test_offset_spheres_surface_distance_matches_quadrature():
    """Directed mean distance from sphere A to sphere B (same radius, centres d
    apart): mean over A of | sqrt(R^2 + d^2 - 2 R d t) - R |, t = cos(theta)
    uniform on [-1, 1] for area-uniform points on a sphere."""
    r, d = 10.0, 3.0
    t = np.linspace(-1, 1, 200001)
    expected = np.abs(np.sqrt(r * r + d * d - 2 * r * d * t) - r).mean()
    got = surface_distance(_sphere(r), _sphere(r, (d, 0, 0)), n_samples=20000)
    assert got["mean_a_to_b"] == pytest.approx(expected, rel=0.02)
    assert got["assd"] == pytest.approx(expected, rel=0.02)


def test_sample_surface_is_area_uniform_and_z_filtered():
    cyl = pv.Cylinder(radius=5.0, height=20.0, direction=(0, 0, 1), capping=False,
                      resolution=200).triangulate()
    pts = sample_surface(cyl, 20000, seed=3)
    # area-uniform on a lateral cylinder -> z uniform on [-10, 10]
    assert abs(pts[:, 2].mean()) < 0.2
    assert np.histogram(pts[:, 2], bins=4, range=(-10, 10))[0].min() > 4500
    sub = sample_surface(cyl, 20000, seed=3, z_range=(0.0, 5.0))
    assert sub[:, 2].min() >= 0.0 and sub[:, 2].max() <= 5.0
    assert len(sub) == pytest.approx(5000, rel=0.06)
    with pytest.raises(ValueError, match="empty masks"):
        dice(np.zeros(3, bool), np.zeros(3, bool))


def _closed_cylinder(r, z0, z1, res=180):
    return pv.Cylinder(
        radius=r, height=z1 - z0, center=(0, 0, (z0 + z1) / 2), direction=(0, 0, 1),
        resolution=res, capping=True,
    ).triangulate()


def _open_cylinder(r, z0, z1, res=180):
    return pv.Cylinder(
        radius=r, height=z1 - z0, center=(0, 0, (z0 + z1) / 2), direction=(0, 0, 1),
        resolution=res, capping=False,
    ).triangulate()


def test_compare_to_regions_slab_dice_volumes_and_best_match():
    """A long segmented tube (r = 6, beyond the slab) against three coaxial
    reference tubes (r = 4, 6, 7.5) cut at z = +/-10: inside the slab the Dice
    are closed-form, and the r = 6 region is the best match."""
    seg = _closed_cylinder(6.0, -20.0, 20.0)
    radii = {"lumen": 4.0, "lumen_ilt": 6.0, "full": 7.5}
    regions = {k: _closed_cylinder(r, -10.0, 10.0) for k, r in radii.items()}
    walls = {k: _open_cylinder(r, -10.0, 10.0) for k, r in radii.items()}
    shape = (90, 90, 180)
    aff = _grid(np.diag([0.2, 0.2, 0.25]), (0, 0, 0), shape)
    out = compare_to_regions(seg, regions, walls, shape, aff, -10.0, 10.0, n_samples=6000)
    assert out["best"] == "lumen_ilt"
    for name, r in radii.items():
        reg = out["regions"][name]
        lo, hi = min(r, 6.0) ** 2, max(r, 6.0) ** 2
        assert reg["dice"] == pytest.approx(2 * lo / (lo + hi), abs=0.01)
        assert reg["volume_ml"] == pytest.approx(np.pi * r * r * 20.0 / 1000, rel=0.02)
        assert reg["distance"]["assd"] == pytest.approx(abs(r - 6.0), abs=0.03)
    # the segmented tube is counted only inside the slab
    assert out["segmented_volume_ml"] == pytest.approx(np.pi * 36 * 20.0 / 1000, rel=0.02)
    assert out["regions"]["lumen_ilt"]["dice"] > 0.98
    assert out["distance_z_mm"] == [-8.0, 8.0]
    with pytest.raises(ValueError, match="empty slab"):
        compare_to_regions(seg, regions, walls, shape, aff, 5.0, 5.0)


# ---------------------------------------------------------------------------
# Zenodo AAA adapter on a synthetic case folder
# ---------------------------------------------------------------------------
def _tag(mesh, sid):
    mesh = mesh.triangulate()
    mesh.cell_data["SurfID"] = np.full(mesh.n_cells, sid, dtype=np.int32)
    return mesh


def _annuli(r_in, r_out, z0, z1):
    discs = [
        pv.Disc(center=(0, 0, z), inner=r_in, outer=r_out, normal=(0, 0, 1), c_res=120)
        for z in (z0, z1)
    ]
    return discs[0].merge(discs[1])


def write_synthetic_zenodo_case(case_dir, *, r_lumen=4.0, r_ilt=6.0, z0=-10.0, z1=10.0,
                                wall=1.5):
    """ILTSurface (SurfID 1 annuli, 3 lumen, 4 interface) + WallSurface (2
    annuli, 4 inner, 5 outer) as coaxial tubes, in the dataset's layout."""
    case_dir.mkdir(parents=True, exist_ok=True)
    ilt = _tag(_annuli(r_lumen, r_ilt, z0, z1), 1).merge(
        _tag(_open_cylinder(r_lumen, z0, z1), 3)
    ).merge(_tag(_open_cylinder(r_ilt, z0, z1), 4))
    w = _tag(_annuli(r_ilt, r_ilt + wall, z0, z1), 2).merge(
        _tag(_open_cylinder(r_ilt, z0, z1), 4)
    ).merge(_tag(_open_cylinder(r_ilt + wall, z0, z1), 5))
    ilt.extract_surface(algorithm="dataset_surface").save(case_dir / "ILTSurface.vtp")
    w.extract_surface(algorithm="dataset_surface").save(case_dir / "WallSurface.vtp")
    return case_dir


def test_zenodo_reference_regions_from_a_synthetic_case(tmp_path):
    from meshforge.io.zenodo_aaa import load_reference, validate_against_reference

    case = write_synthetic_zenodo_case(tmp_path / "P99")
    ref = load_reference(case)
    assert (ref.z0, ref.z1) == (-10.0, 10.0)
    assert set(ref.regions) == {"lumen", "lumen_ilt", "full"}
    for name, r in (("lumen", 4.0), ("lumen_ilt", 6.0), ("full", 7.5)):
        closed = ref.regions[name]
        assert closed.n_open_edges == 0
        assert closed.volume == pytest.approx(np.pi * r * r * 20.0, rel=0.01)
        assert ref.walls[name].n_open_edges > 0  # the lateral patch stays open
    assert "10.5281/zenodo.19182977" in ref.notes[0]
    assert "reference segmentation" in ref.notes[0]

    seg = _closed_cylinder(7.5, -30.0, 30.0)
    shape = (100, 100, 100)
    aff = _grid(np.diag([0.2, 0.2, 0.25]), (0, 0, 0), shape)
    val = validate_against_reference(seg, case, shape, aff, n_samples=4000)
    assert val["best"] == "full" and val["regions"]["full"]["dice"] > 0.98
    assert val["case"] == "P99" and "CC BY 4.0" in val["reference"]


def test_zenodo_reference_refusals(tmp_path):
    from meshforge.io.zenodo_aaa import load_reference

    with pytest.raises(FileNotFoundError):
        load_reference(tmp_path / "nope")
    case = write_synthetic_zenodo_case(tmp_path / "P98")
    w = pv.read(case / "WallSurface.vtp")
    del w.cell_data["SurfID"]
    w.save(case / "WallSurface.vtp")
    with pytest.raises(ValueError, match="no 'SurfID' cell array"):
        load_reference(case)

    case = write_synthetic_zenodo_case(tmp_path / "P97")
    w = pv.read(case / "WallSurface.vtp")
    ids = np.asarray(w.cell_data["SurfID"])
    keep = np.flatnonzero(ids != 5)
    w.extract_cells(keep).extract_surface(algorithm="dataset_surface").save(case / "WallSurface.vtp")
    with pytest.raises(ValueError, match="SurfID == 5"):
        load_reference(case)

    case = write_synthetic_zenodo_case(tmp_path / "P96")
    w = pv.read(case / "WallSurface.vtp")
    extra = _tag(pv.Disc(center=(0, 0, 3.0), inner=6.0, outer=7.5, c_res=60), 2)
    w.merge(extra).extract_surface(algorithm="dataset_surface").save(case / "WallSurface.vtp")
    with pytest.raises(ValueError, match="exactly 2"):
        load_reference(case)


def test_zenodo_end_planes_tolerate_a_vertex_a_hair_off_its_plane(tmp_path):
    """D85: P05 / P16 each carry one annulus vertex 0.009 / 0.002 mm off its
    plane; rounding to 1e-3 used to call that a third z level and refuse."""
    from meshforge.io.zenodo_aaa import load_reference

    case = write_synthetic_zenodo_case(tmp_path / "P05")
    w = pv.read(case / "WallSurface.vtp")
    pts = np.asarray(w.points).copy()
    top = np.flatnonzero(np.isclose(pts[:, 2], 10.0))
    pts[top[0], 2] += 0.009
    w.points = pts
    w.save(case / "WallSurface.vtp")
    ref = load_reference(case)
    assert (ref.z0, ref.z1) == (-10.0, 10.0)  # each plane's most populated level
    pts[top[0], 2] += 0.5  # half a millimetre: an annulus that is not flat
    w.points = pts
    w.save(case / "WallSurface.vtp")
    # (D86's loader) levels further apart than 0.01 mm are separate planes,
    # so the off-plane vertex reads as a third level and is refused
    with pytest.raises(ValueError, match="3 z level"):
        load_reference(case)

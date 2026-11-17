"""Complete solver input decks: mesh + material + BCs + loads + step.

Extends the Hardening-B Abaqus export (``abaqus.py``) so DigiTwin writes
ENTIRE runnable decks. Element/surface blocks are shared via
``abaqus.mesh_lines`` (trap #12: *SURFACE is hand-written, meshio has none).
Formulation defaults respect trap #11: Abaqus decks default to hybrid
(C3D8H/C3D4H — full integration locks at near-incompressible nu), CalculiX
has no hybrid library so ccx decks default to full and refuse hybrid.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pyvista as pv

from digitwin.export.abaqus import _chunk_ids, _face_label, mesh_lines
from digitwin.export.neutral import guard_output_path
from digitwin.meshing.base import WallMesh

_TARGETS = ("abaqus", "ccx", "nastran", "lsdyna", "ansys")
_DEFAULT_FORMULATION = {
    "abaqus": "hybrid",
    "ccx": "full",
    "nastran": "full",
    # LS-DYNA's own solid default is ELFORM 1 (constant stress) = reduced.
    "lsdyna": "reduced",
    # ANSYS SOLID185's own default is KEYOPT(2)=0 (B-bar) = full.
    "ansys": "full",
}


@dataclass(frozen=True)
class Material:
    """Solver material; build via the classmethod constructors."""

    name: str
    kind: str  # "elastic" | "neo_hooke" | "mooney_rivlin" | "thermal"
    params: tuple[float, ...]
    density: float | None = None
    specific_heat: float | None = None  # thermal only (V31 transient)
    # thermal only (V41): temperature-dependent conductivity as ascending
    # (k, T) rows; when set, params[0] is unused and lines() emits the table.
    conductivity_table: tuple[tuple[float, float], ...] | None = None
    # elastic only (V45): constant thermal-expansion coefficient (*EXPANSION).
    expansion: float | None = None

    @classmethod
    def elastic(
        cls,
        E: float,
        nu: float,
        *,
        name: str = "MAT",
        density: float | None = None,
        expansion: float | None = None,
    ) -> "Material":
        if not E > 0:
            raise ValueError(f"Young's modulus must be positive, got E={E}")
        if not -1.0 < nu < 0.5:
            raise ValueError(f"Poisson's ratio must satisfy -1 < nu < 0.5, got nu={nu}")
        if expansion is not None:
            if not isinstance(expansion, (int, float)):
                raise ValueError(
                    "temperature-dependent expansion tables are not supported: "
                    "ccx evaluates an *EXPANSION table as a secant anchored at "
                    "numeric temperature ZERO — not at the *INITIAL CONDITIONS "
                    "reference — so the identical physics yields different "
                    "strains in Celsius vs Kelvin with no warning (V45 probe); "
                    "pass a constant alpha"
                )
            if not (np.isfinite(expansion) and expansion > 0):
                raise ValueError(
                    f"thermal-expansion coefficient must be a positive finite "
                    f"number, got {expansion}"
                )
            expansion = float(expansion)
        return cls(
            name=name, kind="elastic", params=(float(E), float(nu)),
            density=density, expansion=expansion,
        )

    @classmethod
    def neo_hookean(
        cls, c10: float, d1: float, *, name: str = "MAT", density: float | None = None
    ) -> "Material":
        if not c10 > 0:
            raise ValueError(f"neo-Hookean C10 must be positive, got {c10}")
        if d1 < 0:
            raise ValueError(f"neo-Hookean D1 must be >= 0, got {d1}")
        return cls(name=name, kind="neo_hooke", params=(float(c10), float(d1)), density=density)

    @classmethod
    def mooney_rivlin(
        cls,
        c10: float,
        c01: float,
        d1: float,
        *,
        name: str = "MAT",
        density: float | None = None,
    ) -> "Material":
        if not c10 + c01 > 0:
            raise ValueError(f"Mooney-Rivlin needs C10+C01 > 0, got {c10}+{c01}")
        if d1 < 0:
            raise ValueError(f"Mooney-Rivlin D1 must be >= 0, got {d1}")
        return cls(
            name=name,
            kind="mooney_rivlin",
            params=(float(c10), float(c01), float(d1)),
            density=density,
        )

    @classmethod
    def thermal(
        cls,
        k: float | Sequence[tuple[float, float]],
        *,
        name: str = "MAT",
        density: float | None = None,
        specific_heat: float | None = None,
    ) -> "Material":
        """Heat-transfer material: isotropic conductivity (V24); transient
        steps additionally need density + specific_heat (V31).

        ``k`` is a positive scalar, or a temperature-dependent table of
        ascending ``(k, T)`` rows (V41 — *CONDUCTIVITY with a temperature
        column; every k>0, temperatures strictly increasing, >= 2 rows)."""
        if specific_heat is not None and not specific_heat > 0:
            raise ValueError(
                f"specific heat must be positive, got {specific_heat}"
            )
        sh = None if specific_heat is None else float(specific_heat)
        if isinstance(k, (int, float)):
            if not k > 0:
                raise ValueError(f"conductivity must be positive, got k={k}")
            return cls(
                name=name, kind="thermal", params=(float(k),),
                density=density, specific_heat=sh,
            )
        table = tuple((float(kk), float(tt)) for kk, tt in k)
        if len(table) < 2:
            raise ValueError(
                "a conductivity table needs >= 2 (k, T) rows — pass a scalar "
                f"for constant conductivity, got {len(table)} row(s)"
            )
        for kk, _t in table:
            if not kk > 0:
                raise ValueError(
                    f"every conductivity-table k must be positive, got k={kk}"
                )
        temps = [t for _k, t in table]
        if any(b <= a for a, b in zip(temps, temps[1:])):
            raise ValueError(
                f"conductivity-table temperatures must strictly increase, "
                f"got {temps}"
            )
        return cls(
            name=name, kind="thermal", params=(table[0][0],),
            density=density, specific_heat=sh, conductivity_table=table,
        )

    @property
    def is_hyperelastic(self) -> bool:
        return self.kind in ("neo_hooke", "mooney_rivlin")

    def lines(self) -> list[str]:
        out = [f"*MATERIAL, NAME={self.name}"]
        vals = ", ".join(f"{v:.10g}" for v in self.params)
        if self.kind == "elastic":
            out += ["*ELASTIC", vals]
            if self.expansion is not None:
                out += ["*EXPANSION", f"{self.expansion:.10g}"]
        elif self.kind == "neo_hooke":
            out += ["*HYPERELASTIC, NEO HOOKE", vals]
        elif self.kind == "mooney_rivlin":
            out += ["*HYPERELASTIC, MOONEY-RIVLIN", vals]
        elif self.kind == "thermal":
            if self.conductivity_table is not None:
                out.append("*CONDUCTIVITY")
                out += [f"{k:.10g}, {t:.10g}" for k, t in self.conductivity_table]
            else:
                out += ["*CONDUCTIVITY", vals]
        else:  # pragma: no cover - constructors are the only entry points
            raise ValueError(f"unknown material kind {self.kind!r}")
        if self.density is not None:
            out += ["*DENSITY", f"{self.density:.10g}"]
        if self.specific_heat is not None:
            out += ["*SPECIFIC HEAT", f"{self.specific_heat:.10g}"]
        return out


@dataclass(frozen=True)
class BoundaryCondition:
    """*BOUNDARY entry: boundary-group name (-> NSET) or 0-based node id.

    DOFs 1..3 are displacements; DOF 11 is temperature (heat steps, V24).
    """

    target: str | int
    first_dof: int
    last_dof: int | None = None
    value: float = 0.0

    def __post_init__(self) -> None:
        last = self.first_dof if self.last_dof is None else self.last_dof
        if self.first_dof == 11 or last == 11:
            if not (self.first_dof == last == 11):
                raise ValueError(
                    f"temperature is the single DOF 11 — it cannot span a "
                    f"range ({self.first_dof}..{last} for {self.target!r})"
                )
            return
        if not (1 <= self.first_dof <= 3 and 1 <= last <= 3):
            raise ValueError(
                f"solid DOFs are 1..3 (or 11 for temperature), got "
                f"{self.first_dof}..{last} for target {self.target!r}"
            )
        if last < self.first_dof:
            raise ValueError(
                f"last_dof {last} < first_dof {self.first_dof} for {self.target!r}"
            )

    @property
    def is_temperature(self) -> bool:
        return self.first_dof == 11

    def line(self) -> str:
        last = self.first_dof if self.last_dof is None else self.last_dof
        ref = (
            str(int(self.target) + 1)  # 0-based node id -> 1-based deck id
            if isinstance(self.target, int)
            else str(self.target).upper()
        )
        if self.value == 0.0:
            return f"{ref}, {self.first_dof}, {last}"
        return f"{ref}, {self.first_dof}, {last}, {self.value:.10g}"


@dataclass(frozen=True)
class Load:
    """Distributed pressure (*DSLOAD), *CLOAD, surface heat flux (*DFLUX),
    or film convection (*FILM, V31 — ``magnitude`` holds the sink T)."""

    kind: str  # "pressure" | "cload" | "dflux" | "film" | "radiate"
    target: str | int
    magnitude: float
    dof: int | None = None
    h: float | None = None  # film coefficient (kind="film" only)
    emissivity: float | None = None  # kind="radiate" only (V41)

    def __post_init__(self) -> None:
        # Direct construction must be as safe as the factory classmethods —
        # a bad dof/kind otherwise reaches the writers, where abaqus/lsdyna
        # emit it into the card silently (V44 review follow-up).
        if self.kind not in ("pressure", "cload", "dflux", "film", "radiate"):
            raise ValueError(f"unknown load kind {self.kind!r}")
        if self.kind == "cload":
            if self.dof is None or not 1 <= self.dof <= 3:
                raise ValueError(
                    f"solid DOFs are 1..3, got {self.dof} for {self.target!r}"
                )
        else:
            if not isinstance(self.target, str):
                raise ValueError(
                    f"{self.kind} loads target a named boundary surface, "
                    f"got node id {self.target!r}"
                )
            if self.dof is not None:
                raise ValueError(
                    f"dof applies only to concentrated loads, not kind={self.kind!r}"
                )
        if self.kind == "film":
            if self.h is None or not self.h > 0:
                raise ValueError(
                    f"film coefficient must be positive, got h={self.h}"
                )
        elif self.h is not None:
            raise ValueError(
                f"film coefficient h applies only to film loads, "
                f"not kind={self.kind!r}"
            )
        if self.kind == "radiate":
            if self.emissivity is None or not 0.0 < self.emissivity <= 1.0:
                raise ValueError(
                    f"emissivity must satisfy 0 < ε <= 1, got {self.emissivity}"
                )
        elif self.emissivity is not None:
            raise ValueError(
                f"emissivity applies only to radiate loads, not kind={self.kind!r}"
            )

    @classmethod
    def pressure(cls, surface_name: str, magnitude: float) -> "Load":
        """Positive magnitude acts against the surface's outward face normal
        (on an inner wall surface that means inflation)."""
        return cls(kind="pressure", target=surface_name, magnitude=float(magnitude))

    @classmethod
    def concentrated(cls, target: str | int, dof: int, magnitude: float) -> "Load":
        if not 1 <= dof <= 3:
            raise ValueError(f"solid DOFs are 1..3, got {dof} for {target!r}")
        return cls(kind="cload", target=target, magnitude=float(magnitude), dof=dof)

    @classmethod
    def surface_flux(cls, surface_name: str, magnitude: float) -> "Load":
        """*DFLUX heat flux per area on a boundary surface; positive flows
        INTO the body (heat steps only, V24)."""
        return cls(kind="dflux", target=surface_name, magnitude=float(magnitude))

    @classmethod
    def film(cls, surface_name: str, sink: float, h: float) -> "Load":
        """*FILM convection on a boundary surface (heat steps, V31): flux
        h·(sink − T_surface) INTO the body. ccx refuses the surface-name
        form *DFLUX accepts (probe-verified fatal) — the writer expands
        this element-wise, one ``eid, Fk, sink, h`` line per face."""
        if not h > 0:
            raise ValueError(f"film coefficient must be positive, got h={h}")
        return cls(
            kind="film", target=surface_name, magnitude=float(sink), h=float(h)
        )

    @classmethod
    def radiate(cls, surface_name: str, sink: float, emissivity: float) -> "Load":
        """Environmental radiation on a boundary surface (heat steps, V41):
        flux ε·σ·((sink+off)⁴ − (T_surface+off)⁴) INTO the body, with σ and
        the absolute-zero offset from *PHYSICAL CONSTANTS. ``sink`` is the
        environment temperature in the model's unit. Like *FILM, ccx refuses
        the surface-name form — the writer expands this element-wise, one
        ``eid, Rk, sink, ε`` line per face."""
        if not 0.0 < emissivity <= 1.0:
            raise ValueError(
                f"emissivity must satisfy 0 < ε <= 1, got {emissivity}"
            )
        return cls(
            kind="radiate", target=surface_name, magnitude=float(sink),
            emissivity=float(emissivity),
        )


@dataclass(frozen=True)
class Step:
    """Single solver step; output requests for .frd (FILE) and .dat (PRINT).

    ``kind`` (V24): "static" | "frequency" (*FREQUENCY, ``n_modes``) |
    "heat" (*HEAT TRANSFER, STEADY STATE). For heat steps the untouched
    output defaults swap to nodal temperatures (NT) and no element file.

    Transient heat (V31): ``transient=True`` turns the heat step into
    ``*HEAT TRANSFER, DIRECT`` with ``dt, period`` increments and a
    required ``initial_temperature`` (*INITIAL CONDITIONS — never
    guessed). ``direct=False`` opts into ccx adaptive stepping (probe:
    backward Euler either way; DIRECT gives deterministic output times).

    Buckling + preloaded modes (V32): ``kind="buckle"`` emits *BUCKLE
    with ``n_modes`` factors (THE probed trap: too few factors makes
    ccx SILENTLY return higher modes only — degenerate doublets vanish;
    keep n_modes generous). ``kind="frequency", preloaded=True`` emits
    a *STATIC step carrying the BCs + loads, then *STEP, PERTURBATION
    + *FREQUENCY with the stress stiffening of the preload included.

    Riks post-buckling (V42): ``riks=True`` turns a static NLGEOM step into
    ``*STATIC, RIKS`` — arc-length continuation that traces the load–
    displacement path THROUGH limit/bifurcation points where load-controlled
    Newton fails. The data line is ``dl_init, max_lpf, dl_min, dl_max``
    (arc-length increment bounds; field 2 caps the run at that load-
    proportionality factor). RF is auto-requested — THE trap is that the frd
    "time" is the arc-length parameter, not the load, so the true load must be
    read from the reaction force. ``riks`` requires ``kind="static"`` AND
    ``nlgeom=True`` (post-buckling is geometrically nonlinear).
    """

    nlgeom: bool = False
    kind: str = "static"
    n_modes: int = 10
    preloaded: bool = False
    transient: bool = False
    dt: float | None = None
    period: float | None = None
    initial_temperature: float | None = None
    direct: bool = True
    riks: bool = False
    riks_dl_init: float | None = None
    riks_max_lpf: float | None = None
    riks_dl_min: float | None = None
    riks_dl_max: float | None = None
    node_file: tuple[str, ...] = ("U",)
    el_file: tuple[str, ...] = ("S", "E")
    node_print: dict[str, tuple[str, ...]] = field(default_factory=dict)
    el_print: dict[str, tuple[str, ...]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.kind not in ("static", "frequency", "heat", "buckle"):
            raise ValueError(
                f"step kind must be static|frequency|heat|buckle, got {self.kind!r}"
            )
        if self.kind in ("frequency", "buckle") and self.n_modes < 1:
            raise ValueError(f"n_modes must be >= 1, got {self.n_modes}")
        if self.kind != "static" and self.nlgeom:
            raise ValueError(f"nlgeom applies to static steps, not {self.kind!r}")
        if self.preloaded and self.kind != "frequency":
            raise ValueError(
                f"preloaded applies to frequency steps, not {self.kind!r}"
            )
        _riks_knobs = (
            ("riks_dl_init", self.riks_dl_init),
            ("riks_max_lpf", self.riks_max_lpf),
            ("riks_dl_min", self.riks_dl_min),
            ("riks_dl_max", self.riks_dl_max),
        )
        if self.riks:
            if self.kind != "static":
                raise ValueError(
                    f"riks (arc-length continuation) applies to static steps, "
                    f"not {self.kind!r}"
                )
            if not self.nlgeom:
                raise ValueError(
                    "riks requires nlgeom=True — post-buckling continuation is "
                    "geometrically nonlinear (a linear-static step has no limit "
                    "point to trace through)"
                )
            # Fill unset arc-length knobs with generic defaults; validate the
            # supplied ones. The two bounds must bracket the initial increment.
            _defaults = {
                "riks_dl_init": 0.05, "riks_max_lpf": 1.0,
                "riks_dl_min": 1e-5, "riks_dl_max": 0.5,
            }
            for name, value in _riks_knobs:
                if value is None:
                    object.__setattr__(self, name, _defaults[name])
                elif not value > 0:
                    raise ValueError(f"{name} must be positive, got {value}")
            if self.riks_dl_min > self.riks_dl_max:
                raise ValueError(
                    f"riks_dl_min {self.riks_dl_min} exceeds riks_dl_max "
                    f"{self.riks_dl_max}"
                )
            if "RF" not in self.node_file:
                # RF (reaction force) is the load half of the curve — the frd
                # "time" is only the arc-length parameter (trap #2).
                object.__setattr__(
                    self, "node_file", tuple(self.node_file) + ("RF",)
                )
        else:
            stray = [k for k, v in _riks_knobs if v is not None]
            if stray:
                raise ValueError(
                    f"{stray} are arc-length knobs that apply only to a riks "
                    f"step — set Step(riks=True) or drop them"
                )
        if self.kind != "heat":
            if self.transient:
                raise ValueError(
                    f"transient applies to heat steps, not {self.kind!r}"
                )
            stray = [
                k for k, v in (
                    ("dt", self.dt), ("period", self.period),
                    ("initial_temperature", self.initial_temperature),
                ) if v is not None
            ]
            if stray:
                raise ValueError(
                    f"{stray} apply to transient heat steps, not {self.kind!r}"
                )
            if not self.direct:
                raise ValueError(
                    f"direct applies to transient heat steps, not {self.kind!r}"
                )
        elif self.transient:
            if self.dt is None or not self.dt > 0:
                raise ValueError(
                    f"a transient heat step needs a positive dt, got {self.dt}"
                )
            if self.period is None or not self.period > 0:
                raise ValueError(
                    f"a transient heat step needs a positive period, "
                    f"got {self.period}"
                )
            if self.dt > self.period:
                raise ValueError(
                    f"dt {self.dt} exceeds the period {self.period}"
                )
            if self.initial_temperature is None:
                raise ValueError(
                    "a transient heat step needs initial_temperature — "
                    "DigiTwin never guesses a starting field"
                )
        else:
            stray = [
                k for k, v in (
                    ("dt", self.dt), ("period", self.period),
                    ("initial_temperature", self.initial_temperature),
                ) if v is not None
            ]
            if stray:
                raise ValueError(
                    f"{stray} cannot act on a steady heat step — set "
                    f"Step(transient=True) or drop them"
                )
            if not self.direct:
                raise ValueError(
                    "direct=False requires transient=True (adaptive "
                    "stepping is a transient concept)"
                )
        if self.kind in ("frequency", "buckle") and self.el_file == ("S", "E"):
            # Mode shapes (U) are the point of a frequency/buckle step;
            # modal stresses stay opt-in (explicit el_file).
            object.__setattr__(self, "el_file", ())
        if self.kind == "heat":
            # Displacement/stress requests are meaningless in a pure heat
            # step — swap the untouched defaults, refuse explicit ones.
            if self.node_file == ("U",):
                object.__setattr__(self, "node_file", ("NT",))
            if self.el_file == ("S", "E"):
                object.__setattr__(self, "el_file", ())
            bad = [v for v in self.node_file + self.el_file if v in ("U", "S", "E")]
            if bad:
                raise ValueError(
                    f"heat steps cannot request mechanical output {bad}; "
                    f"use NT (nodal temperatures) / HFL (heat flux)"
                )


@dataclass(frozen=True)
class PhysicalConstants:
    """*PHYSICAL CONSTANTS for radiation (V41): the Stefan-Boltzmann constant
    and the absolute-zero offset in the model's temperature unit (0 for
    Kelvin, -273.15 for Celsius). ccx converts T -> T - absolute_zero before
    the σT⁴ law, so a wrong offset is SILENTLY wrong (probe: 555 vs 399.9);
    a missing/incomplete card makes ccx fail loud."""

    stefan_boltzmann: float
    absolute_zero: float

    def __post_init__(self) -> None:
        if not self.stefan_boltzmann > 0:
            raise ValueError(
                f"Stefan-Boltzmann constant must be positive, got "
                f"{self.stefan_boltzmann}"
            )


@dataclass(frozen=True)
class DeckSpec:
    """Everything beyond the mesh that a runnable deck needs."""

    material: Material
    step: Step
    boundary_conditions: tuple[BoundaryCondition, ...] = ()
    loads: tuple[Load, ...] = ()
    target: str = "abaqus"
    formulation: str | None = None
    length_unit: str = "mm"
    force_unit: str = "N"
    heading: str | None = None
    physical_constants: PhysicalConstants | None = None  # radiation (V41)
    # V45 thermal stress (static steps only): a *TEMPERATURE load — uniform
    # over NALL, or per-node (0-based mesh point id, T) pairs (e.g. a prior
    # heat solve's NDTEMP field). Either REQUIRES reference_temperature: the
    # stress-free datum written as *INITIAL CONDITIONS, TYPE=TEMPERATURE —
    # ccx's ONLY expansion reference (an *EXPANSION ZERO= parameter is
    # accepted and silently ignored, V45 probe), so it is never defaulted.
    uniform_temperature: float | None = None
    nodal_temperatures: tuple[tuple[int, float], ...] | None = None
    reference_temperature: float | None = None

    def resolved_formulation(self) -> str:
        if self.target not in _TARGETS:
            raise ValueError(f"target must be one of {_TARGETS}, got {self.target!r}")
        form = self.formulation or _DEFAULT_FORMULATION[self.target]
        if self.target == "ccx" and form == "hybrid":
            raise ValueError(
                "CalculiX has no hybrid element library (trap #11); "
                "use formulation='full' or 'reduced' for target='ccx'"
            )
        if self.target == "nastran" and form != "full":
            raise ValueError(
                f"no {form!r} formulation is wired for NASTRAN solids "
                f"(CHEXA/CTETRA are written as-is); leave formulation unset"
            )
        if self.target == "lsdyna" and form == "hybrid":
            raise ValueError(
                "the LS-DYNA writer maps reduced/full to ELFORM 1/2 "
                "(10/16 for tets); there is no hybrid solid — use "
                "formulation='reduced' or 'full'"
            )
        if self.target == "ansys" and form == "hybrid":
            raise ValueError(
                "the ANSYS writer maps full/reduced to SOLID185 "
                "KEYOPT(2)=0/1; mixed u-P (KEYOPT(6)) is not wired — use "
                "formulation='full' or 'reduced'"
            )
        return form

    def validate(self) -> None:
        self.resolved_formulation()
        step, mat = self.step, self.material
        has_radiate = any(ld.kind == "radiate" for ld in self.loads)
        if has_radiate and self.physical_constants is None:
            raise ValueError(
                "a *RADIATE load needs *PHYSICAL CONSTANTS (Stefan-Boltzmann + "
                "absolute-zero offset) — ccx fails without it and a wrong "
                "offset is silently wrong; pass DeckSpec(physical_constants=…)"
            )
        if self.physical_constants is not None and not has_radiate:
            raise ValueError(
                "physical_constants only feed *RADIATE — drop them or add a "
                "Load.radiate (they are inert without a radiation load)"
            )
        # V45 thermal stress: *TEMPERATURE loads and their traps.
        if (
            self.uniform_temperature is not None
            and self.nodal_temperatures is not None
        ):
            raise ValueError(
                "give either uniform_temperature or nodal_temperatures, "
                "not both"
            )
        has_temp = (
            self.uniform_temperature is not None
            or self.nodal_temperatures is not None
        )
        if has_temp:
            if step.kind != "static":
                raise ValueError(
                    "a *TEMPERATURE thermal load acts in a static step — a "
                    f"heat step solves FOR temperature, and {step.kind!r} "
                    "steps take no thermal load; chain a heat solve's "
                    "temperatures into a static solve instead"
                )
            if self.target not in ("abaqus", "ccx"):
                raise ValueError(
                    f"thermal loads are wired for abaqus/ccx decks only; "
                    f"the {self.target} writer has no *TEMPERATURE mapping"
                )
            if mat.kind != "elastic" or mat.expansion is None:
                raise ValueError(
                    "a *TEMPERATURE load without *EXPANSION runs clean and "
                    "yields silently ZERO thermal stress (V45 probe) — give "
                    "the material Material.elastic(..., expansion=alpha)"
                )
            if self.reference_temperature is None:
                raise ValueError(
                    "a thermal load needs reference_temperature (the "
                    "stress-free datum, written as *INITIAL CONDITIONS, "
                    "TYPE=TEMPERATURE): ccx's expansion reference is the "
                    "initial temperature ONLY — an *EXPANSION ZERO= "
                    "parameter is silently ignored and a wrong datum is "
                    "silently wrong stress (V45 probe) — so it is never "
                    "defaulted"
                )
            if not np.isfinite(self.reference_temperature):
                raise ValueError(
                    f"reference_temperature must be finite, got "
                    f"{self.reference_temperature}"
                )
            if self.uniform_temperature is not None and not np.isfinite(
                self.uniform_temperature
            ):
                raise ValueError(
                    f"uniform_temperature must be finite, got "
                    f"{self.uniform_temperature}"
                )
            if self.nodal_temperatures is not None:
                if len(self.nodal_temperatures) == 0:
                    raise ValueError(
                        "nodal_temperatures is empty — pass None for no "
                        "thermal load"
                    )
                temps = np.asarray(
                    [t for _nid, t in self.nodal_temperatures], dtype=float
                )
                if not np.all(np.isfinite(temps)):
                    raise ValueError(
                        "non-finite nodal temperature (NaN/inf) — refusing "
                        "to build a thermal load from corrupt data"
                    )
        else:
            if mat.expansion is not None:
                raise ValueError(
                    "Material expansion (*EXPANSION) is inert without a "
                    "thermal load — add uniform_temperature/"
                    "nodal_temperatures or drop it"
                )
            if self.reference_temperature is not None:
                raise ValueError(
                    "reference_temperature only feeds a thermal load — add "
                    "uniform_temperature/nodal_temperatures or drop it"
                )
        if step.kind != "static":
            if self.target == "nastran":
                raise ValueError(
                    "the NASTRAN writer emits SOL 101 (linear statics) only; "
                    f"{step.kind!r} steps target abaqus/ccx"
                )
            if self.target == "lsdyna":
                raise ValueError(
                    "the LS-DYNA writer emits linear-static decks only "
                    f"(D29-honest, format-level); {step.kind!r} steps "
                    f"target abaqus/ccx"
                )
            if self.target == "ansys":
                raise ValueError(
                    "the ANSYS writer emits linear-static decks only "
                    f"(D29-honest, format-level); {step.kind!r} steps "
                    f"target abaqus/ccx"
                )
            if mat.is_hyperelastic:
                raise ValueError(
                    f"hyperelastic materials need a static NLGEOM step, "
                    f"not {step.kind!r}"
                )
        if step.kind == "frequency":
            if mat.density is None:
                raise ValueError(
                    "a frequency step needs inertia: give the material a "
                    "density (Material.elastic(..., density=...))"
                )
            if mat.kind == "thermal":
                raise ValueError("a frequency step needs an elastic material")
            if step.preloaded and not self.loads:
                raise ValueError(
                    "preloaded modes factor a preload — give the step at "
                    "least one load, or drop Step(preloaded=True)"
                )
            if not step.preloaded and self.loads:
                raise ValueError(
                    "loads in a plain frequency step — preloaded modes need "
                    "Step(preloaded=True) (V32)"
                )
            if any(bc.is_temperature for bc in self.boundary_conditions):
                raise ValueError("temperature BCs (DOF 11) belong to heat steps")
        elif step.kind == "buckle":
            if mat.kind == "thermal":
                raise ValueError("a buckle step needs an elastic material")
            if not self.loads:
                raise ValueError(
                    "a buckle step factors its loads (factor × load = "
                    "critical load) — give at least one load"
                )
            if any(ld.kind in ("dflux", "film", "radiate") for ld in self.loads):
                raise ValueError(
                    "*DFLUX/*FILM/*RADIATE heat loads belong to heat steps"
                )
            if any(bc.is_temperature for bc in self.boundary_conditions):
                raise ValueError("temperature BCs (DOF 11) belong to heat steps")
        elif step.kind == "heat":
            if mat.kind != "thermal":
                raise ValueError(
                    "a heat step needs Material.thermal(k); "
                    f"got kind {mat.kind!r}"
                )
            mech = [bc for bc in self.boundary_conditions if not bc.is_temperature]
            if mech:
                raise ValueError(
                    f"{len(mech)} mechanical BC(s) in a heat step — only "
                    f"temperature BCs (DOF 11) apply"
                )
            wrong = [
                ld for ld in self.loads
                if ld.kind not in ("dflux", "film", "radiate")
            ]
            if wrong:
                raise ValueError(
                    "pressure/cload loads are mechanical; heat steps take "
                    "Load.surface_flux (*DFLUX), Load.film (*FILM) or "
                    "Load.radiate (*RADIATE) only"
                )
            if step.transient:
                if mat.density is None or mat.specific_heat is None:
                    raise ValueError(
                        "a transient heat step needs thermal mass: "
                        "Material.thermal(k, density=..., specific_heat=...)"
                    )
            elif mat.specific_heat is not None:
                raise ValueError(
                    "specific_heat cannot act in a steady heat step — drop "
                    "it or set Step(transient=True)"
                )
        else:
            if mat.kind == "thermal":
                raise ValueError("a static step needs a mechanical material")
            if any(bc.is_temperature for bc in self.boundary_conditions):
                raise ValueError("temperature BCs (DOF 11) belong to heat steps")
            if any(ld.kind in ("dflux", "film", "radiate") for ld in self.loads):
                raise ValueError(
                    "*DFLUX/*FILM/*RADIATE heat loads belong to heat steps"
                )
        if self.target == "nastran":
            if self.material.is_hyperelastic:
                raise ValueError(
                    "the NASTRAN writer emits linear-elastic MAT1 only; "
                    "hyperelastic decks target abaqus/ccx"
                )
            if self.step.nlgeom:
                raise ValueError(
                    "the NASTRAN writer emits SOL 101 (linear statics); "
                    "nlgeom steps target abaqus/ccx"
                )
            if self.step.node_print or self.step.el_print:
                raise ValueError(
                    ".dat PRINT requests are Abaqus/CalculiX concepts; "
                    "NASTRAN output goes through DISPLACEMENT/STRESS = ALL"
                )
        if self.target == "lsdyna":
            if self.material.kind != "elastic":
                raise ValueError(
                    "the LS-DYNA writer emits *MAT_ELASTIC only; "
                    f"{self.material.kind!r} decks target abaqus/ccx"
                )
            if self.step.nlgeom:
                raise ValueError(
                    "the LS-DYNA writer emits linear-static decks; "
                    "nlgeom steps target abaqus/ccx"
                )
            if self.step.node_print or self.step.el_print:
                raise ValueError(
                    ".dat PRINT requests are Abaqus/CalculiX concepts; "
                    "they have no LS-DYNA keyword mapping"
                )
            nonzero = [bc for bc in self.boundary_conditions if bc.value != 0.0]
            if nonzero:
                raise ValueError(
                    f"{len(nonzero)} nonzero BC value(s): LS-DYNA enforced "
                    f"motion needs *BOUNDARY_PRESCRIBED_MOTION with load "
                    f"curves — not wired; only zero SPC flags are written"
                )
        if self.target == "ansys":
            # NOTE no nonzero-BC refusal here: ANSYS D carries an
            # enforced-displacement value natively (unlike LS-DYNA).
            if self.material.kind != "elastic":
                raise ValueError(
                    "the ANSYS writer emits linear-elastic MP,EX/PRXY only; "
                    f"{self.material.kind!r} decks target abaqus/ccx"
                )
            if self.step.nlgeom:
                raise ValueError(
                    "the ANSYS writer emits linear-static decks; "
                    "nlgeom steps target abaqus/ccx"
                )
            if self.step.node_print or self.step.el_print:
                raise ValueError(
                    ".dat PRINT requests are Abaqus/CalculiX concepts; "
                    "ANSYS output goes through OUTRES/PRNSOL"
                )
        if self.material.is_hyperelastic and not self.step.nlgeom:
            raise ValueError(
                "hyperelastic material requires a NLGEOM step "
                "(Step(nlgeom=True)); small-strain hyperelasticity is not a thing"
            )
        if (
            self.material.is_hyperelastic
            and self.target == "ccx"
            and self.material.params[-1] <= 0
        ):
            raise ValueError(
                "CalculiX needs a strictly positive compressibility D1 "
                "(fully incompressible D1=0 requires hybrid elements, "
                "which ccx lacks — trap #11)"
            )


def pressure_to_nodal_forces(
    wall: WallMesh, group_name: str, magnitude: float
) -> tuple[Load, ...]:
    """Lumped nodal forces equivalent to a pressure on a boundary surface.

    Written for MYSTRAN (V25), which has not implemented the solid-face
    PLOAD4 form the NASTRAN writer emits — the same physics goes in as
    FORCE cards instead. Per quad face the exact bilinear vector area
    0.5·(d13 × d24) is oriented outward (away from the parent cell's
    centroid) and the face force −p·A⃗ is lumped a quarter per node, so
    the load matches *DSLOAD's sign convention: positive pressure acts
    against the outward face normal (inflation on an inner wall).
    """
    if group_name not in wall.boundary:
        raise ValueError(
            f"boundary group {group_name!r} not found; "
            f"available: {sorted(wall.boundary)}"
        )
    group = wall.boundary[group_name]
    face_nodes = np.asarray(group.face_nodes)
    if face_nodes.ndim != 2 or face_nodes.shape[1] != 4:
        raise ValueError(
            f"pressure_to_nodal_forces handles quad faces; group "
            f"{group_name!r} has faces of shape {face_nodes.shape}"
        )
    pts = np.asarray(wall.grid.points, dtype=float)
    forces: dict[int, np.ndarray] = {}
    for row, cid in zip(face_nodes, np.asarray(group.cell_ids)):
        quad = pts[row]
        avec = 0.5 * np.cross(quad[2] - quad[0], quad[3] - quad[1])
        cell_center = wall.grid.get_cell(int(cid)).points.mean(axis=0)
        if np.dot(avec, quad.mean(axis=0) - cell_center) < 0:
            avec = -avec  # group winding is unspecified — orient outward
        face_force = -float(magnitude) * avec / 4.0
        for nid in row:
            forces[int(nid)] = forces.get(int(nid), 0.0) + face_force
    return tuple(
        Load.concentrated(nid, dof, float(vec[dof - 1]))
        for nid, vec in sorted(forces.items())
        for dof in (1, 2, 3)
        if abs(vec[dof - 1]) > 1e-14
    )


def lateral_pins(wall: WallMesh, nset: str = "inlet") -> list[BoundaryCondition]:
    """Three tangential single-node pins that remove the in-plane rigid modes
    (Tx, Ty, Rz) of an axially-held tube without constraining radial motion:
    node nearest theta=0 -> DOF 2, nearest theta=pi -> DOF 2, nearest
    theta=pi/2 -> DOF 1. Work-free against a purely radial (Lame) field."""
    if nset not in wall.boundary:
        raise ValueError(
            f"boundary group {nset!r} not found; available: {sorted(wall.boundary)}"
        )
    nodes = np.unique(wall.boundary[nset].face_nodes)
    pts = np.asarray(wall.grid.points)[nodes]
    center = pts.mean(axis=0)
    theta = np.arctan2(pts[:, 1] - center[1], pts[:, 0] - center[0])

    def nearest(angle: float) -> int:
        wrapped = np.angle(np.exp(1j * (theta - angle)))
        return int(nodes[np.argmin(np.abs(wrapped))])

    n_0, n_pi, n_half = nearest(0.0), nearest(np.pi), nearest(np.pi / 2)
    if len({n_0, n_pi, n_half}) < 3:
        raise ValueError(
            f"lateral pins collapsed onto {len({n_0, n_pi, n_half})} distinct "
            f"node(s) — {nset!r} is too coarse for tangential pinning"
        )
    return [
        BoundaryCondition(n_0, 2),
        BoundaryCondition(n_pi, 2),
        BoundaryCondition(n_half, 1),
    ]


def _as_wall(mesh: WallMesh | pv.UnstructuredGrid, notes: list[str]) -> WallMesh:
    if isinstance(mesh, WallMesh):
        return mesh
    from digitwin.quality.volume import volume_quality

    grid = mesh.copy()
    if "region" not in grid.cell_data:
        grid.cell_data["region"] = np.ones(grid.n_cells, dtype=np.int32)
        notes.append("bare grid: region defaulted to 1 for all elements")
    notes.append("bare grid: no boundary groups — name-based BCs/loads will fail")
    return WallMesh(
        grid=grid, boundary={}, backend="external", quality=volume_quality(grid)
    )


def write_deck(
    mesh: WallMesh | pv.UnstructuredGrid,
    spec: DeckSpec,
    out_path: str | Path,
    *,
    input_path: str | Path | None = None,
) -> Path:
    """Write a complete runnable .inp for ``spec.target``."""
    spec.validate()
    notes: list[str] = []
    wall = _as_wall(mesh, notes)
    form = spec.resolved_formulation()

    nset_names = {name.upper() for name in wall.boundary}
    # NALL (GENERATE) serves frequency lateral clamps (V24), transient
    # *INITIAL CONDITIONS (V31) and thermal-stress loads (V45: the reference
    # *INITIAL CONDITIONS + a uniform *TEMPERATURE both act on NALL); the
    # shared mesh block stays untouched.
    needs_nall = (
        spec.step.kind == "frequency"
        or (spec.step.kind == "heat" and spec.step.transient)
        or spec.reference_temperature is not None
    )
    if needs_nall:
        nset_names.add("NALL")
    surf_names = {f"{n}_SURF" for n in nset_names}
    region = np.asarray(wall.grid.cell_data["region"])
    elset_names = {"WALL"} | {f"REGION{int(t)}" for t in np.unique(region)} | {
        f"{n}_ELS" for n in nset_names
    }

    for bc in spec.boundary_conditions:
        if isinstance(bc.target, str) and bc.target.upper() not in nset_names:
            raise ValueError(
                f"boundary condition targets unknown group {bc.target!r}; "
                f"available NSETs: {sorted(nset_names)}"
            )
        if isinstance(bc.target, (int, np.integer)) and not (
            0 <= int(bc.target) < wall.grid.n_points
        ):
            raise ValueError(
                f"boundary condition node id {bc.target} out of range "
                f"0..{wall.grid.n_points - 1}"
            )
    for load in spec.loads:
        if load.kind in ("pressure", "dflux", "film", "radiate"):
            sname = f"{str(load.target).upper()}_SURF"
            if sname not in surf_names:
                raise ValueError(
                    f"{load.kind} load targets unknown surface {load.target!r}; "
                    f"available: {sorted(n[:-5] for n in surf_names)}"
                )
        elif load.kind == "cload":
            if isinstance(load.target, str) and load.target.upper() not in nset_names:
                raise ValueError(
                    f"concentrated load targets unknown group {load.target!r}; "
                    f"available NSETs: {sorted(nset_names)}"
                )
            # int node ids were never range-checked (V44 review follow-up) —
            # a dangling id wrote a bad node reference into EVERY target's
            # deck. Mirrors the BoundaryCondition check above.
            if isinstance(load.target, (int, np.integer)) and not (
                0 <= int(load.target) < wall.grid.n_points
            ):
                raise ValueError(
                    f"concentrated load node id {load.target} out of range "
                    f"0..{wall.grid.n_points - 1}"
                )
        else:
            raise ValueError(f"unknown load kind {load.kind!r}")
    if spec.nodal_temperatures is not None:
        bad = [
            int(nid) for nid, _t in spec.nodal_temperatures
            if not 0 <= int(nid) < wall.grid.n_points
        ]
        if bad:
            raise ValueError(
                f"nodal_temperatures node id(s) {bad[:5]} out of range "
                f"0..{wall.grid.n_points - 1} — the temperature field does "
                f"not belong to this mesh"
            )
    for name in spec.step.node_print:
        if name.upper() not in nset_names:
            raise ValueError(
                f"node_print targets unknown NSET {name!r}; "
                f"available: {sorted(nset_names)}"
            )
    for name in spec.step.el_print:
        if name.upper() not in elset_names:
            raise ValueError(
                f"el_print targets unknown ELSET {name!r}; "
                f"available: {sorted(elset_names)}"
            )

    if spec.target == "nastran":
        from digitwin.export.nastran import write_nastran_deck

        return write_nastran_deck(
            wall, spec, out_path, input_path=input_path, notes=notes
        )
    if spec.target == "lsdyna":
        from digitwin.export.lsdyna import write_lsdyna_deck

        return write_lsdyna_deck(
            wall, spec, out_path, input_path=input_path, notes=notes
        )
    if spec.target == "ansys":
        from digitwin.export.ansys import write_ansys_deck

        return write_ansys_deck(
            wall, spec, out_path, input_path=input_path, notes=notes
        )

    body, eltype, kind, conn = mesh_lines(wall, form)

    out = Path(out_path).with_suffix(".inp")
    guard_output_path(out, input_paths=[input_path])
    out.parent.mkdir(parents=True, exist_ok=True)

    lines: list[str] = [
        "*HEADING",
        spec.heading or f"digitwin deck ({wall.backend}, {eltype}, target={spec.target})",
        f"** UNITS: length={spec.length_unit}, force={spec.force_unit} -> "
        f"stress = {spec.force_unit}/{spec.length_unit}^2",
        f"** target={spec.target}, formulation={form}",
    ]
    for note in notes:
        lines.append(f"** NOTE: {note}")
    lines.extend(body)

    # Element sets of each boundary group's parent cells (for *EL PRINT etc.).
    for name, group in wall.boundary.items():
        ids = np.unique(np.asarray(group.cell_ids)) + 1
        lines.append(f"*ELSET, ELSET={name.upper()}_ELS")
        lines.extend(_chunk_ids(ids))

    if needs_nall:
        lines.append("*NSET, NSET=NALL, GENERATE")
        lines.append(f"1, {wall.grid.n_points}, 1")

    lines.extend(spec.material.lines())
    lines.append(f"*SOLID SECTION, ELSET=WALL, MATERIAL={spec.material.name}")

    if spec.step.kind == "heat" and spec.step.transient:
        lines.append("*INITIAL CONDITIONS, TYPE=TEMPERATURE")
        lines.append(f"NALL, {spec.step.initial_temperature:.10g}")

    if spec.reference_temperature is not None:
        # V45: the stress-free datum — ccx's ONLY *EXPANSION reference.
        lines.append("*INITIAL CONDITIONS, TYPE=TEMPERATURE")
        lines.append(f"NALL, {spec.reference_temperature:.10g}")

    if spec.physical_constants is not None:
        pc = spec.physical_constants
        lines.append(
            f"*PHYSICAL CONSTANTS, ABSOLUTE ZERO={pc.absolute_zero:.10g}, "
            f"STEFAN BOLTZMANN={pc.stefan_boltzmann:.10g}"
        )

    bc_lines: list[str] = []
    if spec.boundary_conditions:
        bc_lines.append("*BOUNDARY")
        bc_lines.extend(bc.line() for bc in spec.boundary_conditions)
    load_lines: list[str] = []
    pressures = [ld for ld in spec.loads if ld.kind == "pressure"]
    if pressures:
        load_lines.append("*DSLOAD")
        for ld in pressures:
            load_lines.append(f"{str(ld.target).upper()}_SURF, P, {ld.magnitude:.10g}")
    fluxes = [ld for ld in spec.loads if ld.kind == "dflux"]
    if fluxes:
        load_lines.append("*DFLUX")
        for ld in fluxes:
            load_lines.append(f"{str(ld.target).upper()}_SURF, S, {ld.magnitude:.10g}")
    films = [ld for ld in spec.loads if ld.kind == "film"]
    if films:
        # *FILM refuses the surface-name form *DFLUX accepts (V31 probe:
        # fatal card error) — expand element-wise, exactly the (eid, face)
        # pairs the group's *SURFACE block carries.
        by_upper = {n.upper(): g for n, g in wall.boundary.items()}
        load_lines.append("*FILM")
        for ld in films:
            group = by_upper[str(ld.target).upper()]
            for cid, fnodes in zip(group.cell_ids, group.face_nodes):
                label = _face_label(conn[int(cid)], fnodes, kind)
                load_lines.append(
                    f"{int(cid) + 1}, F{label[1:]}, "
                    f"{ld.magnitude:.10g}, {ld.h:.10g}"
                )
    radiates = [ld for ld in spec.loads if ld.kind == "radiate"]
    if radiates:
        # *RADIATE takes the same element-wise (eid, face) form as *FILM
        # (ccx refuses the surface-name form); label Rk, sink T + emissivity.
        by_upper = {n.upper(): g for n, g in wall.boundary.items()}
        load_lines.append("*RADIATE")
        for ld in radiates:
            group = by_upper[str(ld.target).upper()]
            for cid, fnodes in zip(group.cell_ids, group.face_nodes):
                label = _face_label(conn[int(cid)], fnodes, kind)
                load_lines.append(
                    f"{int(cid) + 1}, R{label[1:]}, "
                    f"{ld.magnitude:.10g}, {ld.emissivity:.10g}"
                )
    cloads = [ld for ld in spec.loads if ld.kind == "cload"]
    if cloads:
        load_lines.append("*CLOAD")
        for ld in cloads:
            ref = (
                str(int(ld.target) + 1)
                if isinstance(ld.target, (int, np.integer))
                else str(ld.target).upper()
            )
            load_lines.append(f"{ref}, {ld.dof}, {ld.magnitude:.10g}")
    if spec.uniform_temperature is not None:
        # V45 thermal load: uniform soak over every node.
        load_lines.append("*TEMPERATURE")
        load_lines.append(f"NALL, {spec.uniform_temperature:.10g}")
    if spec.nodal_temperatures is not None:
        # V45 thermal load: per-node field (e.g. a prior heat solve's NDTEMP),
        # inline so the deck stays self-contained (probe: byte-identical to
        # the *TEMPERATURE, FILE= and same-job carryover architectures).
        load_lines.append("*TEMPERATURE")
        load_lines.extend(
            f"{int(nid) + 1}, {t:.10g}" for nid, t in spec.nodal_temperatures
        )
    output_lines: list[str] = []
    if spec.step.node_file:
        output_lines.append("*NODE FILE")
        output_lines.append(", ".join(spec.step.node_file))
    if spec.step.el_file:
        output_lines.append("*EL FILE")
        output_lines.append(", ".join(spec.step.el_file))
    for name, vars_ in spec.step.node_print.items():
        output_lines.append(f"*NODE PRINT, NSET={name.upper()}")
        output_lines.append(", ".join(vars_))
    for name, vars_ in spec.step.el_print.items():
        output_lines.append(f"*EL PRINT, ELSET={name.upper()}")
        output_lines.append(", ".join(vars_))

    if spec.step.kind == "frequency" and spec.step.preloaded:
        # V32 preloaded modes: the *STATIC step carries the BCs + preload
        # (ccx keeps *BOUNDARY active in later steps); the PERTURBATION
        # frequency step includes its stress stiffening and carries the
        # output requests.
        lines.append("*STEP")
        lines.append("*STATIC")
        lines.extend(bc_lines)
        lines.extend(load_lines)
        lines.append("*END STEP")
        lines.append("*STEP, PERTURBATION")
        lines.append("*FREQUENCY")
        lines.append(str(spec.step.n_modes))
        lines.extend(output_lines)
        lines.append("*END STEP")
    else:
        lines.append("*STEP, NLGEOM" if spec.step.nlgeom else "*STEP")
        if spec.step.kind == "frequency":
            lines.append("*FREQUENCY")
            lines.append(str(spec.step.n_modes))
        elif spec.step.kind == "buckle":
            lines.append("*BUCKLE")
            lines.append(str(spec.step.n_modes))
        elif spec.step.kind == "heat":
            if spec.step.transient:
                lines.append(
                    "*HEAT TRANSFER, DIRECT" if spec.step.direct
                    else "*HEAT TRANSFER"
                )
                lines.append(f"{spec.step.dt:.10g}, {spec.step.period:.10g}")
            else:
                lines.append("*HEAT TRANSFER, STEADY STATE")
        elif spec.step.riks:
            # *STATIC, RIKS: arc-length continuation. Data line
            # dl_init, max_lpf, dl_min, dl_max; field 2 caps the LPF (trap #2:
            # the frd "time" is this parameter, NOT the load — read RF).
            lines.append("*STATIC, RIKS")
            lines.append(
                f"{spec.step.riks_dl_init:.10g}, {spec.step.riks_max_lpf:.10g}, "
                f"{spec.step.riks_dl_min:.10g}, {spec.step.riks_dl_max:.10g}"
            )
        else:
            lines.append("*STATIC")
        lines.extend(bc_lines)
        lines.extend(load_lines)
        lines.extend(output_lines)
        lines.append("*END STEP")

    out.write_text("\n".join(lines) + "\n")
    return out

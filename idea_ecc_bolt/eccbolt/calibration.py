"""Calibration of the Crawford & Kulak load-deformation parameters.

The original C&K coefficients ``mu = 10/in`` and ``lambda = 0.55`` were fitted
to **single-bolt** shear tests.  In a bolt group the surrounding bolts, plate
bearing and the rotational restraint change the load-deformation response, so
the two coefficients are not necessarily transferable to a group without
adjustment.

This module fits ``mu`` and ``lambda`` to the observed failure loads of the
physical specimens in :mod:`eccbolt.benchmarks` by minimising the relative
root-mean-square error

    RMSE = sqrt( mean_i [ (Pu_i - Ptest_i) / Ptest_i ]^2 )

with the ICR solver used for every trial parameter set.  The fit is
deliberately restricted to the same experimental database used by the
verification suite (Badawi 1983, ten full-size specimens).  ``delta_max`` and
``Rult`` are kept at their measured/input values - only the two coefficients
that the comment asks for are revised.

The fitted values can be applied to a :class:`~eccbolt.model.ConnectionInput`
with :meth:`CalibrationResult.apply`; the GUI's *Calibrate* command does this
after asking the user.
"""
from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from .benchmarks import (ExperimentalCase, experimental_cases,
                        experimental_sets, select_experimental_cases)
from .model import BoltMaterial, ConnectionInput


# ---------------------------------------------------------------------------
# result
# ---------------------------------------------------------------------------
@dataclass
class CalibrationResult:
    mu: float
    ck_lambda: float
    initial_mu: float
    initial_lambda: float
    rmse: float
    initial_rmse: float
    mean_ratio: float
    initial_mean_ratio: float
    max_error: float
    initial_max_error: float
    n_cases: int
    evaluations: int
    history: List[Tuple[float, float, float]] = field(default_factory=list)
    cases: List[Tuple[str, float, float, float]] = field(default_factory=list)

    @property
    def improvement(self) -> float:
        if self.initial_rmse <= 0:
            return 0.0
        return 1.0 - self.rmse / self.initial_rmse

    def apply(self, ci: ConnectionInput) -> None:
        """Write the fitted coefficients into a connection input."""
        ci.bolt_mat.mu = float(self.mu)
        ci.bolt_mat.ck_lambda = float(self.ck_lambda)

    def as_dict(self) -> Dict:
        d = asdict(self)
        d["improvement"] = self.improvement
        return d

    def format(self) -> str:
        lines = [
            "Crawford & Kulak parameter calibration",
            "=" * 62,
            f"specimens                         : {self.n_cases}",
            f"mu      : {self.initial_mu:10.3f} -> {self.mu:10.3f}  1/m "
            f"({self.initial_mu*0.0254:7.3f} -> {self.mu*0.0254:7.3f} 1/in)",
            f"lambda  : {self.initial_lambda:10.3f} -> {self.ck_lambda:10.3f}",
            f"relative RMSE                     : {self.initial_rmse:10.4f} "
            f"-> {self.rmse:10.4f}  ({self.improvement*100:+.1f} %)",
            f"mean Ptest/Pu                     : "
            f"{self.initial_mean_ratio:10.3f} -> {self.mean_ratio:10.3f}",
            f"max |Ptest/Pu - 1|                : "
            f"{self.initial_max_error:10.3f} -> {self.max_error:10.3f}",
            f"objective evaluations             : {self.evaluations}",
        ]
        if self.cases:
            lines.append("")
            lines.append(f"{'specimen':8s} {'measured':>9s} "
                         f"{'initial':>9s} {'fitted':>9s}")
            for name, meas, p0, p1 in self.cases:
                lines.append(f"{name:8s} {meas/1e3:9.1f} {p0/1e3:9.1f} "
                             f"{p1/1e3:9.1f}")
        return "\n".join(lines)


@dataclass
class FeaCalibrationResult:
    """Bias calibration of the nonlinear FE fallback capacity."""
    factor: float
    initial_factor: float
    rmse: float
    initial_rmse: float
    mean_ratio: float
    initial_mean_ratio: float
    n_cases: int
    n_used: int
    cases: List[Tuple[str, float, float, float]] = field(default_factory=list)
    errors: List[Tuple[str, str]] = field(default_factory=list)

    @property
    def improvement(self) -> float:
        if self.initial_rmse <= 0:
            return 0.0
        return 1.0 - self.rmse / self.initial_rmse

    def apply(self, ci: ConnectionInput) -> None:
        ci.fem.capacity_factor = float(self.factor)

    def format(self) -> str:
        lines = [
            "FE fallback capacity calibration",
            "=" * 62,
            f"specimens used                    : {self.n_used}/{self.n_cases}",
            f"capacity factor                   : {self.initial_factor:.4f}"
            f" -> {self.factor:.4f}",
            f"relative RMSE                     : {self.initial_rmse:10.4f}"
            f" -> {self.rmse:10.4f}  ({self.improvement*100:+.1f} %)",
            f"mean Ptest/Pfe                    : "
            f"{self.initial_mean_ratio:10.3f} -> {self.mean_ratio:10.3f}",
        ]
        if self.cases:
            lines.append("")
            lines.append(f"{'specimen':18s} {'measured':>9s} "
                         f"{'raw FE':>9s} {'fitted':>9s}")
            for name, meas, raw, fitted in self.cases:
                lines.append(f"{name:18s} {meas/1e3:9.1f} {raw/1e3:9.1f} "
                             f"{fitted/1e3:9.1f}")
        if self.errors:
            lines.append("")
            lines.append("not used:")
            for name, err in self.errors:
                lines.append(f"  {name}: {err}")
        return "\n".join(lines)

    def as_dict(self) -> Dict:
        d = asdict(self)
        d["improvement"] = self.improvement
        return d


# ---------------------------------------------------------------------------
# objective and optimiser
# ---------------------------------------------------------------------------
def _objective(x: Sequence[float], cases: Sequence[ExperimentalCase],
               delta_max: Optional[float] = None,
               return_details: bool = False):
    mu = math.exp(float(x[0]))
    lam = math.exp(float(x[1]))
    errs = []
    details = []
    for case in cases:
        try:
            from .icr import solve_from_eccentricity
            bm = BoltMaterial()
            c = solve_from_eccentricity(
                case.bolts_xy, (0.0, -1.0), case.eccentricity,
                delta_max=bm.delta_max if delta_max is None else delta_max,
                mu=mu, lam=lam).C
            pu = c * case.bolt_rult
            err = (pu - case.failure_load) / case.failure_load
        except Exception:
            pu = 0.0
            err = 10.0
        errs.append(err)
        details.append((case.name, case.failure_load, pu))
    rmse = math.sqrt(sum(e * e for e in errs) / max(len(errs), 1))
    if return_details:
        return rmse, mu, lam, details
    return rmse


def _nelder_mead(f: Callable[[Sequence[float]], float],
                 x0: Sequence[float], step: Sequence[float],
                 lo: Sequence[float], hi: Sequence[float],
                 max_iter: int = 250, tol: float = 1e-7):
    """Small bounded Nelder-Mead implementation (no SciPy dependency)."""
    n = len(x0)

    def clip(x):
        return [min(max(x[i], lo[i]), hi[i]) for i in range(n)]

    simplex = [clip(x0)]
    for i in range(n):
        p = list(simplex[0])
        p[i] = min(max(p[i] + step[i], lo[i]), hi[i])
        simplex.append(p)
    values = [f(p) for p in simplex]
    evals = n + 1
    history = [(simplex[0][0], simplex[0][1], values[0])]

    for _ in range(max_iter):
        order = sorted(range(n + 1), key=lambda i: values[i])
        simplex = [simplex[i] for i in order]
        values = [values[i] for i in order]
        history.append((simplex[0][0], simplex[0][1], values[0]))
        # converged in both parameter and function space
        spread = max(max(abs(simplex[i][j] - simplex[0][j])
                         for i in range(1, n + 1)) for j in range(n))
        if spread < tol and abs(values[-1] - values[0]) < tol:
            break
        centroid = [sum(simplex[i][j] for i in range(n)) / n for j in range(n)]
        xr = clip([centroid[j] + (centroid[j] - simplex[-1][j])
                   for j in range(n)])
        fr = f(xr); evals += 1
        if fr < values[0]:
            xe = clip([centroid[j] + 2.0 * (xr[j] - centroid[j])
                       for j in range(n)])
            fe = f(xe); evals += 1
            if fe < fr:
                simplex[-1], values[-1] = xe, fe
            else:
                simplex[-1], values[-1] = xr, fr
        elif fr < values[-2]:
            simplex[-1], values[-1] = xr, fr
        else:
            xc = clip([centroid[j] + 0.5 * (simplex[-1][j] - centroid[j])
                       for j in range(n)])
            fc = f(xc); evals += 1
            if fc < values[-1]:
                simplex[-1], values[-1] = xc, fc
            else:
                for i in range(1, n + 1):
                    simplex[i] = clip([
                        simplex[0][j] + 0.5 * (simplex[i][j] - simplex[0][j])
                        for j in range(n)])
                    values[i] = f(simplex[i]); evals += 1
    order = sorted(range(n + 1), key=lambda i: values[i])
    best = simplex[order[0]]
    return best, values[order[0]], evals, history


def calibrate_ck_parameters(
        cases: Optional[Sequence[ExperimentalCase]] = None,
        mu0: Optional[float] = None,
        lambda0: Optional[float] = None,
        delta_max: Optional[float] = None,
        max_iter: int = 250,
        selection=None) -> CalibrationResult:
    """Fit ``mu`` and ``lambda`` to the experimental failure loads.

    Parameters default to the original C&K values and to the historical
    Badawi-only dataset.  ``selection`` selects named sets/samples through
    :func:`eccbolt.benchmarks.select_experimental_cases`; cases that are not
    meaningful for the rigid-plate C&K method are skipped automatically.
    """
    if selection is not None:
        cases = select_experimental_cases(selection)
    cases = list(experimental_cases() if cases is None else cases)
    cases = [c for c in cases if c.supports_analytical]
    if not cases:
        raise ValueError("no C&K-compatible experimental cases selected")
    bm = BoltMaterial()
    mu0 = float(bm.mu if mu0 is None else mu0)
    lambda0 = float(bm.ck_lambda if lambda0 is None else lambda0)

    def obj(x):
        return _objective(x, cases, delta_max)

    # log-space keeps both coefficients positive; bounds are deliberately wide
    lo = (math.log(1e-2), math.log(0.05))
    hi = (math.log(1e5), math.log(2.0))
    x0 = (math.log(mu0), math.log(lambda0))
    xbest, fbest, evals, history = _nelder_mead(
        obj, x0, (0.35, 0.20), lo, hi, max_iter=max_iter)
    mu, lam = math.exp(xbest[0]), math.exp(xbest[1])

    rmse0, _, _, details0 = _objective(x0, cases, delta_max,
                                       return_details=True)
    _, _, _, details1 = _objective(xbest, cases, delta_max,
                                   return_details=True)
    ratios0 = [d[1] / d[2] for d in details0 if d[2] > 0]
    ratios1 = [d[1] / d[2] for d in details1 if d[2] > 0]
    return CalibrationResult(
        mu=mu, ck_lambda=lam, initial_mu=mu0, initial_lambda=lambda0,
        rmse=fbest, initial_rmse=rmse0,
        mean_ratio=sum(ratios1) / len(ratios1),
        initial_mean_ratio=sum(ratios0) / len(ratios0),
        max_error=max((abs(r - 1.0) for r in ratios1), default=0.0),
        initial_max_error=max((abs(r - 1.0) for r in ratios0), default=0.0),
        n_cases=len(cases), evaluations=evals, history=history,
        cases=[(n, m, p0, p1) for (n, m, p0), (_, _, p1)
               in zip(details0, details1)])


def calibrate_fea_parameters(
        cases: Optional[Sequence[ExperimentalCase]] = None,
        selection=None,
        mesh: float = 0.012,
        plate_nonlinear: bool = False,
        fea_options: Optional[Dict] = None,
        verbose: bool = False) -> FeaCalibrationResult:
    """Calibrate the FE fallback capacity bias against selected specimens.

    The FE model is first run with ``capacity_factor = 1`` for every selected
    specimen.  The factor that minimises the relative least-squares error is

        s = sum(f_i/t_i) / sum((f_i/t_i)^2)

    where ``f_i`` is the raw FE capacity and ``t_i`` the measured load.  The
    result is stored in ``ConnectionInput.fem.capacity_factor``.
    """
    if selection is not None:
        cases = select_experimental_cases(selection)
    cases = list(experimental_cases() if cases is None else cases)
    cases = [c for c in cases if c.supports_fea]
    if not cases:
        raise ValueError("no FE-compatible experimental cases selected")
    options = dict(fea_options or {})
    options.pop("capacity_factor", None)
    details = []
    errors = []
    for case in cases:
        if verbose:
            print(f"  FE calibration: {case.case_id} ...", flush=True)
        try:
            res = case.fea_analysis(mesh=mesh,
                                    plate_nonlinear=plate_nonlinear,
                                    capacity_factor=1.0, **options)
            cap = res.get("raw_capacity") or res.get("capacity")
            if not res.get("ok") or not cap:
                errors.append((case.case_id,
                               res.get("message", "no FE capacity")))
                continue
            details.append((case, float(cap)))
        except Exception as exc:
            errors.append((case.case_id, f"{type(exc).__name__}: {exc}"))
    if not details:
        raise ValueError("the FE model produced no usable capacities: "
                         + "; ".join(e for _, e in errors[:3]))

    q = [cap / case.failure_load for case, cap in details]
    factor = sum(q) / sum(x * x for x in q) if q else 1.0
    cases_out = []
    for case, cap in details:
        cases_out.append((case.case_id, case.failure_load, cap,
                          cap * factor))

    def stats(fac):
        errs = [(fac * cap - case.failure_load) / case.failure_load
                for case, cap in details]
        rmse = math.sqrt(sum(e * e for e in errs) / len(errs))
        ratios = [case.failure_load / (fac * cap) for case, cap in details]
        return rmse, sum(ratios) / len(ratios)

    rmse0, ratio0 = stats(1.0)
    rmse1, ratio1 = stats(factor)
    return FeaCalibrationResult(
        factor=factor, initial_factor=1.0, rmse=rmse1, initial_rmse=rmse0,
        mean_ratio=ratio1, initial_mean_ratio=ratio0,
        n_cases=len(cases), n_used=len(details), cases=cases_out,
        errors=errors)


def calibrate_selected(selection=None, include_ck: bool = True,
                       include_fea: bool = False,
                       fea_mesh: float = 0.012,
                       fea_options: Optional[Dict] = None,
                       max_iter: int = 250,
                       mu0: Optional[float] = None,
                       lambda0: Optional[float] = None,
                       verbose: bool = False) -> Dict[str, object]:
    """Run the requested calibrations on the selected experimental data."""
    out: Dict[str, object] = {}
    cases = (select_experimental_cases(selection)
             if selection is not None else experimental_cases())
    if include_ck:
        ck_cases = [c for c in cases if c.supports_analytical]
        if ck_cases:
            out["ck"] = calibrate_ck_parameters(cases=ck_cases,
                                                max_iter=max_iter,
                                                mu0=mu0, lambda0=lambda0)
    if include_fea:
        fea_cases = [c for c in cases if c.supports_fea]
        if fea_cases:
            out["fea"] = calibrate_fea_parameters(
                cases=fea_cases, mesh=fea_mesh, fea_options=fea_options,
                verbose=verbose)
    if not out:
        raise ValueError("no calibration could be run for the selection")
    return out


# convenient public aliases
calibrate = calibrate_ck_parameters
calibrate_fea = calibrate_fea_parameters


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _selection_from_args(sets_arg, samples_arg):
    sets = experimental_sets()
    if not sets_arg and not samples_arg:
        return None
    if sets_arg:
        keys = [s.strip() for s in sets_arg.split(",") if s.strip()]
        if "all" in keys or "*" in keys:
            keys = list(sets)
    else:
        keys = []
    selection: Dict[str, object] = {k: None for k in keys}
    for spec in samples_arg or []:
        for part in str(spec).split(";"):
            part = part.strip()
            if not part:
                continue
            if ":" not in part:
                raise SystemExit(f"sample must be SET:NAME, got {part!r}")
            key, name = part.split(":", 1)
            selection.setdefault(key, [])
            if selection[key] is None:
                selection[key] = []
            selection[key].append(name)
    return selection


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(
        description="Calibrate the C&K mu/lambda parameters and/or the FE "
                    "fallback against the physical test results.")
    ap.add_argument("--json", metavar="PATH",
                    help="write the fitted parameters and details to JSON")
    ap.add_argument("--mu", type=float, help="initial mu [1/m]")
    ap.add_argument("--lambda", dest="lam", type=float,
                    help="initial lambda")
    ap.add_argument("--max-iter", type=int, default=250)
    ap.add_argument("--list-sets", action="store_true",
                    help="list the available experimental sets and samples")
    ap.add_argument("--sets", metavar="KEYS",
                    help="comma-separated set keys, or 'all'")
    ap.add_argument("--sample", action="append", metavar="SET:NAME",
                    help="select one sample (repeatable; SET:NAME)")
    ap.add_argument("--no-ck", action="store_true",
                    help="skip the analytical C&K calibration")
    ap.add_argument("--fea", action="store_true",
                    help="also calibrate the FE fallback capacity factor")
    ap.add_argument("--fea-mesh", type=float, default=0.012,
                    help="FE mesh size used during calibration [m]")
    args = ap.parse_args(argv)

    if args.list_sets:
        for key, cases in experimental_sets().items():
            print(f"{key}: {cases[0].set_name if cases else key}")
            for c in cases:
                print(f"  {c.name:12s} {c.note}")
        return 0

    selection = _selection_from_args(args.sets, args.sample)
    try:
        results = calibrate_selected(
            selection=selection, include_ck=not args.no_ck,
            include_fea=args.fea, fea_mesh=args.fea_mesh,
            max_iter=args.max_iter, mu0=args.mu, lambda0=args.lam,
            verbose=True)
    except ValueError as exc:
        print(f"Calibration error: {exc}")
        return 2
    for name in ("ck", "fea"):
        result = results.get(name)
        if result is not None:
            print(result.format())
            print()
    if args.json:
        payload = {k: v.as_dict() for k, v in results.items()}
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=1)
        print(f"written: {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

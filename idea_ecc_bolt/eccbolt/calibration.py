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

from .benchmarks import ExperimentalCase, experimental_cases
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
        max_iter: int = 250) -> CalibrationResult:
    """Fit ``mu`` and ``lambda`` to the experimental failure loads.

    Parameters default to the original C&K values and to all experimental
    specimens available in :func:`eccbolt.benchmarks.experimental_cases`.
    """
    cases = list(experimental_cases() if cases is None else cases)
    if not cases:
        raise ValueError("no experimental cases supplied for calibration")
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


# convenient public alias
calibrate = calibrate_ck_parameters


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(
        description="Calibrate the C&K mu/lambda parameters against the "
                    "physical test results.")
    ap.add_argument("--json", metavar="PATH",
                    help="write the fitted parameters and details to JSON")
    ap.add_argument("--mu", type=float, help="initial mu [1/m]")
    ap.add_argument("--lambda", dest="lam", type=float,
                    help="initial lambda")
    ap.add_argument("--max-iter", type=int, default=250)
    args = ap.parse_args(argv)
    result = calibrate_ck_parameters(mu0=args.mu, lambda0=args.lam,
                                     max_iter=args.max_iter)
    print(result.format())
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(result.as_dict(), fh, indent=1)
        print(f"\nwritten: {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

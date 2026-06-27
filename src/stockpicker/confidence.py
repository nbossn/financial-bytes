"""
confidence.py — The confidence matrix.

Turns each signal's measured historical accuracy into a weight. The headline
metric is the Information Coefficient (IC): the Spearman rank-correlation between
a signal's cross-sectional values on day t and the realized forward return over
the prediction horizon. IC is scale-free, outlier-robust, and measures exactly
what we care about — predicted-vs-actual ordering.

Cold-start problem
------------------
A freshly-built signal has little IC history, so its measured IC is noisy. We
shrink each measured IC toward a literature-derived prior (James-Stein style):
the less data we have, the more we trust the prior. As the accuracy ledger fills
up over months, the data takes over and the prior fades.

    IC*_k = (1 - lambda_k) * IC_measured_k  +  lambda_k * IC_prior_k

    lambda_k = prior_strength / (prior_strength + n_k)

where n_k is the number of independent cross-sections measured for signal k.

Weights
-------
Shrunk ICs are floored at 0 (a signal that doesn't predict gets no vote), then
normalized to sum to 1. Optionally divided by signal volatility (ICIR) to reward
consistency. Correlated signals are de-emphasized via an optional correlation
penalty so we don't double-count (e.g. momentum + 52-week-high).

This module is intentionally pure / dependency-light (numpy only) so it is easy
to audit and easy for a future model (Fable) to reason about.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

# ---------------------------------------------------------------------------
# Literature-derived IC priors (per ~1-day to multi-day horizon).
#
# These are deliberately MODEST. Published long-short alphas translate to small
# daily ICs (a "good" equity signal sits around IC 0.03-0.06). We anchor priors
# near the low end so the cold-start system is humble until real IC is measured.
# Sources noted per signal in the methodology doc.
# ---------------------------------------------------------------------------
IC_PRIORS: dict[str, float] = {
    "momentum_12_1":     0.045,  # Jegadeesh-Titman; robust, crash-prone
    "reversal_5d":       0.035,  # Jegadeesh 1990; strong gross, cost-fragile
    "overnight_drift":   0.040,  # Lou-Polk-Skouras; persistent, unique horizon
    "earnings_sue":      0.060,  # Bernard-Thomas PEAD; strongest event signal
    "pead_drift":        0.050,  # post-earnings drift continuation
    "revision_proxy":    0.040,  # analyst target proximity (revision proxy)
    "sentiment":         0.025,  # news sentiment; noisy, decays fast
    "insider_cluster":   0.035,  # Lakonishok-Lee; sparse but uncorrelated
    "vol_signal":        0.020,  # vol/overnight-vol; mostly a gate, low standalone
    "technical_52w":     0.040,  # George-Hwang 52-week-high proximity
    # ── Finviz-derived signals (no API key; validated live vs MU screenshots) ──
    "short_squeeze":     0.045,  # ELEVATED per Nick's directive — short data
                                 #   weighted heavily; squeeze fuel + breakout trigger
    "short_pressure":    0.025,  # short float as market-sentiment deviation read
    "analyst_recom":     0.040,  # finviz consensus recommendation (1=buy..5=sell)
    "quality":           0.030,  # ROE/ROIC/margins composite (Novy-Marx quality)
}

# Prior strength = pseudo-count of cross-sections the prior is "worth".
# Higher => prior dominates longer. 60 ≈ 3 trading months, our min-sample bar.
DEFAULT_PRIOR_STRENGTH = 60.0


@dataclass
class SignalConfidence:
    """Per-signal confidence record — the row of the confidence matrix."""
    name: str
    ic_measured: float          # measured IC over available history (may be nan)
    ic_prior: float             # literature prior
    n_obs: int                  # independent cross-sections measured
    ic_shrunk: float = 0.0      # post-shrinkage IC
    icir: float = float("nan")  # IC information ratio (mean/std of IC series)
    shrink_lambda: float = 1.0  # how much weight went to the prior
    weight: float = 0.0         # final normalized weight
    hit_rate: float = float("nan")  # directional hit rate (diagnostic)


@dataclass
class ConfidenceMatrix:
    """The full confidence matrix: one SignalConfidence per signal + weights."""
    rows: list[SignalConfidence] = field(default_factory=list)
    prior_strength: float = DEFAULT_PRIOR_STRENGTH
    horizon_days: int = 1

    @property
    def weights(self) -> dict[str, float]:
        return {r.name: r.weight for r in self.rows}

    def as_table(self) -> str:
        hdr = (f"{'Signal':<18}{'IC_meas':>9}{'IC_prior':>9}{'n':>6}"
               f"{'lambda':>8}{'IC*':>8}{'ICIR':>7}{'hit%':>7}{'WEIGHT':>9}")
        lines = [hdr, "-" * len(hdr)]
        for r in sorted(self.rows, key=lambda x: x.weight, reverse=True):
            icir = "  n/a" if np.isnan(r.icir) else f"{r.icir:6.2f}"
            hit = "  n/a" if np.isnan(r.hit_rate) else f"{r.hit_rate*100:5.1f}"
            lines.append(
                f"{r.name:<18}{r.ic_measured:>9.4f}{r.ic_prior:>9.4f}{r.n_obs:>6}"
                f"{r.shrink_lambda:>8.2f}{r.ic_shrunk:>8.4f}{icir:>7}{hit:>7}"
                f"{r.weight*100:>8.1f}%"
            )
        return "\n".join(lines)


def shrink_ic(ic_measured: float, ic_prior: float, n_obs: int,
              prior_strength: float = DEFAULT_PRIOR_STRENGTH) -> tuple[float, float]:
    """Bayesian shrinkage of a measured IC toward its prior.

    Returns (ic_shrunk, lambda) where lambda in [0,1] is the weight on the prior.
    """
    if n_obs <= 0 or np.isnan(ic_measured):
        return ic_prior, 1.0
    lam = prior_strength / (prior_strength + n_obs)
    ic_shrunk = (1.0 - lam) * ic_measured + lam * ic_prior
    return ic_shrunk, lam


def build_confidence_matrix(
    measured: dict[str, dict],
    *,
    prior_strength: float = DEFAULT_PRIOR_STRENGTH,
    horizon_days: int = 1,
    correlation: np.ndarray | None = None,
    signal_order: list[str] | None = None,
    use_icir: bool = True,
) -> ConfidenceMatrix:
    """Build the confidence matrix from measured per-signal accuracy.

    Parameters
    ----------
    measured : dict
        signal_name -> {"ic": float, "n": int, "icir": float, "hit_rate": float}
    correlation : optional (K x K) ndarray
        Correlation matrix of the signals (same order as signal_order). If given,
        weights are penalized by average absolute correlation to de-duplicate.
    use_icir : bool
        If True, multiply shrunk IC by a consistency factor derived from ICIR.

    Returns
    -------
    ConfidenceMatrix with .weight populated on each row (sums to 1).
    """
    rows: list[SignalConfidence] = []
    for name, prior in IC_PRIORS.items():
        m = measured.get(name, {})
        ic_meas = float(m.get("ic", float("nan")))
        n = int(m.get("n", 0))
        icir = float(m.get("icir", float("nan")))
        hit = float(m.get("hit_rate", float("nan")))
        ic_shrunk, lam = shrink_ic(ic_meas, prior, n, prior_strength)
        rows.append(SignalConfidence(
            name=name, ic_measured=ic_meas, ic_prior=prior, n_obs=n,
            ic_shrunk=ic_shrunk, icir=icir, shrink_lambda=lam, hit_rate=hit,
        ))

    # Raw weight basis = max(shrunk IC, 0). A non-predictive signal gets 0 vote.
    basis = np.array([max(r.ic_shrunk, 0.0) for r in rows], dtype=float)

    # Consistency tilt: reward signals whose IC is stable (high ICIR).
    if use_icir:
        icir_factor = np.array([
            1.0 if np.isnan(r.icir) else float(np.clip(0.5 + 0.5 * r.icir, 0.25, 2.0))
            for r in rows
        ])
        basis = basis * icir_factor

    # Correlation penalty: divide by (1 + mean abs corr to other signals).
    if correlation is not None and signal_order is not None:
        idx = {n: i for i, n in enumerate(signal_order)}
        penalties = np.ones(len(rows))
        for j, r in enumerate(rows):
            if r.name in idx:
                i = idx[r.name]
                row_corr = np.abs(correlation[i])
                # exclude self
                mean_corr = (row_corr.sum() - 1.0) / max(len(row_corr) - 1, 1)
                penalties[j] = 1.0 + max(mean_corr, 0.0)
        basis = basis / penalties

    total = basis.sum()
    if total <= 0:
        # Degenerate: fall back to equal weight.
        w = np.full(len(rows), 1.0 / len(rows))
    else:
        w = basis / total
    for r, wi in zip(rows, w):
        r.weight = float(wi)

    return ConfidenceMatrix(rows=rows, prior_strength=prior_strength,
                            horizon_days=horizon_days)

"""
stockpicker — Confidence-weighted composite stock predictor for financial-bytes.

A daily-updating quantitative picker that combines ~10 empirically-validated
signals into a single forward-return forecast. Each signal's weight is calibrated
by its measured Information Coefficient (IC) — its historical rank-correlation with
realized forward returns — shrunk toward literature priors during the cold-start
period before enough IC history accumulates.

Modules
-------
engine      : signal computation + IC backtest + composite scoring (the core)
confidence  : IC-based weighting with Bayesian shrinkage (the "confidence matrix")
risk        : per-name risk classification (P/E, revenue, beta, speculation tier)

Design references
-----------------
- Arc 40 (vault): compound-signal decision matrix — downgrade-only composition
- Arc 73 (vault): implied-consensus earnings method
- Arc 58 (vault): overnight-drift thesis (after-hours = noise, open = signal)
- Bernard & Thomas 1989 (PEAD), Jegadeesh & Titman 1993 (momentum),
  George & Hwang 2004 (52-week high), Lou-Polk-Skouras 2019 (overnight),
  Lakonishok & Lee 2001 (insider clusters), Grinold & Kahn (IC weighting).

This engine RANKS and PREDICTS only. It never trades. All output is for review.
"""

__version__ = "0.1.0"

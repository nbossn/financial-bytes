# stockpicker

Confidence-weighted composite stock predictor for financial-bytes. Combines ~10
empirically-validated signals into one ranked prediction per stock; each signal
is weighted by its measured Information Coefficient (IC), shrunk toward academic
priors during cold-start. **Ranks and predicts only — never trades.**

## Run
```bash
cd /home/nboss/financial-bytes && source .venv/bin/activate
PYTHONPATH=. python -m src.stockpicker.engine    # universe scores + IC backtest
PYTHONPATH=. python -m src.stockpicker.report    # enrich top 70 -> 20 picks
```
Outputs land in `data/stockpicker/{universe_scores,picks}.json`.

## Modules
- `engine.py` — signal computation, IC backtest, composite scoring (the core)
- `confidence.py` — IC-shrinkage weighting (the confidence matrix)
- `risk.py` — per-name risk-tier classifier (Conservative→Speculative)
- `report.py` — enrichment (fundamentals/earnings) + pick selection

## Full docs (Obsidian vault)
- Methodology: `Projects/stock-picker/METHODOLOGY.md`
- Sample report: `Projects/stock-picker/REPORT-2026-06-27.md`

## Status
v0.1 cold-start. Event signals (earnings/revision/insider/sentiment) carry
literature priors until the accuracy ledger accumulates. Pending feeds:
consensus EPS, after-hours quotes, live news sentiment. See methodology §8.

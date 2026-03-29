"""Portfolio configuration — defines per-portfolio sources, labels, and recipients."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class PortfolioDef:
    name: str                               # identifier: used in DB + output paths
    label: str                              # display name in newsletter
    csv_path: str | None = None             # static portfolio CSV
    transactions_path: str | None = None    # Robinhood transaction history CSV
    fidelity_positions: str | None = None   # Fidelity Portfolio_Positions_*.csv export
    fidelity_account_filter: str | None = None  # Optional: filter by account name substring
    purchase_history: str | None = None     # JSON file with per-lot acquisition dates/costs
    email_recipients: list[str] = field(default_factory=list)


def load_portfolio_defs(config_path: str | Path | None = None) -> list[PortfolioDef]:
    """Load portfolio definitions from portfolios.json.

    Falls back to a single 'default' portfolio from PORTFOLIO_CSV_PATH env if
    the config file doesn't exist — preserving backward compatibility.
    """
    from src.config import settings

    path = Path(config_path or settings.portfolios_config)

    if not path.exists():
        # Backward compat: single portfolio from env
        return [PortfolioDef(
            name="default",
            label="Portfolio",
            csv_path=settings.portfolio_csv_path,
        )]

    with path.open(encoding="utf-8") as f:
        raw = json.load(f)

    if not isinstance(raw, list):
        from src.portfolio.reader import PortfolioReadError
        raise PortfolioReadError("portfolios.json must be a JSON array of portfolio definitions")

    defs = []
    for item in raw:
        if not item.get("name"):
            from src.portfolio.reader import PortfolioReadError
            raise PortfolioReadError("Each portfolio definition must have a 'name' field")
        defs.append(PortfolioDef(
            name=item["name"],
            label=item.get("label", item["name"].replace("_", " ").title()),
            csv_path=item.get("csv_path") or item.get("csv"),
            transactions_path=item.get("transactions_path") or item.get("transactions"),
            fidelity_positions=item.get("fidelity_positions"),
            fidelity_account_filter=item.get("fidelity_account_filter"),
            purchase_history=item.get("purchase_history"),
            email_recipients=item.get("email_recipients", []),
        ))

    if not defs:
        from src.portfolio.reader import PortfolioReadError
        raise PortfolioReadError("portfolios.json must contain at least one portfolio definition")

    return defs

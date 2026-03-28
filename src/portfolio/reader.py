import csv
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

from loguru import logger

from src.portfolio.models import Holding


REQUIRED_COLUMNS = {"ticker", "shares", "cost_basis", "purchase_date"}


class PortfolioReadError(Exception):
    pass


def read_portfolio(csv_path: str | Path) -> list[Holding]:
    """Read portfolio holdings from a CSV file.

    Expected columns: ticker, shares, cost_basis, purchase_date (YYYY-MM-DD)

    Returns:
        List of Holding objects.

    Raises:
        PortfolioReadError: If the file is missing, malformed, or has invalid data.
    """
    path = Path(csv_path)
    if not path.exists():
        raise PortfolioReadError(f"Portfolio file not found: {path}")

    holdings = []
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)

        if reader.fieldnames is None:
            raise PortfolioReadError("Portfolio CSV is empty")

        missing = REQUIRED_COLUMNS - {col.strip().lower() for col in reader.fieldnames}
        if missing:
            raise PortfolioReadError(f"Portfolio CSV missing columns: {missing}")

        for row_num, row in enumerate(reader, start=2):
            try:
                ticker = row["ticker"].strip().upper()
                if not ticker:
                    raise ValueError("ticker cannot be empty")

                shares = Decimal(row["shares"].strip())
                if shares <= 0:
                    raise ValueError(f"shares must be positive, got {shares}")

                cost_basis = Decimal(row["cost_basis"].strip())
                if cost_basis <= 0:
                    raise ValueError(f"cost_basis must be positive, got {cost_basis}")

                purchase_date = date.fromisoformat(row["purchase_date"].strip())

                holdings.append(
                    Holding(
                        ticker=ticker,
                        shares=shares,
                        cost_basis=cost_basis,
                        purchase_date=purchase_date,
                    )
                )
                logger.debug(f"Loaded holding: {ticker} {shares}@{cost_basis}")

            except (ValueError, InvalidOperation, KeyError) as e:
                raise PortfolioReadError(f"Row {row_num} invalid: {e}") from e

    if not holdings:
        raise PortfolioReadError("Portfolio CSV contains no holdings")

    logger.info(f"Loaded {len(holdings)} holdings: {[h.ticker for h in holdings]}")
    return holdings


def save_portfolio_to_db(holdings: list[Holding], portfolio_name: str = "default") -> None:
    """Upsert portfolio holdings into the database, scoped to portfolio_name."""
    from src.db.models import Portfolio
    from src.db.session import get_db

    with get_db() as db:
        # Clear only this portfolio's rows (not other portfolios)
        db.query(Portfolio).filter_by(portfolio_name=portfolio_name).delete()
        for holding in holdings:
            db.add(
                Portfolio(
                    portfolio_name=portfolio_name,
                    ticker=holding.ticker,
                    shares=holding.shares,
                    cost_basis=holding.cost_basis,
                    purchase_date=holding.purchase_date,
                )
            )
    logger.info(f"Saved {len(holdings)} holdings to database (portfolio: {portfolio_name})")

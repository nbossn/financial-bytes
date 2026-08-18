"""Fidelity positions CSV header-casing tolerance.

Fidelity silently changed its export header casing between 2026-06-24 and
2026-08-18 (`Account Number` -> `Account number`, `Average Cost Basis` ->
`Average cost basis`). The reader looked columns up by exact key, so every
row lost its account number and cost basis and was dropped — the whole
export parsed to zero holdings and raised "No equity holdings found".

These tests pin the reader to both casings.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from src.portfolio.fidelity_reader import read_fidelity_positions

TITLE_CASE_HEADER = (
    "Account Number,Account Name,Symbol,Description,Quantity,Last Price,"
    "Last Price Change,Current Value,Today's Gain/Loss Dollar,"
    "Today's Gain/Loss Percent,Total Gain/Loss Dollar,Total Gain/Loss Percent,"
    "Percent Of Account,Cost Basis Total,Average Cost Basis,Type"
)

SENTENCE_CASE_HEADER = (
    "Account number,Account name,Symbol,Description,Quantity,Last price,"
    "Last price change,Current value,Today's gain/loss dollar,"
    "Today's gain/loss percent,Total gain/loss dollar,Total gain/loss percent,"
    "Percent of account,Cost basis total,Average cost basis,Type"
)

ROWS = "\n".join(
    [
        "Z38250874,Trust: Under Agreement,SPAXX**,HELD IN MONEY MARKET,,,,$1325.71,,,,,0.06%,,,Cash,",
        "Z38250874,Trust: Under Agreement,NVDA,NVIDIA CORPORATION COM,1008.126,$219.99,"
        "-$5.02,$221777.63,-$5060.80,-2.24%,+$71920.62,+47.99%,10.02%,$149857.01,$148.65,Cash,",
        "Z32785271,Trust: Under Agreement,MSFT,MICROSOFT CORP,100,$500.00,"
        "$1.00,$50000.00,$100.00,0.20%,+$10000.00,+25.00%,5.00%,$40000.00,$400.00,Cash,",
        "235984228,Health Savings Account,FCNTX,FIDELITY CONTRAFUND,10,$20.00,"
        "$0.10,$200.00,$1.00,0.50%,+$50.00,+33.33%,1.00%,$150.00,$15.00,Cash,",
    ]
)


def _write(tmp_path, header: str, name: str):
    path = tmp_path / name
    # Fidelity exports carry a UTF-8 BOM.
    path.write_text("﻿" + header + "\n" + ROWS + "\n", encoding="utf-8")
    return path


@pytest.mark.parametrize(
    "header,name",
    [
        (TITLE_CASE_HEADER, "title_case.csv"),
        (SENTENCE_CASE_HEADER, "sentence_case.csv"),
    ],
)
def test_reader_handles_both_header_casings(tmp_path, header, name):
    """Both Fidelity header casings must parse to the same holdings."""
    path = _write(tmp_path, header, name)

    holdings = read_fidelity_positions(path, account_filter="Z38250874")

    by_ticker = {h.ticker: h for h in holdings}
    assert set(by_ticker) == {"NVDA", "SPAXX"}

    nvda = by_ticker["NVDA"]
    assert nvda.shares == Decimal("1008.126")
    assert nvda.cost_basis == Decimal("148.65")
    assert nvda.account_number == "Z38250874"

    # Money-market share count is derived from Current Value at $1.00/share.
    spaxx = by_ticker["SPAXX"]
    assert spaxx.shares == Decimal("1325.71")
    assert spaxx.cost_basis == Decimal("1.00")


@pytest.mark.parametrize(
    "header,name",
    [
        (TITLE_CASE_HEADER, "title_filter.csv"),
        (SENTENCE_CASE_HEADER, "sentence_filter.csv"),
    ],
)
def test_account_filter_applies_under_both_casings(tmp_path, header, name):
    """The account filter reads Account number/name regardless of casing."""
    path = _write(tmp_path, header, name)

    holdings = read_fidelity_positions(path, account_filter=["Z32785271"])

    assert [h.ticker for h in holdings] == ["MSFT"]
    assert holdings[0].account_number == "Z32785271"


@pytest.mark.parametrize(
    "header,name",
    [
        (TITLE_CASE_HEADER, "title_all.csv"),
        (SENTENCE_CASE_HEADER, "sentence_all.csv"),
    ],
)
def test_unfiltered_read_skips_only_non_equity(tmp_path, header, name):
    """With no filter, FCNTX is the only row dropped under either casing."""
    path = _write(tmp_path, header, name)

    holdings = read_fidelity_positions(path)

    assert {h.ticker for h in holdings} == {"NVDA", "MSFT", "SPAXX"}

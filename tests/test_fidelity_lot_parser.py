"""Parse per-lot acquisition dates out of Fidelity's Tax Loss Harvesting table.

Fidelity's positions export has no lot dates, which left holding period unknown
for ~97% of positions (tax rate reported as a 24%-41% band) and let the analyst
agent invent one. The TLH page ("Tax Loss Harvesting Experience",
/ftgw/digital/harvest-tax-losses/) renders a table that *does* carry them: each
position row is followed by one row per tax lot, labelled
"Select lot acquired on Jun-08-2026 / Jun-08-2026 60 shares".

Row shapes below are copied from a live capture on 2026-08-18.

Scope limit worth remembering: the TLH tool only lists positions currently at a
loss. It resolves holding period exactly where the harvest decision lives, but it
will never return lot dates for winners.
"""
from __future__ import annotations

import pytest

from src.portfolio.fidelity_lots import parse_tlh_lot_rows

HEADER = [
    "Symbol", "Cost basis", "Current value", "Short-term loss",
    "Long-term loss", "Total loss", "Shares", "Est. sale value", "Est. loss",
]

MRVL_POSITION = [
    "Select position\nMARVELL TE",
    "MRVL\nMARVELL TE",
    "$285.12\nAverage",
    "$12,769.20",
    "-$4,337.95",
    "$0.00",
    "-$4,337.95",
    "0/60\n\nSell all shares",
    "--",
]
MRVL_LOT = [
    "Select lot acquired on Jun-08-2026\n Jun-08-2026 60 shares",
    "$285.12\nAverage",
    "$12,769.20",
    "-$4,337.95",
    "--", "--", "--",
]

DRAM_POSITION = [
    "Select position\nROUNDHILL",
    "DRAM\nROUNDHILL",
    "$71.42\nAverage",
    "$11,066.00",
    "-$3,217.50",
    "$0.00",
    "-$3,217.50",
    "0/200\n\nSell all shares",
    "--",
]
DRAM_LOT_1 = [
    "Select lot acquired on Jun-29-2026\n Jun-29-2026 50 shares",
    "$72.91\nAverage", "$2,766.50", "-$879.00", "--", "--", "--",
]
DRAM_LOT_2 = [
    "Select lot acquired on Jun-29-2026\n Jun-29-2026 150 shares",
    "$70.92\nAverage", "$8,299.50", "-$2,338.50", "--", "--", "--",
]

FULL_TABLE = [HEADER, MRVL_POSITION, MRVL_LOT, DRAM_POSITION, DRAM_LOT_1, DRAM_LOT_2]


class TestLotExtraction:
    def test_extracts_lots_keyed_by_ticker(self):
        result = parse_tlh_lot_rows(FULL_TABLE)
        assert set(result) == {"MRVL", "DRAM"}

    def test_single_lot_fields(self):
        lot = parse_tlh_lot_rows(FULL_TABLE)["MRVL"][0]
        assert lot["purchase_date"] == "2026-06-08"
        assert lot["shares"] == 60.0
        assert lot["cost_basis"] == 285.12

    def test_multiple_lots_are_all_captured(self):
        lots = parse_tlh_lot_rows(FULL_TABLE)["DRAM"]
        assert len(lots) == 2
        assert {l["shares"] for l in lots} == {50.0, 150.0}
        assert {l["cost_basis"] for l in lots} == {72.91, 70.92}
        assert {l["purchase_date"] for l in lots} == {"2026-06-29"}

    def test_lots_attach_to_the_preceding_position(self):
        """A lot row carries no ticker — it inherits the position above it."""
        result = parse_tlh_lot_rows(FULL_TABLE)
        assert all(l["shares"] in (50.0, 150.0) for l in result["DRAM"])
        assert all(l["shares"] == 60.0 for l in result["MRVL"])


class TestRobustness:
    def test_header_row_is_ignored(self):
        assert parse_tlh_lot_rows([HEADER]) == {}

    def test_empty_table(self):
        assert parse_tlh_lot_rows([]) == {}

    def test_position_with_no_lot_rows_is_omitted(self):
        """No lot detail means no date — emit nothing rather than a bare ticker."""
        assert parse_tlh_lot_rows([HEADER, MRVL_POSITION]) == {}

    def test_lot_before_any_position_is_skipped(self):
        """Orphan lot rows must not crash or attach to nothing."""
        assert parse_tlh_lot_rows([HEADER, MRVL_LOT]) == {}

    def test_ragged_and_blank_rows_are_tolerated(self):
        rows = [HEADER, [], ["   "], MRVL_POSITION, MRVL_LOT, ["--"]]
        assert parse_tlh_lot_rows(rows)["MRVL"][0]["shares"] == 60.0

    def test_commas_in_share_counts(self):
        pos = ["Select position\nX", "AAPL\nAPPLE", "$100.00\nAverage",
               "$1.00", "-$1.00", "$0.00", "-$1.00", "0/1,500", "--"]
        lot = ["Select lot acquired on Jan-05-2024\n Jan-05-2024 1,500 shares",
               "$100.00\nAverage", "$1.00", "-$1.00", "--", "--", "--"]
        assert parse_tlh_lot_rows([HEADER, pos, lot])["AAPL"][0]["shares"] == 1500.0

    def test_unparseable_date_is_skipped_not_guessed(self):
        lot = ["Select lot acquired on NOT-A-DATE\n NOT-A-DATE 60 shares",
               "$285.12\nAverage", "$12,769.20", "-$4,337.95", "--", "--", "--"]
        assert parse_tlh_lot_rows([HEADER, MRVL_POSITION, lot]) == {}


class TestMergeIntoPurchaseHistory:
    """Scraped lots must not clobber lots Nick curated by hand."""

    def test_existing_manual_entries_win(self):
        from src.portfolio.fidelity_lots import merge_lot_history

        existing = {
            "_comment": "keep me",
            "NVDA": [{"shares": 4000, "cost_basis": 44.5, "purchase_date": "2023-06-15"}],
        }
        scraped = {"NVDA": [{"shares": 1, "cost_basis": 999.0, "purchase_date": "2026-08-01"}]}

        merged = merge_lot_history(existing, scraped)

        assert merged["NVDA"] == existing["NVDA"], "hand-curated lots are authoritative"
        assert merged["_comment"] == "keep me"

    def test_new_tickers_are_added(self):
        from src.portfolio.fidelity_lots import merge_lot_history

        merged = merge_lot_history(
            {"NVDA": [{"shares": 1, "cost_basis": 1.0, "purchase_date": "2023-01-01"}]},
            {"MRVL": [{"shares": 60, "cost_basis": 285.12, "purchase_date": "2026-06-08"}]},
        )
        assert set(merged) == {"NVDA", "MRVL"}

    def test_comment_keys_are_preserved(self):
        from src.portfolio.fidelity_lots import merge_lot_history

        merged = merge_lot_history({"_note": "x"}, {"MRVL": [
            {"shares": 60, "cost_basis": 285.12, "purchase_date": "2026-06-08"}]})
        assert merged["_note"] == "x"


class TestAccountScopeGuard:
    """The TLH table carries no account column.

    Nick's Fidelity login spans the personal account (Z38250874) and two trust
    sub-accounts. A ticker held in both would otherwise write trust lots into the
    personal purchase history — the exact cross-account mixup that has bitten this
    vault before. Scraped lots are therefore intersected with the target
    portfolio's actual holdings, and share-count disagreements are surfaced.
    """

    def test_tickers_not_in_the_portfolio_are_dropped(self):
        from src.portfolio.fidelity_lots import filter_lots_to_holdings

        lots = {
            "MRVL": [{"shares": 60.0, "cost_basis": 285.12, "purchase_date": "2026-06-08"}],
            "NBIS": [{"shares": 10.0, "cost_basis": 50.0, "purchase_date": "2026-01-02"}],
        }
        kept, dropped, _ = filter_lots_to_holdings(lots, {"MRVL": 60.0})

        assert set(kept) == {"MRVL"}
        assert dropped == ["NBIS"]

    def test_share_count_mismatch_is_reported_not_silently_kept(self):
        from src.portfolio.fidelity_lots import filter_lots_to_holdings

        lots = {"DRAM": [
            {"shares": 50.0, "cost_basis": 72.91, "purchase_date": "2026-06-29"},
            {"shares": 150.0, "cost_basis": 70.92, "purchase_date": "2026-06-29"},
        ]}
        # Portfolio holds 200; lots sum to 200 → agrees.
        _, _, mismatches = filter_lots_to_holdings(lots, {"DRAM": 200.0})
        assert mismatches == []

        # Portfolio holds 500; lots sum to 200 → the lots describe a different account.
        _, _, mismatches = filter_lots_to_holdings(lots, {"DRAM": 500.0})
        assert [m[0] for m in mismatches] == ["DRAM"]

    def test_mismatched_lots_are_still_returned_for_the_caller_to_judge(self):
        from src.portfolio.fidelity_lots import filter_lots_to_holdings

        lots = {"DRAM": [{"shares": 50.0, "cost_basis": 72.91, "purchase_date": "2026-06-29"}]}
        kept, _, mismatches = filter_lots_to_holdings(lots, {"DRAM": 500.0})
        assert "DRAM" in kept and mismatches

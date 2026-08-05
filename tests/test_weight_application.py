"""Which published weights the pipeline actually applies.

`running_weights.json` publishes a weight for all 15 signals and SCORECARD.md
renders every one of them. Only 8 of those weights are ever read: the composite
in `run.py` pins the other 7 to `PRIOR_W` and ignores the measured vector
entirely.

That is invisible today only because `using_measured` is False, so the footer
says "NOT yet used" about the whole file and happens to be right about all 15
rows. The moment max-n crosses MIN_OBS_FOR_WEIGHTS the same footer starts
claiming "Used by the next pipeline run" — true for 8 rows, false for 7.

These tests pin the partition, force it to stay total and disjoint as signals
are added, and require the report to state per-signal what it states today only
per-file.
"""
from __future__ import annotations

import numpy as np
import pytest

from src.stockpicker import accuracy, confidence, run
from src.stockpicker.confidence import IC_PRIORS, PRIOR_W


# ───────────────────────── the partition itself ─────────────────────────

def test_consumers_and_pinned_partition_every_signal():
    """Total and disjoint. A signal wired neither way is silently weightless;
    a signal wired both ways has an ambiguous applied weight. Either is a bug,
    and adding a signal is exactly when it happens."""
    consumers = run.measured_weight_consumers()
    pinned = run.prior_pinned_signals()
    assert consumers & pinned == set(), "a signal cannot be both measured and pinned"
    assert consumers | pinned == set(IC_PRIORS), (
        "every signal must be classified; unclassified: "
        f"{set(IC_PRIORS) - consumers - pinned}"
    )


def test_partition_matches_the_measured_state_of_the_code():
    """The 8/7 split as it stands. This is a characterization test: if it
    changes, the applied weight vector changed and that is a pick-affecting
    decision that must be made deliberately, not noticed later."""
    assert run.measured_weight_consumers() == {
        "momentum_12_1", "reversal_5d", "overnight_drift", "vol_signal",
        "technical_52w", "revision_proxy", "earnings_sue", "pead_drift",
    }
    assert run.prior_pinned_signals() == {
        "short_squeeze", "short_pressure", "analyst_recom", "quality",
        "opt_pc_sentiment", "insider_cluster", "sentiment",
    }


def test_partition_is_derived_from_source_not_restated():
    """Both sets must come from run.py's actual text. A hand-maintained list
    would drift from the composite the day someone edits it -- the failure this
    whole module exists to catch."""
    consumers = run.measured_weight_consumers()
    pinned = run.prior_pinned_signals()
    src = run.Path(run.__file__).read_text(encoding="utf-8")
    for s in pinned:
        assert f'contrib["{s}"]' in src and f'PRIOR_W["{s}"]' in src
    # every non-price consumer must literally read the weights dict
    for s in consumers - set(run.PRICE_SIGNALS):
        assert f'weights.get("{s}"' in src


# ───────────────────────── applied vs published ─────────────────────────

def test_applied_weights_pin_the_frozen_signals_to_prior():
    """A published weight for a pinned signal is not what the composite uses."""
    published = {k: 0.99 for k in IC_PRIORS}
    applied = run.applied_weights(published)
    for s in run.prior_pinned_signals():
        assert applied[s] == pytest.approx(PRIOR_W[s]), (
            f"{s} is pinned; the published 0.99 must not reach the composite"
        )
    for s in run.measured_weight_consumers():
        assert applied[s] == pytest.approx(0.99)


def test_applied_weights_fall_back_the_way_the_composite_does():
    """A consumer absent from the published vector: price signals default to
    0.0 (the PRICE_SIGNALS loop), event signals to PRIOR_W. Those are two
    different fallbacks six lines apart in run.py and both must be reproduced,
    or the audit reports a divergence that does not exist."""
    applied = run.applied_weights({})
    for s in run.PRICE_SIGNALS:
        assert applied[s] == 0.0
    for s in ("revision_proxy", "earnings_sue", "pead_drift"):
        assert applied[s] == pytest.approx(PRIOR_W[s])


def test_applied_weights_covers_every_signal():
    assert set(run.applied_weights({})) == set(IC_PRIORS)


def test_divergence_reports_only_real_mismatches():
    """Positive control on the detector: a published vector that already
    equals the applied one must produce an empty report, and the same detector
    must be able to return non-empty."""
    agreeing = dict(PRIOR_W)
    for s in run.measured_weight_consumers():
        agreeing[s] = PRIOR_W[s]
    assert run.weight_divergences(agreeing) == []

    disagreeing = dict(agreeing)
    pinned = sorted(run.prior_pinned_signals())[0]
    disagreeing[pinned] = PRIOR_W[pinned] + 0.10
    found = run.weight_divergences(disagreeing)
    assert [d["signal"] for d in found] == [pinned]
    assert found[0]["published"] == pytest.approx(PRIOR_W[pinned] + 0.10)
    assert found[0]["applied"] == pytest.approx(PRIOR_W[pinned])


def test_divergence_row_names_the_cause():
    """The audit prints "pinned to literature prior" vs "not in composite" off
    this flag; without it every divergence looks like the same problem."""
    published = dict(PRIOR_W)
    pinned = sorted(run.prior_pinned_signals())[0]
    published[pinned] = PRIOR_W[pinned] + 0.10
    assert run.weight_divergences(published)[0]["pinned"] is True


def test_divergence_tolerates_a_partial_published_vector():
    """Real case, not hypothetical: the universe_scores cache publishes only 10
    of the 15 signals. A signal absent from the published vector has no
    published weight to disagree with, and indexing it blindly raises KeyError
    — which would take the audit down on exactly the input it exists to read."""
    # A consumer alone: whatever it publishes IS what gets applied, so a
    # partial vector of consumers can never diverge -- it must also not raise.
    consumer = sorted(run.measured_weight_consumers())[0]
    assert run.weight_divergences({consumer: 0.42}) == []
    # A pinned signal alone: diverges, and still must not raise on the 14
    # signals absent from the dict.
    pinned = sorted(run.prior_pinned_signals())[0]
    found = run.weight_divergences({pinned: PRIOR_W[pinned] + 0.2})
    assert [d["signal"] for d in found] == [pinned]


def test_divergence_ignores_negligible_float_noise():
    published = dict(PRIOR_W)
    published[sorted(run.prior_pinned_signals())[0]] += 1e-12
    assert run.weight_divergences(published) == []


def test_live_published_weights_diverge_today():
    """Not a hypothetical. The real running_weights.json disagrees with what
    the composite applies, for pinned signals, right now."""
    rw = confidence.build_confidence_matrix(accuracy.signal_stats()).weights
    found = run.weight_divergences(rw)
    assert found, "expected the live measured vector to diverge on pinned signals"
    assert set(d["signal"] for d in found) <= run.prior_pinned_signals()


# ───────────────────── the report must not overclaim ─────────────────────

def _rw(using_measured: bool) -> dict:
    return {
        "generated_at": "2026-08-05", "horizon": "r5",
        "max_obs_per_signal": 20 if using_measured else 12,
        "using_measured": using_measured,
        "weights": {k: v for k, v in PRIOR_W.items()},
        "rows": [
            {"signal": k, "ic_measured": 0.01, "ic_prior": v, "n": 6,
             "lambda_prior": 0.9, "ic_shrunk": v, "hit_rate": 0.5,
             "weight": PRIOR_W[k]}
            for k, v in IC_PRIORS.items()
        ],
    }


def test_measured_section_does_not_claim_all_signals_are_used():
    """The bug this module is named for. Once using_measured flips, the old
    text said 'Used by the next pipeline run.' full stop -- a statement that is
    false for 7 of the 15 rows printed directly above it."""
    text = accuracy._running_weights_section(_rw(using_measured=True))
    n_consumers = len(run.measured_weight_consumers())
    n_pinned = len(run.prior_pinned_signals())
    assert f"Used for {n_consumers} of {len(IC_PRIORS)} signals" in text
    # The exact phrase, not `str(n_pinned) in text`: that weaker form passed
    # against a mutant reporting "0 of 15 pinned", because the digit 7 also
    # occurs in a weight percentage in the table above. An assertion that can
    # be satisfied by unrelated text is not testing the number.
    assert f"{n_pinned} of {len(IC_PRIORS)} signals are pinned" in text


def test_pinned_signals_are_named_in_the_section():
    text = accuracy._running_weights_section(_rw(using_measured=True))
    for s in run.prior_pinned_signals():
        assert s in text


def test_prior_dominated_section_still_says_not_yet_used():
    """The existing, correct behaviour must survive: while using_measured is
    False the whole file is unused and the footer must keep saying so."""
    text = accuracy._running_weights_section(_rw(using_measured=False))
    assert "NOT yet used" in text
    assert str(accuracy.MIN_OBS_FOR_WEIGHTS) in text


def test_prior_dominated_section_also_discloses_the_pinning():
    """A reader who acts on this table before the flip is still misled about
    7 rows -- they will never be used, flip or no flip."""
    text = accuracy._running_weights_section(_rw(using_measured=False))
    assert "pinned" in text.lower()


def test_weights_table_marks_each_row_as_applied_or_pinned():
    table = accuracy._weights_table(_rw(using_measured=True))
    lines = [ln for ln in table.splitlines() if ln.startswith("| ")]
    body = [ln for ln in lines if not ln.startswith("| Signal")]
    assert len(body) == len(IC_PRIORS)
    for ln in body:
        sig = ln.split("|")[1].strip()
        if sig in run.prior_pinned_signals():
            assert "prior" in ln.lower(), f"{sig} row must disclose it is pinned"
        else:
            assert "measured" in ln.lower(), f"{sig} row must say it is applied"


def test_scorecard_renders_with_the_new_column():
    """End-to-end: the vault file Nick reads must still build."""
    text = accuracy.render_scorecard()
    assert "Running weights" in text
    assert "pinned" in text.lower()


# ───────────────────────── the imminent flip ─────────────────────────

def test_flip_is_close_enough_to_warn_about():
    """Guards the fact that motivated this: max-n is near the threshold, so
    the regime change is days away, not months. If this ever fails because
    max_obs raced past the threshold, the flip already happened."""
    rw = accuracy.running_weights(persist=False)
    assert rw["max_obs_per_signal"] <= accuracy.MIN_OBS_FOR_WEIGHTS


def test_icir_tilt_is_unshrunk_and_can_swing_a_weight_eightfold():
    """Documents the mechanism, without changing it. IC is shrunk toward the
    prior by n, then multiplied by an ICIR factor that is NOT shrunk and NOT
    gated on n -- so 6 observations can move a weight by 0.25x..2.0x while the
    same 6 observations move the IC by only ~9%."""
    lo = confidence.build_confidence_matrix(
        {"vol_signal": {"ic": 0.05, "n": 6, "icir": -3.0, "hit_rate": 0.5}})
    hi = confidence.build_confidence_matrix(
        {"vol_signal": {"ic": 0.05, "n": 6, "icir": 3.0, "hit_rate": 0.5}})
    lo_row = next(r for r in lo.rows if r.name == "vol_signal")
    hi_row = next(r for r in hi.rows if r.name == "vol_signal")
    # identical IC and identical n -> identical shrinkage
    assert lo_row.ic_shrunk == pytest.approx(hi_row.ic_shrunk)
    assert lo_row.shrink_lambda == pytest.approx(hi_row.shrink_lambda)
    # but the weights differ by very nearly the full clipped tilt range. Not
    # exactly 8x: the tilt also moves the normalisation denominator, so the
    # realised swing is ~7.45x. Asserting the exact 8.0 would be asserting the
    # basis, not the weight the composite actually receives.
    assert hi_row.weight / lo_row.weight > 7.0


def test_icir_needs_only_two_cross_sections_to_exist():
    """Why the tilt is fragile: np.std of two points is half their range, so
    two same-sign ICs produce an arbitrarily large ICIR."""
    a, b = 0.10, 0.11
    icir = float(np.mean([a, b]) / np.std([a, b]))
    assert icir > 20
    assert float(np.clip(0.5 + 0.5 * icir, 0.25, 2.0)) == 2.0


# ───────────── sibling: the second place priors mix with weights ─────────────

def test_report_enrich_falls_back_to_the_normalised_prior():
    """`confidence.py` states normalised priors MUST be used wherever a prior
    is mixed with measured weights in one composite. run.py's three event-signal
    lines obey it; report.py's identical three used the RAW prior, 1.82x
    smaller. LATENT -- both live weight sources carry all three keys, so the
    fallback is unreachable and correcting it moves no pick today.

    ⚠️ This test GREPS SOURCE; it executes nothing. It cannot tell you the
    fallback behaves correctly, only that the constant named on that line is
    the right one. A behavioural test would have to drive `report.main()`,
    which fetches yfinance end-to-end. Stated rather than implied, because a
    green source-grep reads exactly like a green behavioural test.
    """
    from pathlib import Path
    from src.stockpicker import report
    src = Path(report.__file__).read_text(encoding="utf-8")
    for s in ("revision_proxy", "earnings_sue", "pead_drift"):
        assert f'weights.get("{s}", PRIOR_W["{s}"])' in src
        assert f'weights.get("{s}", IC_PRIORS["{s}"])' not in src


def test_the_two_prior_scales_really_do_differ():
    """Control: if PRIOR_W and IC_PRIORS were the same numbers, the test above
    would be asserting a distinction without a difference."""
    for s in ("revision_proxy", "earnings_sue", "pead_drift"):
        assert PRIOR_W[s] / IC_PRIORS[s] == pytest.approx(1.82, rel=0.02)

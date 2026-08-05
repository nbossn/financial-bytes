#!/usr/bin/env python3
"""weight_application_audit.py — fail when a published weight is never applied.

`running_weights.json` publishes a weight for every signal and SCORECARD.md
prints all of them. The composite in `run.py` reads only some: the rest are
hardcoded to `PRIOR_W` and cannot learn from the accuracy ledger no matter what
it measures. Nothing detected that, because the pipeline's only report of it is
a weight table that looks authoritative.

A log line is not a consumer that can fail. This is.

    exit 0  every published weight is the weight the composite applies
    exit 1  a published weight diverges, or the signal partition is broken
    exit 2  the audit could not run (controls failed / wrong tree)

Run:  python scripts/weight_application_audit.py [--repo /path/to/repo]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _pin_tree(repo: Path) -> None:
    """Import from `repo`, and prove it.

    Run as `python scripts/thing.py`, sys.path[0] is `scripts/`, not the repo
    root — so `import src...` can fall through to whatever a venv .pth points
    at. An audit that does that reports on a tree it is not sitting in, and it
    reports clean. This pins the tree and then checks that the pin took.
    """
    sys.path.insert(0, str(repo))
    import src.stockpicker.run as run  # noqa: F401

    resolved = Path(run.__file__).resolve()
    if repo.resolve() not in resolved.parents:
        print(f"FATAL: pinned {repo}, but src.stockpicker.run resolved to "
              f"{resolved}. The audit would be measuring another tree.")
        raise SystemExit(2)

    # A tree that predates the partition API cannot be audited. Say that
    # plainly — a traceback here reads as "the audit is broken" when the real
    # answer is "this tree has no partition to check".
    missing = [n for n in ("measured_weight_consumers", "prior_pinned_signals",
                           "applied_weights", "weight_divergences")
               if not hasattr(run, n)]
    if missing:
        print(f"FATAL: {resolved} predates this audit — missing {missing}. "
              f"Nothing to measure; this is not a clean result.")
        raise SystemExit(2)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=str(Path(__file__).resolve().parents[1]),
                    help="repository root to audit (default: this script's repo)")
    args = ap.parse_args()
    repo = Path(args.repo).resolve()
    _pin_tree(repo)

    from src.stockpicker import run
    from src.stockpicker.confidence import IC_PRIORS, PRIOR_W

    print(f"[audit] tree: {repo}")

    consumers = run.measured_weight_consumers()
    pinned = run.prior_pinned_signals()

    # ── controls, before any verdict ─────────────────────────────────────
    # A partition read out of source can silently return nothing if the source
    # is reformatted; an empty set would then make every check trivially pass.
    if not consumers or not pinned:
        print(f"FATAL control: derived partition is empty "
              f"(consumers={len(consumers)}, pinned={len(pinned)}). "
              f"run.py's contrib lines probably changed shape.")
        return 2
    # The detector must be able to say yes AND no.
    if run.weight_divergences({k: PRIOR_W[k] for k in pinned}):
        print("FATAL control: an agreeing vector reported a divergence.")
        return 2
    probe = dict(PRIOR_W)
    probe[sorted(pinned)[0]] += 0.5
    if not run.weight_divergences(probe):
        print("FATAL control: a deliberately wrong vector reported clean.")
        return 2
    print(f"[audit] controls ok — detector fires on a planted divergence and "
          f"stays quiet on an agreeing vector")

    rc = 0

    # ── the partition must stay total and disjoint ───────────────────────
    both = consumers & pinned
    neither = set(IC_PRIORS) - consumers - pinned
    if both:
        print(f"BROKEN PARTITION: signals both measured and pinned: {sorted(both)}")
        rc = 1
    if neither:
        print(f"BROKEN PARTITION: signals wired neither way (they carry a "
              f"published weight and can never move a pick): {sorted(neither)}")
        rc = 1

    print(f"[audit] {len(consumers)} of {len(IC_PRIORS)} signals consume the "
          f"measured weight vector; {len(pinned)} are pinned to prior")

    # ── published vs applied ─────────────────────────────────────────────
    wpath = repo / "data" / "stockpicker" / "running_weights.json"
    if not wpath.exists():
        print(f"[audit] no {wpath.name} yet — nothing published to diverge")
        return rc

    rw = json.loads(wpath.read_text())
    published = rw.get("weights") or {}
    if not published:
        print(f"FATAL control: {wpath.name} has no weights block.")
        return 2

    div = run.weight_divergences(published)
    if div:
        rc = 1
        print(f"\nDIVERGENCE: {len(div)} published weight(s) never reach a pick")
        print(f"{'signal':<20}{'published':>11}{'applied':>10}   why")
        for d in div:
            why = "pinned to literature prior" if d["pinned"] else "not in composite"
            print(f"{d['signal']:<20}{d['published']*100:>10.2f}%"
                  f"{d['applied']*100:>9.2f}%   {why}")
    else:
        print("[audit] every published weight is applied as published")

    # ── the regime change, announced before it happens ───────────────────
    from src.stockpicker.accuracy import MIN_OBS_FOR_WEIGHTS
    obs = int(rw.get("max_obs_per_signal", 0))
    if not rw.get("using_measured") and obs >= MIN_OBS_FOR_WEIGHTS - 3:
        print(f"\nIMMINENT: max_obs_per_signal={obs}, threshold="
              f"{MIN_OBS_FOR_WEIGHTS}. When it crosses, the pipeline swaps the "
              f"universe_scores cache for this vector in one step, for the "
              f"{len(consumers)} signals that consume it.")

    return rc


if __name__ == "__main__":
    raise SystemExit(main())

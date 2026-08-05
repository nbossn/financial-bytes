#!/usr/bin/env python3
"""Mutation harness for the weight-application partition.

Each mutation must (a) actually apply — a replacement whose anchor is absent
silently mutates nothing and then reports "all tests pass", which reads exactly
like a surviving mutant — and (b) be caught by tests/test_weight_application.py.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
RUN = REPO / "src" / "stockpicker" / "run.py"
ACC = REPO / "src" / "stockpicker" / "accuracy.py"

MUTATIONS = [
    # (file, old, new, label)
    (RUN, 'out[name] = PRIOR_W[name]', 'out[name] = float(published.get(name, PRIOR_W[name]))',
     "M01 pinned signals honour the published weight"),
    (RUN, 'out[name] = float(published.get(name, 0.0))',
     'out[name] = float(published.get(name, PRIOR_W[name]))',
     "M02 price-signal fallback becomes PRIOR_W instead of 0.0"),
    (RUN, 'out[name] = float(published.get(name, PRIOR_W[name]))\n    return out',
     'out[name] = float(published.get(name, 0.0))\n    return out',
     "M03 event-signal fallback becomes 0.0 instead of PRIOR_W"),
    (RUN, 'def weight_divergences(published: dict, tol: float = 1e-9)',
     'def weight_divergences(published: dict, tol: float = 1e9)',
     "M04 divergence tolerance so wide nothing ever reports"),
    (RUN, 'if abs(pub - applied[name]) > tol:', 'if abs(pub - applied[name]) < tol:',
     "M05 divergence comparison inverted"),
    (RUN, 'return set(PRICE_SIGNALS) | set(\n        re.findall(r\'contrib\\[["\\\']([a-z0-9_]+)["\\\']\\]\\s*=\\s*weights\\.get\', src))',
     'return set(\n        re.findall(r\'contrib\\[["\\\']([a-z0-9_]+)["\\\']\\]\\s*=\\s*weights\\.get\', src))',
     "M06 consumers drop the PRICE_SIGNALS loop"),
    (RUN, 're.findall(r\'contrib\\[["\\\']([a-z0-9_]+)["\\\']\\]\\s*=\\s*PRIOR_W\\[\', src))',
     're.findall(r\'contrib\\[["\\\']([a-z0-9_]+)["\\\']\\]\\s*=\\s*NOPE_W\\[\', src))',
     "M07 pinned-signal detection returns empty"),
    (RUN, 'if name not in published:\n            continue', 'if False:\n            continue',
     "M08 divergence no longer skips unpublished signals"),
    (ACC, 'mark = ("**no — pinned to prior**" if r["signal"] in pinned\n                else "yes — measured")',
     'mark = "yes — measured"',
     "M09 weights table marks every row as applied"),
    (ACC, 'used += (f" **{len(pinned)} of {n_total} signals are pinned to their "',
     'used += (f" " f"" f"**{0} of {n_total} signals are pinned to their "',
     "M10 pinned count reported as zero"),
    (ACC, 'f"Used for {n_used} of {n_total} signals by the next pipeline "',
     'f"Used for {n_total} of {n_total} signals by the next pipeline "',
     "M11 claims all signals consume the vector"),
    (RUN, 'pinned = prior_pinned_signals()\n    out: dict[str, float] = {}',
     'pinned = set()\n    out: dict[str, float] = {}',
     "M12 applied_weights forgets the pinned set"),
    (ACC, 'if r["signal"] in pinned', 'if r["signal"] not in pinned',
     "M13 applied/pinned marker inverted"),
    (RUN, '"pinned": name in prior_pinned_signals()', '"pinned": False',
     "M14 divergence rows never report the pinning cause"),
]


def run_tests() -> bool:
    r = subprocess.run(
        [str(REPO / ".venv" / "bin" / "python"), "-m", "pytest",
         "tests/test_weight_application.py", "-q", "--no-cov", "-x"],
        cwd=REPO, capture_output=True, text=True)
    return r.returncode == 0


def main() -> int:
    originals = {p: p.read_text() for p in {RUN, ACC}}
    caught = survived = refused = 0
    try:
        for path, old, new, label in MUTATIONS:
            src = originals[path]
            if old not in src:
                print(f"REFUSED  {label} — anchor not found, mutation is a no-op")
                refused += 1
                continue
            mutated = src.replace(old, new, 1)
            if mutated == src:
                print(f"REFUSED  {label} — replacement changed nothing")
                refused += 1
                continue
            path.write_text(mutated)
            try:
                ok = run_tests()
            finally:
                path.write_text(src)
            if ok:
                print(f"SURVIVED {label}")
                survived += 1
            else:
                print(f"caught   {label}")
                caught += 1
    finally:
        for p, s in originals.items():
            p.write_text(s)
    print(f"\n{caught} caught · {survived} survived · {refused} refused "
          f"(of {len(MUTATIONS)})")
    return 1 if (survived or refused) else 0


if __name__ == "__main__":
    raise SystemExit(main())

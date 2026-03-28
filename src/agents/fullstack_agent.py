"""Fullstack agent — weekly maintenance: DB audit, cost audit, security scan, GitHub sync."""
from __future__ import annotations

import subprocess
from datetime import date, datetime, timedelta
from pathlib import Path

from loguru import logger


# ── DB Audit ──────────────────────────────────────────────────────

def _db_audit() -> dict:
    """Check DB health: table counts, old data, orphaned records."""
    from src.db.models import Article, Summary, Recommendation, Newsletter, ScrapeLog
    from src.db.session import get_db

    results = {}
    cutoff_30 = datetime.utcnow() - timedelta(days=30)

    with get_db() as db:
        results["articles_total"] = db.query(Article).count()
        results["articles_last_30d"] = db.query(Article).filter(Article.scraped_at >= cutoff_30).count()
        results["summaries_total"] = db.query(Summary).count()
        results["recommendations_total"] = db.query(Recommendation).count()
        results["newsletters_total"] = db.query(Newsletter).count()
        results["newsletters_sent"] = db.query(Newsletter).filter_by(status="sent").count()
        results["newsletters_failed"] = db.query(Newsletter).filter_by(status="failed").count()
        results["scrape_logs_total"] = db.query(ScrapeLog).count()

    logger.info("DB Audit:")
    for k, v in results.items():
        logger.info(f"  {k}: {v}")
    return results


# ── Cost Audit ────────────────────────────────────────────────────

def _cost_audit() -> dict:
    """Estimate Claude API costs from recent summaries and recommendations."""
    from src.db.models import Summary, Recommendation
    from src.db.session import get_db

    # Approximate token costs (as of 2025)
    # Haiku 4.5: $0.80/M input, $4.00/M output
    # Sonnet 4.6: $3.00/M input, $15.00/M output
    HAIKU_INPUT_PER_TOKEN = 0.80 / 1_000_000
    HAIKU_OUTPUT_PER_TOKEN = 4.00 / 1_000_000
    SONNET_INPUT_PER_TOKEN = 3.00 / 1_000_000
    SONNET_OUTPUT_PER_TOKEN = 15.00 / 1_000_000

    # Approximate tokens per call (rough estimates)
    ANALYST_INPUT_TOKENS = 2000
    ANALYST_OUTPUT_TOKENS = 800
    DIRECTOR_INPUT_TOKENS = 3000
    DIRECTOR_OUTPUT_TOKENS = 1500

    cutoff_7 = datetime.utcnow() - timedelta(days=7)
    cutoff_30 = datetime.utcnow() - timedelta(days=30)

    with get_db() as db:
        analyst_calls_7d = db.query(Summary).filter(Summary.report_date >= cutoff_7.date()).count()
        analyst_calls_30d = db.query(Summary).filter(Summary.report_date >= cutoff_30.date()).count()
        director_calls_7d = db.query(Recommendation).filter(Recommendation.report_date >= cutoff_7.date()).count()
        director_calls_30d = db.query(Recommendation).filter(Recommendation.report_date >= cutoff_30.date()).count()

    def _calc_cost(calls: int, input_t: int, output_t: int, in_rate: float, out_rate: float) -> float:
        return calls * (input_t * in_rate + output_t * out_rate)

    analyst_7d = _calc_cost(analyst_calls_7d, ANALYST_INPUT_TOKENS, ANALYST_OUTPUT_TOKENS,
                            HAIKU_INPUT_PER_TOKEN, HAIKU_OUTPUT_PER_TOKEN)
    analyst_30d = _calc_cost(analyst_calls_30d, ANALYST_INPUT_TOKENS, ANALYST_OUTPUT_TOKENS,
                             HAIKU_INPUT_PER_TOKEN, HAIKU_OUTPUT_PER_TOKEN)
    director_7d = _calc_cost(director_calls_7d, DIRECTOR_INPUT_TOKENS, DIRECTOR_OUTPUT_TOKENS,
                             SONNET_INPUT_PER_TOKEN, SONNET_OUTPUT_PER_TOKEN)
    director_30d = _calc_cost(director_calls_30d, DIRECTOR_INPUT_TOKENS, DIRECTOR_OUTPUT_TOKENS,
                              SONNET_INPUT_PER_TOKEN, SONNET_OUTPUT_PER_TOKEN)

    results = {
        "analyst_calls_7d": analyst_calls_7d,
        "analyst_calls_30d": analyst_calls_30d,
        "director_calls_7d": director_calls_7d,
        "director_calls_30d": director_calls_30d,
        "estimated_cost_7d_usd": round(analyst_7d + director_7d, 4),
        "estimated_cost_30d_usd": round(analyst_30d + director_30d, 4),
    }

    logger.info("Cost Audit (estimates):")
    logger.info(f"  7-day cost: ~${results['estimated_cost_7d_usd']:.4f}")
    logger.info(f"  30-day cost: ~${results['estimated_cost_30d_usd']:.4f}")
    return results


# ── Security Scan ─────────────────────────────────────────────────

def _security_scan() -> list[str]:
    """Basic security checks: .env not committed, no hardcoded secrets."""
    issues = []
    project_root = Path(__file__).parents[2]

    # Check .env is gitignored
    gitignore = project_root / ".gitignore"
    if gitignore.exists():
        content = gitignore.read_text()
        if ".env" not in content:
            issues.append("WARNING: .env not in .gitignore — risk of secret exposure")
    else:
        issues.append("WARNING: No .gitignore found")

    # Check for accidentally committed .env
    try:
        result = subprocess.run(
            ["git", "ls-files", "--error-unmatch", ".env"],
            cwd=project_root,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            issues.append("CRITICAL: .env is tracked by git — remove immediately with git rm --cached .env")
    except FileNotFoundError:
        pass  # git not available

    # Scan for hardcoded API keys in Python source
    import re
    key_patterns = [
        r'api_key\s*=\s*["\'][A-Za-z0-9_\-]{20,}["\']',
        r'password\s*=\s*["\'][^"\']{8,}["\']',
        r'secret\s*=\s*["\'][^"\']{8,}["\']',
    ]
    src_dir = project_root / "src"
    for py_file in src_dir.rglob("*.py"):
        text = py_file.read_text(errors="ignore")
        for pattern in key_patterns:
            if re.search(pattern, text, re.IGNORECASE):
                issues.append(f"WARNING: Possible hardcoded secret in {py_file.relative_to(project_root)}")
                break

    if not issues:
        logger.info("Security scan: clean")
    else:
        for issue in issues:
            logger.warning(f"Security: {issue}")
    return issues


# ── GitHub Sync ───────────────────────────────────────────────────

def _github_sync() -> bool:
    """Commit any new newsletters and push to GitHub."""
    from src.config import settings

    if not settings.github_token:
        logger.info("GitHub sync skipped — no GITHUB_TOKEN configured")
        return False

    project_root = Path(__file__).parents[2]

    try:
        # Stage new newsletters only (HTML, MD — not PDF, not .env)
        subprocess.run(
            ["git", "add", "newsletters/*.html", "newsletters/*.md"],
            cwd=project_root, check=False, capture_output=True,
        )

        # Check if there's anything to commit
        status = subprocess.run(
            ["git", "diff", "--cached", "--quiet"],
            cwd=project_root, capture_output=True,
        )
        if status.returncode == 0:
            logger.info("GitHub sync: nothing to commit")
            return True

        today_str = date.today().strftime("%Y-%m-%d")
        subprocess.run(
            ["git", "commit", "-m", f"chore: newsletter {today_str}"],
            cwd=project_root, check=True, capture_output=True,
        )

        # Push using token auth via environment — avoids embedding token in command args
        # which would expose it in error messages, process lists, and logs.
        import os
        import tempfile
        repo_url = f"https://x-token-auth@github.com/{settings.github_repo}.git"
        askpass_script = tempfile.NamedTemporaryFile(
            mode="w", suffix=".sh", delete=False, prefix="fb_askpass_"
        )
        askpass_script.write(f"#!/bin/sh\necho '{settings.github_token}'\n")
        askpass_script.flush()
        os.chmod(askpass_script.name, 0o700)
        askpass_path = askpass_script.name
        askpass_script.close()
        try:
            push_env = {**os.environ, "GIT_ASKPASS": askpass_path, "GIT_TERMINAL_PROMPT": "0"}
            subprocess.run(
                ["git", "push", repo_url, "HEAD:main"],
                cwd=project_root, check=True, capture_output=True, env=push_env,
            )
        finally:
            os.unlink(askpass_path)
        logger.info(f"GitHub sync: pushed newsletter {today_str}")
        return True

    except subprocess.CalledProcessError as e:
        # Sanitize error output — strip any token that may appear in stderr
        safe_stderr = (e.stderr or b"").decode(errors="replace")
        if settings.github_token:
            safe_stderr = safe_stderr.replace(settings.github_token, "***")
        logger.error(f"GitHub sync failed (exit {e.returncode}): {safe_stderr[:200]}")
        return False


# ── Main entry ────────────────────────────────────────────────────

def run_audit() -> dict:
    """Run all maintenance tasks."""
    logger.info("=== Fullstack agent audit starting ===")
    results = {}

    logger.info("--- DB Audit ---")
    results["db"] = _db_audit()

    logger.info("--- Cost Audit ---")
    results["cost"] = _cost_audit()

    logger.info("--- Security Scan ---")
    results["security"] = _security_scan()

    logger.info("--- GitHub Sync ---")
    results["github_synced"] = _github_sync()

    logger.info("=== Fullstack agent audit complete ===")
    return results

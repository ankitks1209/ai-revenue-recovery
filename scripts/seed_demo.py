#!/usr/bin/env python3
"""Seed deterministic demo DBs via FullBatchReplay — demo DBs only."""
from __future__ import annotations

import argparse
import pathlib
import sys

# Minimal repository-root bootstrap for direct invocation:
# `python scripts/seed_demo.py --confirm` must work from repo root without PYTHONPATH.
_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


DEMO_PAYMENTS_URL = "sqlite:///demo_failed_payments.db"
DEMO_AUDIT_URL = "sqlite:///demo_audit_log.db"


def _is_demo_url(url: str, kind: str) -> bool:
    if kind == "payments":
        return url == DEMO_PAYMENTS_URL
    if kind == "audit":
        return url == DEMO_AUDIT_URL
    return False


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed demo DBs (demo_* only, requires --confirm)")
    parser.add_argument("--seed", type=int, default=42, help="Deterministic seed (default 42)")
    parser.add_argument("--count", type=int, default=60, help="Record count >=50 (default 60)")
    parser.add_argument("--confirm", action="store_true", help="Required to actually write demo DBs")
    parser.add_argument("--payments-db-url", dest="payments_db_url", default=DEMO_PAYMENTS_URL, help="Demo payments DB URL")
    parser.add_argument("--audit-db-url", dest="audit_db_url", default=DEMO_AUDIT_URL, help="Demo audit DB URL")
    args = parser.parse_args()

    if not args.confirm:
        parser.print_usage(sys.stderr)
        print("ERROR: --confirm is required before writing demo DBs.", file=sys.stderr)
        sys.exit(2)

    if args.count < 50:
        print(f"ERROR: count must be >=50, got {args.count}", file=sys.stderr)
        sys.exit(2)

    if not _is_demo_url(args.payments_db_url, "payments"):
        print(f"ERROR: payments-db-url must target demo_failed_payments.db, got {args.payments_db_url}", file=sys.stderr)
        sys.exit(2)
    if not _is_demo_url(args.audit_db_url, "audit"):
        print(f"ERROR: audit-db-url must target demo_audit_log.db, got {args.audit_db_url}", file=sys.stderr)
        sys.exit(2)

    # Only import here so --help / validation never touches DB
    from src.application.full_batch_replay import FullBatchReplay

    try:
        replay = FullBatchReplay(
            seed=args.seed,
            count=args.count,
            payments_db_url=args.payments_db_url,
            audit_db_url=args.audit_db_url,
        )
        fp = replay.execute()
    except Exception as exc:
        print(f"ERROR: demo seeding failed: {exc}", file=sys.stderr)
        sys.exit(1)

    # Print fingerprint for verification / CI
    print(fp)
    # Also print deterministic fields explicitly for easy grep
    print(f"seed={fp.seed} batch_size={fp.batch_size} total_processed={fp.total_processed}")
    print(f"money_recovered={fp.money_recovered} recoverable_denominator={fp.recoverable_denominator}")
    print(f"audit_event_count={fp.audit_event_count} tier_counts={fp.tier_counts} outcome_counts={fp.outcome_counts}")


if __name__ == "__main__":
    main()

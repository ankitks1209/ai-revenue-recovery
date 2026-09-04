import argparse
import os
from pathlib import Path
from src.config import RANDOM_SEED
from src.database import engine, SessionLocal, FailedPayment
from src.ingest import load_failed_payments_to_db
from src.classifier import FailureClassifier
from src.policy_engine import PolicyEngine
from src.application.execute_recovery_batch import ExecuteRecoveryBatch
from src.application.generate_recovery_report import GenerateRecoveryReport
from src.infrastructure.repository import SQLiteFailedPaymentRepository
from src.infrastructure.mock_payment_rail import MockPaymentRail
from src.infrastructure.clock import SystemClock

def is_db_initialized() -> bool:
    """
    Checks if the SQLite database is initialized and has records in the failed_payments table.
    """
    from sqlalchemy import inspect
    inspector = inspect(engine)
    if not inspector.has_table("failed_payments"):
        return False
    session = SessionLocal()
    try:
        count = session.query(FailedPayment).count()
        return count > 0
    except Exception:
        return False
    finally:
        session.close()

def main():
    parser = argparse.ArgumentParser(description="AI Revenue Recovery Agent CLI")
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Explicitly reset and re-seed the SQLite database. WARNING: This will destroy existing records!"
    )
    parser.add_argument(
        "--count",
        type=int,
        default=60,
        help="Number of failed payments to generate if seeding is triggered (default: 60)"
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=RANDOM_SEED,
        help=f"Random seed for data generation (default: {RANDOM_SEED})"
    )
    args = parser.parse_args()

    print("==================================================")
    print("        AI REVENUE RECOVERY AGENT CLI (v1)")
    print("==================================================")

    # 1. Database safety and initialization
    db_ready = is_db_initialized()
    if args.reset:
        print("\n[Safe-Guard] '--reset' flag provided. Dropping tables and seeding fresh demo dataset...")
        load_failed_payments_to_db(count=args.count, seed=args.seed)
    elif not db_ready:
        print("\n[Safe-Guard] Database is uninitialized or empty. Seeding a fresh demo dataset...")
        load_failed_payments_to_db(count=args.count, seed=args.seed)
    else:
        print("\n[Safe-Guard] Existing database found with records. Processing existing dataset.")
        print("             (To seed a fresh dataset, run with the '--reset' flag.)")

    # 2. Phase 1 Classification and Policy
    print("\n[Phase 1] Running Failure Classifier & Intervention Policy Engine...")
    session = SessionLocal()
    try:
        db_records = session.query(FailedPayment).all()
        records_list = [
            {
                "txn_id": r.txn_id,
                "customer_id": r.customer_id,
                "amount": r.amount,
                "currency": r.currency,
                "failure_code": r.failure_code,
                "root_cause_label": r.root_cause_label,
                "recoverable_flag": r.recoverable_flag,
                "retry_count": r.retry_count,
                "timestamp": r.timestamp,
                "payment_method": r.payment_method
            }
            for r in db_records
        ]
    finally:
        session.close()

    classifier = FailureClassifier()
    eval_result = classifier.evaluate_batch(records_list)
    classified_df = eval_result["dataframe"]

    print("\n[Phase 1] Classifier Performance vs Ground Truth:")
    print(eval_result["report_str"])

    policy_engine = PolicyEngine()
    policy_df = policy_engine.apply_policy(classified_df)

    print("\n[Phase 1] Decision Preview (Sample of 10 records):")
    preview_cols = ["txn_id", "failure_code", "predicted_category", "chosen_action", "bounds"]
    print(policy_df[preview_cols].head(10).to_string(index=False))

    summary = policy_engine.generate_summary(policy_df)
    print("\n==================================================")
    print(" PHASE 1 SUMMARY AGGREGATES")
    print("==================================================")
    print("Records per Root-Cause Category:")
    for cat, cnt in summary["category_counts"].items():
        print(f"  - {cat}: {cnt}")

    print("\nRecords per Intervention Action:")
    for act, cnt in summary["action_counts"].items():
        print(f"  - {act}: {cnt}")

    print(f"\nTotal Hard-Stop / Escalate-to-Human Review Records: {summary['escalation_count']}")
    print("==================================================")

    # 3. Phase 2 Bounded Recovery Execution
    print("\n[Phase 2] Running Bounded Recovery Execution Batch...")
    repo = SQLiteFailedPaymentRepository()
    rail = MockPaymentRail(seed=args.seed)
    clock = SystemClock()

    executor = ExecuteRecoveryBatch(
        repository=repo,
        payment_rail=rail,
        clock=clock,
        policy_engine=policy_engine
    )
    exec_stats = executor.execute()

    print("\n[Phase 2] Execution Summary:")
    print(f"  - Total Processed     : {exec_stats['total_processed']}")
    print(f"  - Executed Rail Retries: {exec_stats['executed_count']}")
    print(f"  - Successful Recoveries: {exec_stats['success_count']}")
    print(f"  - Failed Retries       : {exec_stats['failed_count']}")
    print(f"  - Skipped (Backoff)    : {exec_stats['skipped_count']}")
    print(f"  - Escalated to Human   : {exec_stats['escalated_count']}")
    print("==================================================")

    # 4. Phase 2 Reporting v1 Output
    print("\n[Phase 2] Generating Recovery Report v1...")
    report_gen = GenerateRecoveryReport(repository=repo)
    report_data = report_gen.generate_report()

    print("\n" + report_gen.format_cli_report(report_data))

if __name__ == "__main__":
    main()

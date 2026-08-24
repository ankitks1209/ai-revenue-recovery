from src.ingest import load_failed_payments_to_db
from src.classifier import FailureClassifier
from src.policy_engine import PolicyEngine
from src.database import SessionLocal, FailedPayment
from src.config import RANDOM_SEED

def run_phase1_pipeline(count: int = 60, seed: int = RANDOM_SEED):
    print("==================================================")
    print(" AI REVENUE RECOVERY AGENT - PHASE 1 PIPELINE")
    print("==================================================")

    # 1. Generate & Load Synthetic Data (Day 1)
    print("\n[Step 1/4] Generating & Loading Synthetic Failed Payments...")
    load_failed_payments_to_db(count=count, seed=seed)

    # 2. Read from Database & Classify (Day 2)
    print("\n[Step 2/4] Running Failure Classifier / Detector...")
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

    print("\nClassifier Performance vs Ground Truth:")
    print(eval_result["report_str"])

    # 3. Apply Intervention Policy (Day 3)
    print("\n[Step 3/4] Applying Intervention Policy Table...")
    policy_engine = PolicyEngine()
    policy_df = policy_engine.apply_policy(classified_df)

    # 4. Produce Phase 1 Annotated Output Preview & Summaries (Day 3)
    print("\n[Step 4/4] Phase 1 Decision Preview (Sample of 10 records):")
    preview_cols = ["txn_id", "failure_code", "predicted_category", "chosen_action", "bounds"]
    print(policy_df[preview_cols].head(10).to_string(index=False))

    summary = policy_engine.generate_summary(policy_df)
    print("\n==================================================")
    print(" PHASE 1 SUMMARY AGGREGATES")
    print("==================================================")
    print("\nRecords per Root-Cause Category:")
    for cat, cnt in summary["category_counts"].items():
        print(f"  - {cat}: {cnt}")

    print("\nRecords per Intervention Action:")
    for act, cnt in summary["action_counts"].items():
        print(f"  - {act}: {cnt}")

    print(f"\nTotal Hard-Stop / Escalate-to-Human Review Records: {summary['escalation_count']}")
    print("==================================================")
    print(" PHASE 1 COMPLETE - NO RETRIES EXECUTED (PER SCOPE)")
    print("==================================================")

if __name__ == "__main__":
    run_phase1_pipeline()

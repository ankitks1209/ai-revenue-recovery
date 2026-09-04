import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from src.database import Base, FailedPayment as FailedPaymentModel
from src.generator import generate_failed_payments
from src.classifier import FailureClassifier
from src.policy_engine import PolicyEngine
from src.application.execute_recovery_batch import ExecuteRecoveryBatch
from src.application.generate_recovery_report import GenerateRecoveryReport
from src.infrastructure.repository import SQLiteFailedPaymentRepository
from src.infrastructure.mock_payment_rail import MockPaymentRail
from src.infrastructure.clock import SystemClock


@pytest.fixture(scope="function")
def isolated_e2e_db(tmp_path, monkeypatch):
    """
    Create an isolated temporary SQLite database for the E2E test.

    The real development database is never used by this test.
    """
    db_file = tmp_path / "e2e_test.db"
    db_url = f"sqlite:///{db_file}"

    test_engine = create_engine(db_url, echo=False)
    TestSessionLocal = sessionmaker(
        bind=test_engine,
        autoflush=False,
        autocommit=False,
    )

    Base.metadata.create_all(bind=test_engine)

    # Patch the database module so components that import these
    # references use the isolated test database.
    import src.database

    monkeypatch.setattr(src.database, "engine", test_engine)
    monkeypatch.setattr(src.database, "SessionLocal", TestSessionLocal)

    try:
        yield test_engine, TestSessionLocal
    finally:
        test_engine.dispose()


def test_full_pipeline_e2e_reconciliation(isolated_e2e_db):
    test_engine, TestSessionLocal = isolated_e2e_db

    # ============================================================
    # 1. PHASE 1 — GENERATE + PERSIST 60 SYNTHETIC RECORDS
    # ============================================================
    generated_records = generate_failed_payments(
        count=60,
        seed=42,
    )

    session = TestSessionLocal()
    try:
        session.add_all(
            [
                FailedPaymentModel(**record)
                for record in generated_records
            ]
        )
        session.commit()
    finally:
        session.close()

    # Verify Phase 1 persistence.
    session = TestSessionLocal()
    try:
        db_count = session.query(FailedPaymentModel).count()

        assert db_count == 60, (
            "Database should contain exactly 60 generated payments."
        )

        records_list = [
            {
                "txn_id": record.txn_id,
                "customer_id": record.customer_id,
                "amount": record.amount,
                "currency": record.currency,
                "failure_code": record.failure_code,
                "root_cause_label": record.root_cause_label,
                "recoverable_flag": record.recoverable_flag,
                "retry_count": record.retry_count,
                "timestamp": record.timestamp,
                "payment_method": record.payment_method,
            }
            for record in session.query(FailedPaymentModel).all()
        ]
    finally:
        session.close()

    # ============================================================
    # 2. PHASE 1 — CLASSIFICATION + POLICY
    # ============================================================
    classifier = FailureClassifier()

    eval_result = classifier.evaluate_batch(records_list)
    classified_df = eval_result["dataframe"]

    assert len(classified_df) == 60

    policy_engine = PolicyEngine()

    policy_df = policy_engine.apply_policy(classified_df)

    assert len(policy_df) == 60

    # ============================================================
    # 3. PHASE 2 — BOUNDED RECOVERY EXECUTION
    # ============================================================
    repo = SQLiteFailedPaymentRepository(
        session_factory=TestSessionLocal
    )

    rail = MockPaymentRail(seed=42)
    clock = SystemClock()

    executor = ExecuteRecoveryBatch(
        repository=repo,
        payment_rail=rail,
        clock=clock,
        policy_engine=policy_engine,
    )

    exec_stats = executor.execute()

    assert exec_stats["total_processed"] == 60
    assert exec_stats["executed_count"] > 0
    assert exec_stats["success_count"] > 0

    # ============================================================
    # 4. PHASE 2 — REPORT GENERATION
    # ============================================================
    report_gen = GenerateRecoveryReport(repository=repo)

    report_data = report_gen.generate_report()

    assert report_data["total_processed"] == 60
    assert "recoverable_denominator" in report_data
    assert "money_recovered" in report_data
    assert "recovery_rate" in report_data
    assert "escalation_count" in report_data
    assert "intervention_breakdown" in report_data
    assert "exception_list" in report_data

    # ============================================================
    # 5. INDEPENDENT SQL RECONCILIATION
    # ============================================================
    with test_engine.connect() as conn:

        # --------------------------------------------------------
        # A. Recoverable denominator
        # --------------------------------------------------------
        result = conn.execute(
            text(
                """
                SELECT SUM(amount)
                FROM failed_payments
                WHERE recoverable_flag = 1
                """
            )
        )

        sql_denom = result.scalar() or 0.0

        assert abs(
            report_data["recoverable_denominator"] - sql_denom
        ) < 1e-2

        # --------------------------------------------------------
        # B. Money recovered
        #    Deduplicated by unique txn_id
        # --------------------------------------------------------
        result = conn.execute(
            text(
                """
                SELECT SUM(amount)
                FROM failed_payments
                WHERE txn_id IN (
                    SELECT DISTINCT txn_id
                    FROM recovery_attempts
                    WHERE outcome = 'SUCCESS'
                )
                """
            )
        )

        sql_recovered = result.scalar() or 0.0

        assert abs(
            report_data["money_recovered"] - sql_recovered
        ) < 1e-2

        # --------------------------------------------------------
        # C. Recovery rate
        # --------------------------------------------------------
        expected_rate = (
            round((sql_recovered / sql_denom) * 100.0, 2)
            if sql_denom > 0
            else 0.0
        )

        assert abs(
            report_data["recovery_rate"] - expected_rate
        ) < 1e-2

        # --------------------------------------------------------
        # D. Unique escalation count
        # --------------------------------------------------------
        result = conn.execute(
            text(
                """
                SELECT COUNT(DISTINCT txn_id)
                FROM escalations
                """
            )
        )

        sql_escalation_count = result.scalar() or 0

        assert (
            report_data["escalation_count"]
            == sql_escalation_count
        )

        # --------------------------------------------------------
        # E. Executed attempt count
        # --------------------------------------------------------
        result = conn.execute(
            text(
                """
                SELECT COUNT(*)
                FROM recovery_attempts
                WHERE outcome IN ('SUCCESS', 'FAILED')
                """
            )
        )

        sql_executed_attempts = result.scalar() or 0

        report_executed_attempts = sum(
            category_data["executed_attempts"]
            for category_data
            in report_data["intervention_breakdown"].values()
        )

        assert (
            report_executed_attempts
            == sql_executed_attempts
        )

        # --------------------------------------------------------
        # F. Exception list entries must not be recovered
        # --------------------------------------------------------
        exception_txn_ids = {
            exception["txn_id"]
            for exception in report_data["exception_list"]
        }

        for txn_id in exception_txn_ids:
            result = conn.execute(
                text(
                    """
                    SELECT COUNT(*)
                    FROM recovery_attempts
                    WHERE txn_id = :txn_id
                      AND outcome = 'SUCCESS'
                    """
                ),
                {"txn_id": txn_id},
            )

            assert result.scalar() == 0, (
                f"Exception list contains recovered transaction "
                f"{txn_id}."
            )

        # --------------------------------------------------------
        # G. Exception list completeness
        # --------------------------------------------------------
        result = conn.execute(
            text(
                """
                SELECT COUNT(*)
                FROM failed_payments
                WHERE txn_id NOT IN (
                    SELECT DISTINCT txn_id
                    FROM recovery_attempts
                    WHERE outcome = 'SUCCESS'
                )
                """
            )
        )

        sql_unrecovered_count = result.scalar() or 0

        assert (
            len(report_data["exception_list"])
            == sql_unrecovered_count
        )

    # ============================================================
    # 6. CLI FORMATTER VERIFICATION
    # ============================================================
    cli_output = report_gen.format_cli_report(report_data)

    assert "AI REVENUE RECOVERY REPORT" in cli_output

    assert (
        f"Total Unique Escalations  : {sql_escalation_count}"
        in cli_output
    )
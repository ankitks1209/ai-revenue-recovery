from typing import Dict, Any, List, Optional
from src.infrastructure.ports import FailedPaymentRepositoryPort
from src.infrastructure.repository import SQLiteFailedPaymentRepository
from src.domain.models import Outcome

class GenerateRecoveryReport:
    def __init__(self, repository: Optional[FailedPaymentRepositoryPort] = None):
        self.repository = repository or SQLiteFailedPaymentRepository()

    def generate_report(self) -> Dict[str, Any]:
        payments = self.repository.get_all_payments()

        total_processed = len(payments)
        total_at_risk = sum(p.amount for p in payments)
        recoverable_denominator = sum(p.amount for p in payments if p.recoverable_flag)
        unrecoverable_amount = sum(p.amount for p in payments if not p.recoverable_flag)

        # Money recovered: sum of amount for unique txn_id with at least one SUCCESS attempt
        recovered_payments = [
            p for p in payments
            if any(att.outcome == Outcome.SUCCESS for att in p.attempts)
        ]
        money_recovered = sum(p.amount for p in recovered_payments)

        # Recovery rate: safely handle zero denominator
        if recoverable_denominator > 0:
            recovery_rate = round((money_recovered / recoverable_denominator) * 100.0, 2)
        else:
            recovery_rate = 0.0

        # Escalation count: UNIQUE payment records routed to human review (count(distinct txn_id))
        escalated_payments = [p for p in payments if len(p.escalations) > 0]
        escalation_count = len(escalated_payments)

        # Intervention breakdown per root_cause_label
        intervention_breakdown: Dict[str, Dict[str, Any]] = {}
        categories = sorted(list(set(p.root_cause_label for p in payments)))

        for category in categories:
            cat_payments = [p for p in payments if p.root_cause_label == category]

            # Executed attempts = SUCCESS + FAILED only (excludes SKIPPED and pre-rail ESCALATED)
            executed_attempts = sum(
                1 for p in cat_payments for att in p.attempts
                if att.outcome in (Outcome.SUCCESS, Outcome.FAILED)
            )

            cat_recovered = [
                p for p in cat_payments
                if any(att.outcome == Outcome.SUCCESS for att in p.attempts)
            ]
            cat_success_count = len(cat_recovered)
            cat_recovered_amount = sum(p.amount for p in cat_recovered)

            if executed_attempts > 0:
                cat_success_rate = round((cat_success_count / executed_attempts) * 100.0, 2)
            else:
                cat_success_rate = 0.0

            intervention_breakdown[category] = {
                "total_records": len(cat_payments),
                "executed_attempts": executed_attempts,
                "success_count": cat_success_count,
                "recovered_amount": cat_recovered_amount,
                "success_rate": cat_success_rate
            }

        # Exception list: unresolved records (no SUCCESS outcome)
        exception_list: List[Dict[str, Any]] = []
        for p in payments:
            has_success = any(att.outcome == Outcome.SUCCESS for att in p.attempts)
            if not has_success:
                if len(p.escalations) > 0:
                    status = "ESCALATED"
                    reason = p.escalations[-1].reason
                elif any(att.outcome == Outcome.FAILED for att in p.attempts):
                    status = "FAILED"
                    last_failed = [att for att in p.attempts if att.outcome == Outcome.FAILED][-1]
                    reason = last_failed.reason or "Payment rail declined"
                elif any(att.outcome == Outcome.SKIPPED for att in p.attempts):
                    status = "SKIPPED"
                    last_skipped = [att for att in p.attempts if att.outcome == Outcome.SKIPPED][-1]
                    reason = last_skipped.reason or "Skipped due to retry policy hold"
                else:
                    status = "UNPROCESSED"
                    reason = "No attempt executed"

                exception_list.append({
                    "txn_id": p.txn_id,
                    "customer_id": p.customer_id,
                    "amount": p.amount,
                    "root_cause_label": p.root_cause_label,
                    "status": status,
                    "reason": reason
                })

        return {
            "total_processed": total_processed,
            "total_at_risk": total_at_risk,
            "recoverable_denominator": recoverable_denominator,
            "unrecoverable_amount": unrecoverable_amount,
            "money_recovered": money_recovered,
            "recovery_rate": recovery_rate,
            "escalation_count": escalation_count,
            "intervention_breakdown": intervention_breakdown,
            "exception_list": exception_list
        }

    def format_cli_report(self, report_data: Dict[str, Any]) -> str:
        lines = [
            "==================================================",
            "      AI REVENUE RECOVERY REPORT - PHASE 2 (v1)   ",
            "==================================================",
            f"Total Payments Processed  : {report_data['total_processed']}",
            f"Total Amount At-Risk      : ₹{report_data['total_at_risk']:,.2f}",
            f"Recoverable Denominator   : ₹{report_data['recoverable_denominator']:,.2f}",
            f"Unrecoverable Amount      : ₹{report_data['unrecoverable_amount']:,.2f}",
            "--------------------------------------------------",
            f"Money Recovered           : ₹{report_data['money_recovered']:,.2f}",
            f"Recovery Rate             : {report_data['recovery_rate']:.2f}%",
            f"Total Unique Escalations  : {report_data['escalation_count']}",
            "==================================================",
            "INTERVENTION BREAKDOWN BY CATEGORY:",
            "--------------------------------------------------"
        ]

        for cat, data in report_data["intervention_breakdown"].items():
            lines.append(f" Category: {cat}")
            lines.append(f"   - Total Records   : {data['total_records']}")
            lines.append(f"   - Executed Retries: {data['executed_attempts']}")
            lines.append(f"   - Recovered Count : {data['success_count']}")
            lines.append(f"   - Recovered Amount: ₹{data['recovered_amount']:,.2f}")
            lines.append(f"   - Success Rate    : {data['success_rate']:.2f}%")

        lines.append("==================================================")
        lines.append(f"EXCEPTION LIST (Unresolved Records: {len(report_data['exception_list'])}):")
        lines.append("--------------------------------------------------")

        for exc in report_data["exception_list"]:
            lines.append(
                f" [{exc['status']}] Txn: {exc['txn_id']} | ₹{exc['amount']:,.2f} | "
                f"Cat: {exc['root_cause_label']} | Reason: {exc['reason']}"
            )

        lines.append("==================================================")
        return "\n".join(lines)

"""T10.3 — Streamlit dashboard (read-only, no business logic)."""
from __future__ import annotations

import os

import pandas as pd
import streamlit as st
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.application.build_dashboard_data import BuildDashboardData
from src.application.get_recovery_queue import GetRecoveryQueue
from src.domain.metrics import DashboardMetrics
from src.domain.metrics import MetricsAggregator
from src.domain.recovery_queue import RecoveryQueue
from src.infrastructure.audit_repository import AuditLogRepository
from src.infrastructure.repository import SQLiteFailedPaymentRepository


# ---------------------------------------------------------------------------
# Pure helpers — no Streamlit side-effects, testable without a server
# ---------------------------------------------------------------------------

def format_inr(amount: float) -> str:
    # Indian grouping: 1,23,456.78 — keep deterministic, no locale dep.
    sign = "-" if amount < 0 else ""
    s = f"{abs(amount):.2f}"
    integer, decimal = s.split(".")
    if len(integer) <= 3:
        grouped = integer
    else:
        last3 = integer[-3:]
        rest = integer[:-3]
        parts: list[str] = []
        while len(rest) > 2:
            parts.append(rest[-2:])
            rest = rest[:-2]
        if rest:
            parts.append(rest)
        parts.reverse()
        grouped = ",".join(parts) + "," + last3
    return f"{sign}\u20b9{grouped}.{decimal}"


def format_pct(rate: float) -> str:
    return f"{rate:.2f}%"


def build_intervention_df(metrics: DashboardMetrics) -> pd.DataFrame:
    order = ["retry", "dunning", "re-auth", "refuse"]
    # DashboardMetrics.intervention_mix preserves expected order but we enforce
    data = {k: metrics.intervention_mix.get(k, 0) for k in order}
    # include any extra keys beyond expected (sorted) for completeness
    extras = {k: v for k, v in metrics.intervention_mix.items() if k not in data}
    for k in sorted(extras):
        data[k] = extras[k]
    df = pd.DataFrame(list(data.items()), columns=["Action", "Count"])
    return df


def build_tier_df(metrics: DashboardMetrics) -> pd.DataFrame:
    order = ["T1", "T2", "T3"]
    data = {k: metrics.tier_breakdown.get(k, 0) for k in order}
    df = pd.DataFrame(list(data.items()), columns=["Tier", "Count"])
    return df


def build_exception_df(metrics: DashboardMetrics) -> pd.DataFrame:
    rows = []
    for e in metrics.exception_list:
        rows.append({
            "Transaction": e.txn_id,
            "Amount": e.amount,
            "Root Cause": e.root_cause_label,
            "Status": e.status,
            "Tier": e.tier or "",
            "Reason Code": e.reason_code or "",
            "Reason": e.reason,
        })
    if not rows:
        return pd.DataFrame(columns=["Transaction", "Amount", "Root Cause", "Status", "Tier", "Reason Code", "Reason"])
    return pd.DataFrame(rows)


def filter_exceptions(
    exception_list,
    tier_filter: list[str] | None = None,
    status_filter: list[str] | None = None,
    root_cause_filter: list[str] | None = None,
):
    """View-only filter — does not alter KPIs."""
    result = list(exception_list)
    if tier_filter:
        result = [e for e in result if (e.tier or "") in tier_filter]
    if status_filter:
        result = [e for e in result if e.status in status_filter]
    if root_cause_filter:
        result = [e for e in result if e.root_cause_label in root_cause_filter]
    return result


def build_recovery_queue_df(queue: RecoveryQueue) -> pd.DataFrame:
    cols = [
        "Transaction",
        "Amount",
        "Currency",
        "Root Cause",
        "Failure Code",
        "Lifecycle State",
        "Recommendation",
        "Hint",
        "Status",
        "Tier",
        "Reason Code",
    ]
    if not queue.rows:
        return pd.DataFrame(columns=cols)
    records: list[dict[str, object]] = []
    for r in queue.rows:
        records.append({
            "Transaction": r.txn_id,
            "Amount": format_inr(r.amount),
            "Currency": r.currency,
            "Root Cause": r.root_cause_label,
            "Failure Code": r.failure_code,
            "Lifecycle State": r.lifecycle_state.value,
            "Recommendation": r.recommendation_kind.value,
            "Hint": r.provider_hint or "",
            "Status": r.status,
            "Tier": r.tier or "",
            "Reason Code": r.reason_code or "",
        })
    return pd.DataFrame(records, columns=cols)


def filter_recovery_queue(
    rows: tuple | list,  # type: ignore[type-arg]
    state_filter: list[str] | None = None,
    kind_filter: list[str] | None = None,
    tier_filter: list[str] | None = None,
    root_cause_filter: list[str] | None = None,
) -> list:
    """View-only filter for queue rows — does not alter KPIs."""
    result = list(rows)
    if state_filter:
        result = [r for r in result if r.lifecycle_state.value in state_filter]
    if kind_filter:
        result = [r for r in result if r.recommendation_kind.value in kind_filter]
    if tier_filter:
        result = [r for r in result if (r.tier or "") in tier_filter]
    if root_cause_filter:
        result = [r for r in result if r.root_cause_label in root_cause_filter]
    return result


DEMO_PAYMENTS_URL = "sqlite:///demo_failed_payments.db"
DEMO_AUDIT_URL = "sqlite:///demo_audit_log.db"


def load_metrics() -> DashboardMetrics:
    """Simple read-only flow: repos -> BuildDashboardData -> DashboardMetrics."""
    if os.getenv("DEMO_MODE") == "1":
        # Read-only selection of demo DBs — never seeds/writes.
        p_engine = create_engine(DEMO_PAYMENTS_URL, echo=False)
        PSess = sessionmaker(bind=p_engine, autoflush=False, autocommit=False)
        payment_repo = SQLiteFailedPaymentRepository(session_factory=PSess)
        audit_repo = AuditLogRepository(db_url=DEMO_AUDIT_URL)
    else:
        payment_repo = SQLiteFailedPaymentRepository()
        audit_repo = AuditLogRepository()
    aggregator = MetricsAggregator()
    builder = BuildDashboardData(
        payment_repository=payment_repo,
        audit_repository=audit_repo,
        metrics_aggregator=aggregator,
    )
    return builder.run()


def load_recovery_queue() -> RecoveryQueue:
    """Read-only queue load — same repo factory as load_metrics, no KPI mutation."""
    if os.getenv("DEMO_MODE") == "1":
        p_engine = create_engine(DEMO_PAYMENTS_URL, echo=False)
        PSess = sessionmaker(bind=p_engine, autoflush=False, autocommit=False)
        payment_repo = SQLiteFailedPaymentRepository(session_factory=PSess)
        audit_repo = AuditLogRepository(db_url=DEMO_AUDIT_URL)
    else:
        payment_repo = SQLiteFailedPaymentRepository()
        audit_repo = AuditLogRepository()
    service = GetRecoveryQueue(
        payment_repository=payment_repo,
        audit_repository=audit_repo,
    )
    return service.run()


# ---------------------------------------------------------------------------
# Streamlit rendering — only runs inside a Streamlit script context
# ---------------------------------------------------------------------------

def _render_dashboard() -> None:
    st.set_page_config(
        page_title="AI Revenue Recovery — Dashboard",
        page_icon="💳",
        layout="wide",
    )

    st.title("AI Revenue Recovery")
    st.caption("Failed-Payment Recovery Operations")
    st.info("Read-only / Simulation — no payment actions, no database writes.", icon="🔒")
    st.divider()

    # Load metrics (simple flow, no cache)
    try:
        metrics = load_metrics()
    except Exception as exc:  # noqa: BLE001 — dashboard must degrade gracefully
        st.error(f"Failed to load dashboard data: {exc}")
        st.stop()

    # Sidebar filters — view only, headline KPIs remain batch-wide
    st.sidebar.header("View filters \u2014 headline KPIs remain batch-wide")
    all_tiers = ["T1", "T2", "T3"]
    tier_filter = st.sidebar.multiselect("Tier", all_tiers, default=[])
    all_statuses = sorted({e.status for e in metrics.exception_list}) if metrics.exception_list else ["ESCALATED", "FAILED", "SKIPPED", "UNPROCESSED"]
    status_filter = st.sidebar.multiselect("Status", all_statuses, default=[])
    all_root_causes = sorted({e.root_cause_label for e in metrics.exception_list}) if metrics.exception_list else []
    root_cause_filter = st.sidebar.multiselect("Root Cause", all_root_causes, default=[])

    # Headline KPIs — directly from DashboardMetrics, no recalculation
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Money Recovered", format_inr(metrics.money_recovered))
    c2.metric("Total At Risk", format_inr(metrics.total_at_risk))
    c3.metric("Recoverable Pool", format_inr(metrics.money_recoverable))
    c4.metric("Recovery Rate", format_pct(metrics.recovery_rate))
    st.caption(f"Processed {metrics.total_processed} payments \u00b7 {metrics.total_events} audit events")

    # Intervention + Tier — two columns
    left, right = st.columns(2)
    with left:
        st.subheader("Intervention mix")
        idf = build_intervention_df(metrics)
        # Apply view filter to chart? Filters affect visible table only per spec;
        # but spec says filtering may affect visualization data only — keep charts batch-wide
        # to avoid confusing KPI/chart divergence. Charts remain batch-wide.
        st.bar_chart(idf.set_index("Action"))
    with right:
        st.subheader("Escalation tier breakdown")
        tdf = build_tier_df(metrics)
        st.bar_chart(tdf.set_index("Tier"))

    # Exception table — filtered view
    st.subheader("Unresolved / Exception list")
    filtered = filter_exceptions(
        metrics.exception_list,
        tier_filter=tier_filter,
        status_filter=status_filter,
        root_cause_filter=root_cause_filter,
    )
    if not metrics.exception_list:
        st.info("No unresolved payments")
    elif not filtered:
        st.info("No unresolved payments match the current filters.")
    else:
        # Build DF from filtered
        rows = []
        for e in filtered:
            rows.append({
                "Transaction": e.txn_id,
                "Amount": f"\u20b9{e.amount:,.2f}",
                "Root Cause": e.root_cause_label,
                "Status": e.status,
                "Tier": e.tier or "",
                "Reason Code": e.reason_code or "",
                "Reason": e.reason,
            })
        df = pd.DataFrame(rows)
        st.dataframe(df, use_container_width=True, hide_index=True)
        if len(filtered) != len(metrics.exception_list):
            st.caption(f"Showing {len(filtered)} of {len(metrics.exception_list)} exceptions — headline KPIs remain batch-wide")

    # Recovery Queue — operator view (read-only, filters view-only, KPIs batch-wide)
    st.subheader("Recovery Queue")
    try:
        queue = load_recovery_queue()
    except Exception as exc:  # noqa: BLE001 — degrade queue only, preserve KPIs
        st.error(f"Failed to load recovery queue: {exc}")
        from src.domain.recovery_lifecycle import RecoveryState as _RS
        from src.domain.recovery_recommendation import RecommendationKind as _RK
        queue = RecoveryQueue(
            rows=(),
            total=0,
            counts_by_state=tuple((s.value, 0) for s in _RS),
            counts_by_kind=tuple((k.value, 0) for k in _RK),
        )

    if not queue.rows:
        st.info("No payments requiring attention.")
    else:
        # Queue-specific sidebar filters — distinct keys so they don't collide with exception filters
        with st.sidebar:
            st.markdown("---")
            st.markdown("**Recovery Queue filters — queue view only**")
            q_state_filter = st.multiselect(
                "Queue lifecycle state",
                sorted({r.lifecycle_state.value for r in queue.rows}),
                default=[],
                key="queue_state",
            )
            q_kind_filter = st.multiselect(
                "Queue recommendation",
                sorted({r.recommendation_kind.value for r in queue.rows}),
                default=[],
                key="queue_kind",
            )
            q_tier_values = sorted({r.tier or "" for r in queue.rows if r.tier})
            q_tier_filter = st.multiselect("Queue tier", q_tier_values, default=[], key="queue_tier")
            q_rc_values = sorted({r.root_cause_label for r in queue.rows})
            q_rc_filter = st.multiselect("Queue root cause", q_rc_values, default=[], key="queue_root_cause")

        q_filtered = filter_recovery_queue(
            queue.rows,
            state_filter=q_state_filter or None,
            kind_filter=q_kind_filter or None,
            tier_filter=q_tier_filter or None,
            root_cause_filter=q_rc_filter or None,
        )
        if not q_filtered:
            st.info("No queue entries match the current filters.")
        else:
            qdf = build_recovery_queue_df(
                RecoveryQueue(
                    rows=tuple(q_filtered),
                    total=len(q_filtered),
                    counts_by_state=queue.counts_by_state,
                    counts_by_kind=queue.counts_by_kind,
                )
            )
            st.dataframe(qdf, use_container_width=True, hide_index=True)
            if len(q_filtered) != len(queue.rows):
                st.caption(f"Showing {len(q_filtered)} of {len(queue.rows)} queue entries — headline KPIs remain batch-wide")

    # Graceful failure — prominent
    st.subheader("Graceful failure")
    gf = metrics.graceful_failure
    if gf is None:
        st.info("No do-not-retry record")
    else:
        with st.container(border=True):
            st.markdown("**Graceful — refused hard-fraud record, handled without retry**")
            gc1, gc2, gc3, gc4 = st.columns(4)
            gc1.markdown(f"**Transaction:** {gf.txn_id}")
            gc2.markdown(f"**Action:** {gf.action}")
            gc3.markdown(f"**Reason code:** {gf.reason_code}")
            gc4.markdown(f"**Tier:** {gf.tier}")
            st.markdown(f"**Masked customer reference:** `{gf.customer_ref_masked}`")
            st.markdown(f"**Decision rationale:** {gf.decision_rationale}")
            st.caption(f"{gf.timestamp.isoformat()}")
            st.success("Refused and escalated — fully audited, no retry.")


def _is_streamlit_running() -> bool:
    try:
        from streamlit.runtime.scriptrunner import get_script_run_ctx
        return get_script_run_ctx() is not None
    except Exception:
        return False


# Only auto-run when executed by `streamlit run dashboard.py`, not on plain import
if _is_streamlit_running():
    _render_dashboard()

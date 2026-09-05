"""P5.5 DecideRecovery comprehensive behavioral tests."""
from __future__ import annotations

import uuid
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

from src.application.decide_recovery import DecideRecovery
from src.database import Base, FailedPayment, OperatorAuditModel, RecoveryLifecycleModel, SessionLocal
from src.domain.audit import ActionType
from src.domain.recovery_lifecycle import RecoveryState, HardStopViolation, InvalidTransitionError


def _clean(txn_id: str):
    Base.metadata.create_all(bind=SessionLocal.kw["bind"])
    with SessionLocal() as s:
        s.query(OperatorAuditModel).filter(OperatorAuditModel.txn_id == txn_id).delete()
        s.query(RecoveryLifecycleModel).filter(RecoveryLifecycleModel.txn_id == txn_id).delete()
        s.query(FailedPayment).filter(FailedPayment.txn_id == txn_id).delete()
        s.commit()

def _make_payment(txn_id: str, failure_code="insufficient_funds", root_cause="Insufficient Funds", recoverable=True, customer_id="CUST_X", amount=1000):
    _clean(txn_id)
    with SessionLocal() as s:
        s.add(FailedPayment(
            txn_id=txn_id, customer_id=customer_id, amount=amount, currency="INR",
            failure_code=failure_code, root_cause_label=root_cause,
            recoverable_flag=recoverable, retry_count=0, timestamp=datetime.utcnow(),
            payment_method="card"
        ))
        s.commit()
    return txn_id

def _lifecycle_state(txn_id: str):
    with SessionLocal() as s:
        row = s.get(RecoveryLifecycleModel, txn_id)
        return RecoveryState(row.state) if row else None

def _audit_rows(txn_id: str):
    with SessionLocal() as s:
        return s.query(OperatorAuditModel).filter(OperatorAuditModel.txn_id == txn_id).all()


def test_fresh_received_bootstrap_approve():
    txn = f"DR_APPROVE_{uuid.uuid4().hex[:6]}"
    _make_payment(txn, failure_code="insufficient_funds", root_cause="Insufficient Funds", recoverable=True)
    import src.application.decide_recovery as mod
    orig = mod.transition
    calls = []
    def wrapping(lc, target, payment, **kw):
        calls.append((lc.state, target, kw.get("auto_eligible")))
        return orig(lc, target, payment, **kw)
    with patch.object(mod, "transition", side_effect=wrapping):
        result = DecideRecovery().decide(txn, "approve", "operator ok")
        assert result.applied is True
        assert result.lifecycle_state == RecoveryState.APPROVED
        assert len(calls) == 3
        assert calls[0][1] == RecoveryState.ANALYZED
        assert calls[1][1] == RecoveryState.PENDING_APPROVAL
        assert calls[2][1] == RecoveryState.APPROVED
        for _, _, ae in calls:
            assert ae is False
    assert _lifecycle_state(txn) == RecoveryState.APPROVED
    with SessionLocal() as s:
        rows = s.query(RecoveryLifecycleModel).filter(RecoveryLifecycleModel.txn_id == txn).all()
        assert len(rows) == 1

def test_fresh_received_bootstrap_reject():
    txn = f"DR_REJECT_{uuid.uuid4().hex[:6]}"
    _make_payment(txn, failure_code="insufficient_funds", root_cause="Insufficient Funds", recoverable=True)
    import src.application.decide_recovery as mod
    orig = mod.transition
    calls = []
    def wrapping(lc, target, payment, **kw):
        calls.append((lc.state, target, kw.get("auto_eligible")))
        return orig(lc, target, payment, **kw)
    with patch.object(mod, "transition", side_effect=wrapping):
        result = DecideRecovery().decide(txn, "reject", "not good")
        assert result.applied is True
        assert result.lifecycle_state == RecoveryState.REJECTED
        assert len(calls) == 3
        assert calls[0][1] == RecoveryState.ANALYZED
        assert calls[1][1] == RecoveryState.PENDING_APPROVAL
        assert calls[2][1] == RecoveryState.REJECTED
    assert _lifecycle_state(txn) == RecoveryState.REJECTED

def test_bootstrap_intermediates_not_persisted():
    txn = f"DR_INTERM_{uuid.uuid4().hex[:6]}"
    _make_payment(txn)
    DecideRecovery().decide(txn, "approve", "ok")
    with SessionLocal() as s:
        rows = s.query(RecoveryLifecycleModel).filter(RecoveryLifecycleModel.txn_id == txn).all()
        assert len(rows) == 1
        assert rows[0].state == RecoveryState.APPROVED.value

def test_every_bootstrap_step_uses_transition():
    src = open("src/application/decide_recovery.py").read()
    assert "transition(" in src
    assert src.count("transition(") >= 2

def test_no_direct_state_assignment():
    src = open("src/application/decide_recovery.py").read()
    assert "lifecycle.state =" not in src

def test_auto_eligible_false_for_all_steps():
    txn = f"DR_AUTO_{uuid.uuid4().hex[:6]}"
    _make_payment(txn)
    import src.application.decide_recovery as mod
    orig = mod.transition
    seen = []
    def wrapping(lc, target, payment, **kw):
        seen.append(kw.get("auto_eligible"))
        return orig(lc, target, payment, **kw)
    with patch.object(mod, "transition", side_effect=wrapping):
        DecideRecovery().decide(txn, "approve", "ok")
        assert all(v is False for v in seen)

def test_auto_eligible_false_for_recommend():
    txn = f"DR_AUTO2_{uuid.uuid4().hex[:6]}"
    _make_payment(txn)
    mock_rec = MagicMock()
    from src.domain.recovery_recommendation import RecommendationKind, RecoveryRecommendation
    mock_rec.recommend.return_value = RecoveryRecommendation(
        txn_id=txn, kind=RecommendationKind.RETRY, suggested_next_state=RecoveryState.PENDING_APPROVAL,
        chosen_action="Smart retry", bounds="Max 3", rationale="x", provider_hint=None
    )
    from src.application.decide_recovery import DecideRecovery as DR
    # need to avoid real transition failing due to mock kind; use fresh txn with real payment
    # Instead verify real DecideRecovery calls recommend with auto_eligible=False by spying
    with patch("src.application.decide_recovery.RecommendRecovery") as MockRec:
        inst = MockRec.return_value
        inst.recommend.return_value = mock_rec.recommend.return_value
        txn3 = f"DR_AUTO3_{uuid.uuid4().hex[:6]}"
        _make_payment(txn3)
        try:
            DecideRecovery().decide(txn3, "approve", "ok")
        except Exception:
            pass
        assert inst.recommend.called
        _, kw = inst.recommend.call_args
        assert kw.get("auto_eligible") is False

def test_hard_stop_before_idempotency():
    txn = f"DR_HS_IDEM_{uuid.uuid4().hex[:6]}"
    _make_payment(txn, failure_code="fraud_suspected", root_cause="Hard Fraud / Do-Not-Retry", recoverable=False, customer_id="C_HS")
    Base.metadata.create_all(bind=SessionLocal.kw["bind"])
    with SessionLocal() as s:
        s.add(RecoveryLifecycleModel(txn_id=txn, state=RecoveryState.APPROVED.value, updated_at=datetime.utcnow(), reason="bad"))
        s.commit()
    with pytest.raises(HardStopViolation):
        DecideRecovery().decide(txn, "approve", "ok")

def test_hard_stop_cannot_be_approved_fresh():
    txn = f"DR_HS_FRESH_{uuid.uuid4().hex[:6]}"
    _make_payment(txn, failure_code="fraud_suspected", root_cause="Hard Fraud / Do-Not-Retry", recoverable=False)
    with pytest.raises(HardStopViolation):
        DecideRecovery().decide(txn, "approve", "try approve")
    assert _lifecycle_state(txn) is None
    assert len(_audit_rows(txn)) == 0

def test_hard_stop_unknown_ambiguous_cannot_be_approved():
    txn = f"DR_HS_UNK_{uuid.uuid4().hex[:6]}"
    _make_payment(txn, failure_code="unknown_code_xyz", root_cause="Unknown / Ambiguous", recoverable=True)
    with pytest.raises(HardStopViolation):
        DecideRecovery().decide(txn, "approve", "ok")

def test_rejection_requires_non_empty_stripped_reason():
    txn = f"DR_BLANK_{uuid.uuid4().hex[:6]}"
    _make_payment(txn)
    for bad in ["", "   ", "\t\n", " \n "]:
        with pytest.raises(ValueError, match="Rejection reason"):
            DecideRecovery().decide(txn, "reject", bad)
    result = DecideRecovery().decide(txn, "reject", "  valid reason  ")
    assert result.applied is True
    assert result.lifecycle_state == RecoveryState.REJECTED

def test_duplicate_approve_is_idempotent():
    txn = f"DR_DUP_AP_{uuid.uuid4().hex[:6]}"
    _make_payment(txn)
    r1 = DecideRecovery().decide(txn, "approve", "first")
    assert r1.applied is True
    r2 = DecideRecovery().decide(txn, "approve", "first again")
    assert r2.applied is False
    assert r2.lifecycle_state == RecoveryState.APPROVED
    assert _lifecycle_state(txn) == RecoveryState.APPROVED
    audits = _audit_rows(txn)
    assert len(audits) == 1

def test_duplicate_reject_is_idempotent():
    txn = f"DR_DUP_RJ_{uuid.uuid4().hex[:6]}"
    _make_payment(txn)
    r1 = DecideRecovery().decide(txn, "reject", "nope")
    assert r1.applied is True
    r2 = DecideRecovery().decide(txn, "reject", "nope again")
    assert r2.applied is False
    assert r2.lifecycle_state == RecoveryState.REJECTED
    assert _lifecycle_state(txn) == RecoveryState.REJECTED
    assert len(_audit_rows(txn)) == 1

def test_approved_to_rejected_conflict():
    txn = f"DR_CONFLICT1_{uuid.uuid4().hex[:6]}"
    _make_payment(txn)
    DecideRecovery().decide(txn, "approve", "ok")
    with pytest.raises((ValueError, InvalidTransitionError, HardStopViolation)):
        DecideRecovery().decide(txn, "reject", "change mind")

def test_rejected_to_approved_conflict():
    txn = f"DR_CONFLICT2_{uuid.uuid4().hex[:6]}"
    _make_payment(txn)
    DecideRecovery().decide(txn, "reject", "nope")
    with pytest.raises((ValueError, InvalidTransitionError, HardStopViolation)):
        DecideRecovery().decide(txn, "approve", "change mind")

def test_terminal_state_rejected():
    txn = f"DR_TERM_{uuid.uuid4().hex[:6]}"
    _make_payment(txn)
    Base.metadata.create_all(bind=SessionLocal.kw["bind"])
    with SessionLocal() as s:
        s.query(RecoveryLifecycleModel).filter(RecoveryLifecycleModel.txn_id == txn).delete()
        s.query(OperatorAuditModel).filter(OperatorAuditModel.txn_id == txn).delete()
        s.commit()
        s.add(RecoveryLifecycleModel(txn_id=txn, state=RecoveryState.REJECTED.value, updated_at=datetime.utcnow(), reason="x"))
        s.commit()
    with pytest.raises((ValueError, InvalidTransitionError)):
        DecideRecovery().decide(txn, "approve", "ok")
    txn2 = f"DR_TERM2_{uuid.uuid4().hex[:6]}"
    _make_payment(txn2)
    with SessionLocal() as s:
        s.add(RecoveryLifecycleModel(txn_id=txn2, state=RecoveryState.ESCALATED.value, updated_at=datetime.utcnow(), reason="x"))
        s.commit()
    with pytest.raises((ValueError, InvalidTransitionError)):
        DecideRecovery().decide(txn2, "approve", "ok")

def test_recommendation_retry_mapping():
    txn = f"DR_MAP_RETRY_{uuid.uuid4().hex[:6]}"
    _make_payment(txn, failure_code="insufficient_funds", root_cause="Insufficient Funds", recoverable=True)
    result = DecideRecovery().decide(txn, "approve", "ok")
    assert result.audit_event.action == ActionType.RETRY

def test_recommendation_dunning_mapping():
    txn = f"DR_MAP_DUNN_{uuid.uuid4().hex[:6]}"
    _make_payment(txn, failure_code="expired_card", root_cause="Expired Card", recoverable=True)
    result = DecideRecovery().decide(txn, "approve", "ok")
    assert result.audit_event.action == ActionType.DUNNING

def test_recommendation_reauth_mapping():
    txn = f"DR_MAP_REAUTH_{uuid.uuid4().hex[:6]}"
    _make_payment(txn, failure_code="mandate_revoked", root_cause="Mandate Lapse", recoverable=True)
    result = DecideRecovery().decide(txn, "approve", "ok")
    assert result.audit_event.action == ActionType.REAUTH

def test_rejection_maps_to_refuse():
    txn = f"DR_MAP_REFUSE_{uuid.uuid4().hex[:6]}"
    _make_payment(txn, failure_code="insufficient_funds", root_cause="Insufficient Funds", recoverable=True)
    result = DecideRecovery().decide(txn, "reject", "operator refuses")
    assert result.audit_event.action == ActionType.REFUSE

def test_outcome_skipped():
    txn = f"DR_SKIPPED_{uuid.uuid4().hex[:6]}"
    _make_payment(txn)
    r = DecideRecovery().decide(txn, "approve", "ok")
    assert r.audit_event.outcome.value == "skipped"
    txn2 = f"DR_SKIPPED2_{uuid.uuid4().hex[:6]}"
    _make_payment(txn2)
    r2 = DecideRecovery().decide(txn2, "reject", "nope")
    assert r2.audit_event.outcome.value == "skipped"

def test_audit_field_preservation_and_masked_ref():
    from src.domain.masking import MaskingPolicy
    txn = f"DR_AUDIT_{uuid.uuid4().hex[:6]}"
    cust = f"CUST_{uuid.uuid4().hex[:6]}"
    _make_payment(txn, customer_id=cust)
    result = DecideRecovery().decide(txn, "approve", "  my reason  ")
    ae = result.audit_event
    assert ae.txn_id == txn
    assert ae.decision_rationale == "my reason"
    assert ae.tier == "T1"
    assert ae.reason_code is not None
    mp = MaskingPolicy()
    assert ae.customer_ref_masked == mp.mask_customer_ref(cust)
    assert "MASKED::" in ae.customer_ref_masked
    rows = _audit_rows(txn)
    assert len(rows) == 1
    assert rows[0].customer_ref_masked == ae.customer_ref_masked
    assert rows[0].tier == "T1"

def test_lifecycle_plus_audit_atomic_commit():
    txn = f"DR_ATOMIC_{uuid.uuid4().hex[:6]}"
    _make_payment(txn)
    result = DecideRecovery().decide(txn, "approve", "ok")
    assert result.applied is True
    with SessionLocal() as s:
        lc = s.get(RecoveryLifecycleModel, txn)
        audits = s.query(OperatorAuditModel).filter(OperatorAuditModel.txn_id == txn).all()
        assert lc is not None
        assert lc.state == RecoveryState.APPROVED.value
        assert len(audits) == 1

def test_audit_failure_rolls_back_lifecycle():
    txn = f"DR_AUDITFAIL_{uuid.uuid4().hex[:6]}"
    _make_payment(txn)
    real_factory = SessionLocal
    def failing_factory():
        sess = real_factory()
        orig_add = sess.add
        def fake_add(obj):
            if isinstance(obj, OperatorAuditModel):
                raise Exception("audit insert failure")
            return orig_add(obj)
        sess.add = fake_add
        return sess
    with pytest.raises(Exception, match="audit insert failure"):
        DecideRecovery(session_factory=failing_factory).decide(txn, "approve", "ok")
    assert _lifecycle_state(txn) is None
    assert len(_audit_rows(txn)) == 0

def test_lifecycle_persistence_failure_prevents_audit():
    txn = f"DR_LCFAIL_{uuid.uuid4().hex[:6]}"
    _make_payment(txn)
    mock_repo = MagicMock()
    mock_repo.get.return_value = None
    mock_repo.compare_and_set.side_effect = Exception("db down")
    with pytest.raises(Exception, match="db down"):
        DecideRecovery(lifecycle_repository=mock_repo).decide(txn, "approve", "ok")
    assert _lifecycle_state(txn) is None
    assert len(_audit_rows(txn)) == 0

def test_cas_losing_race_idempotent():
    txn2 = f"DR_CAS_IDEM2_{uuid.uuid4().hex[:6]}"
    _make_payment(txn2)
    mock_repo = MagicMock()
    mock_repo.compare_and_set.return_value = False
    from src.domain.recovery_lifecycle import RecoveryLifecycle
    mock_repo.get.return_value = RecoveryLifecycle(txn_id=txn2, state=RecoveryState.APPROVED, updated_at=datetime.utcnow())
    result = DecideRecovery(lifecycle_repository=mock_repo).decide(txn2, "approve", "ok")
    assert result.applied is False
    assert result.lifecycle_state == RecoveryState.APPROVED

def test_cas_losing_race_conflict():
    txn = f"DR_CAS_CONFL_{uuid.uuid4().hex[:6]}"
    _make_payment(txn)
    mock_repo = MagicMock()
    mock_repo.compare_and_set.return_value = False
    from src.domain.recovery_lifecycle import RecoveryLifecycle
    mock_repo.get.return_value = RecoveryLifecycle(txn_id=txn, state=RecoveryState.REJECTED, updated_at=datetime.utcnow())
    with pytest.raises(ValueError, match="Conflict"):
        DecideRecovery(lifecycle_repository=mock_repo).decide(txn, "approve", "ok")

def test_no_razorpay_network_calls():
    src = open("src/application/decide_recovery.py").read()
    assert "infrastructure.razorpay" not in src
    assert "ingest_razorpay" not in src
    assert "requests." not in src

def test_no_direct_received_to_approved():
    src = open("src/application/decide_recovery.py").read()
    assert "ANALYZED" in src
    assert "PENDING_APPROVAL" in src
    assert src.count("transition(") >= 3

def test_approved_never_claims_recovered():
    from src.domain.audit import ReasonCode
    for fc, rc in [("insufficient_funds", "Insufficient Funds"), ("expired_card", "Expired Card"), ("mandate_revoked", "Mandate Lapse")]:
        txn = f"DR_NOREC_{uuid.uuid4().hex[:6]}"
        _make_payment(txn, failure_code=fc, root_cause=rc, recoverable=True)
        result = DecideRecovery().decide(txn, "approve", "ok")
        assert result.audit_event.reason_code != ReasonCode.RECOVERED
        assert result.audit_event.outcome.value == "skipped"
        rows = _audit_rows(txn)
        assert rows[0].reason_code != ReasonCode.RECOVERED.value
        assert rows[0].reason_code == result.audit_event.reason_code.value

def test_approved_reason_code_matches_failure_context():
    from src.domain.audit import ReasonCode
    txn = f"DR_CTX_{uuid.uuid4().hex[:6]}"
    _make_payment(txn, failure_code="insufficient_funds", root_cause="Insufficient Funds", recoverable=True)
    result = DecideRecovery().decide(txn, "approve", "ok")
    assert result.audit_event.reason_code == ReasonCode.RAIL_DECLINED
    assert result.audit_event.outcome.value == "skipped"
    rows = _audit_rows(txn)
    assert rows[0].reason_code == ReasonCode.RAIL_DECLINED.value
    assert rows[0].outcome == "skipped"

def test_approved_idempotent_never_recovered():
    from src.domain.audit import ReasonCode
    txn = f"DR_IDEM_NOREC_{uuid.uuid4().hex[:6]}"
    _make_payment(txn, failure_code="insufficient_funds", root_cause="Insufficient Funds", recoverable=True)
    r1 = DecideRecovery().decide(txn, "approve", "ok")
    r2 = DecideRecovery().decide(txn, "approve", "again")
    assert r1.audit_event.reason_code != ReasonCode.RECOVERED
    assert r2.audit_event.reason_code != ReasonCode.RECOVERED
    assert r2.audit_event.reason_code == ReasonCode.RAIL_DECLINED
    assert r2.applied is False

def test_persisted_audit_matches_returned_event():
    from src.domain.audit import ReasonCode
    txn = f"DR_PERSIST_{uuid.uuid4().hex[:6]}"
    _make_payment(txn, failure_code="insufficient_funds", root_cause="Insufficient Funds", recoverable=True)
    result = DecideRecovery().decide(txn, "approve", "my audit check")
    rows = _audit_rows(txn)
    assert len(rows) == 1
    row = rows[0]
    ae = result.audit_event
    assert row.event_id == ae.event_id
    assert row.txn_id == ae.txn_id
    assert row.action == ae.action.value
    assert row.reason_code == ae.reason_code.value
    assert row.reason_code != ReasonCode.RECOVERED.value
    assert row.outcome == ae.outcome.value == "skipped"
    assert row.customer_ref_masked == ae.customer_ref_masked
    assert row.tier == ae.tier
    assert row.decision_rationale == ae.decision_rationale

import pytest
import pandas as pd
from src.policy_engine import PolicyEngine

def test_policy_engine_initialization():
    engine = PolicyEngine()
    assert engine.policies is not None
    assert "Insufficient Funds" in engine.policies
    assert "Hard Fraud / Do-Not-Retry" in engine.policies

def test_get_action_for_category():
    engine = PolicyEngine()
    
    insufficient_policy = engine.get_action_for_category("Insufficient Funds")
    assert "retry" in insufficient_policy["chosen_action"].lower()
    assert insufficient_policy["bounds"] is not None

    fraud_policy = engine.get_action_for_category("Hard Fraud / Do-Not-Retry")
    assert "escalate" in fraud_policy["chosen_action"].lower()

    unknown_policy = engine.get_action_for_category("Unknown / Ambiguous")
    assert "escalate" in unknown_policy["chosen_action"].lower()

def test_apply_policy_and_summary():
    engine = PolicyEngine()
    
    # Mock classified dataframe
    data = [
        {"txn_id": "pay_1", "failure_code": "insufficient_funds", "predicted_category": "Insufficient Funds"},
        {"txn_id": "pay_2", "failure_code": "fraud_suspected", "predicted_category": "Hard Fraud / Do-Not-Retry"},
        {"txn_id": "pay_3", "failure_code": "unknown_err", "predicted_category": "Unknown / Ambiguous"}
    ]
    df = pd.DataFrame(data)
    
    policy_df = engine.apply_policy(df)
    assert "chosen_action" in policy_df.columns
    assert "bounds" in policy_df.columns
    assert len(policy_df) == 3

    summary = engine.generate_summary(policy_df)
    assert "category_counts" in summary
    assert "action_counts" in summary
    assert summary["escalation_count"] == 2  # Fraud + Unknown

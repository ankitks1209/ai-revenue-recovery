import pytest
from src.classifier import FailureClassifier
from src.generator import generate_failed_payments

def test_classifier_initialization():
    classifier = FailureClassifier()
    assert classifier.code_to_category is not None
    assert len(classifier.code_to_category) > 0
    # Verify specific known code mappings
    assert classifier.classify_code("insufficient_funds") == "Insufficient Funds"
    assert classifier.classify_code("51") == "Insufficient Funds"
    assert classifier.classify_code("expired_card") == "Expired Card"
    assert classifier.classify_code("fraud_suspected") == "Hard Fraud / Do-Not-Retry"
    assert classifier.classify_code("do_not_honor") == "Hard Fraud / Do-Not-Retry"

def test_unknown_failure_code_routing():
    classifier = FailureClassifier()
    # Unknown or unmapped codes must route to "Unknown / Ambiguous"
    assert classifier.classify_code("completely_bogus_code_xyz") == "Unknown / Ambiguous"
    assert classifier.classify_code("") == "Unknown / Ambiguous"
    assert classifier.classify_code(None) == "Unknown / Ambiguous"

def test_classifier_batch_and_evaluation():
    classifier = FailureClassifier()
    records = generate_failed_payments(count=50, seed=42)
    
    eval_res = classifier.evaluate_batch(records)
    
    assert "dataframe" in eval_res
    assert "report_dict" in eval_res
    assert "report_str" in eval_res
    assert "misclassifications" in eval_res
    
    df = eval_res["dataframe"]
    assert "predicted_category" in df.columns
    assert len(df) == 50
    
    # Since synthetic generator failure codes match taxonomy codes, accuracy should be 100%
    assert eval_res["report_dict"]["accuracy"] == 1.0

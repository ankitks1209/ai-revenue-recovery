import pytest
from datetime import datetime
from pydantic import ValidationError
from src.config import RANDOM_SEED
from src.models import FailedPaymentSchema
from src.generator import generate_failed_payments, seed_db
from src.database import SessionLocal, FailedPayment, init_db

def test_generate_failed_payments_count():
    """Verify that the generator produces exactly the specified count of records."""
    count = 60
    records = generate_failed_payments(count=count, seed=RANDOM_SEED)
    assert len(records) == count
    
    # Assert every record has correct structure and valid types
    for r in records:
        assert r["txn_id"].startswith("pay_")
        assert r["customer_id"].startswith("cust_")
        assert r["amount"] > 0
        assert r["currency"] == "INR"
        assert isinstance(r["timestamp"], datetime)
        assert r["payment_method"] in ["card", "upi", "netbanking", "mandate"]

def test_generate_failed_payments_seeding_reproducibility():
    """Verify that seeding results in strict reproducibility of generated records."""
    records_1 = generate_failed_payments(count=30, seed=123)
    records_2 = generate_failed_payments(count=30, seed=123)
    records_3 = generate_failed_payments(count=30, seed=456)
    
    # Same seed should produce identical records
    assert records_1 == records_2
    
    # Different seed should produce different records
    assert records_1 != records_3

def test_pydantic_schema_validation():
    """Verify that the Pydantic schema correctly validates field values and formats."""
    # Valid input should succeed
    valid_data = {
        "txn_id": "pay_test123",
        "customer_id": "cust_test123",
        "amount": 1500.50,
        "currency": "inr",  # Lowercase should validate and cast to uppercase
        "failure_code": "insufficient_funds",
        "root_cause_label": "Insufficient Funds",
        "recoverable_flag": True,
        "retry_count": 0,
        "timestamp": datetime.now(),
        "payment_method": "card"
    }
    schema = FailedPaymentSchema(**valid_data)
    assert schema.currency == "INR"

    # Negative amount should fail
    invalid_data_negative_amount = valid_data.copy()
    invalid_data_negative_amount["amount"] = -10.0
    with pytest.raises(ValidationError):
        FailedPaymentSchema(**invalid_data_negative_amount)

    # Invalid payment method should fail
    invalid_data_payment_method = valid_data.copy()
    invalid_data_payment_method["payment_method"] = "crypto"
    with pytest.raises(ValidationError):
        FailedPaymentSchema(**invalid_data_payment_method)

    # Negative retry count should fail
    invalid_data_retry = valid_data.copy()
    invalid_data_retry["retry_count"] = -1
    with pytest.raises(ValidationError):
        FailedPaymentSchema(**invalid_data_retry)

def test_database_seeding_and_retrieval():
    """Verify that DB tables are created and seeded correctly via SQLAlchemy."""
    session = SessionLocal()
    try:
        # Seed the database
        seed_db(session, count=60, seed=RANDOM_SEED)
        
        # Query and assert counts
        db_records = session.query(FailedPayment).all()
        assert len(db_records) == 60
        
        # Verify first record fields
        first_rec = db_records[0]
        assert first_rec.txn_id is not None
        assert first_rec.amount > 0
        assert first_rec.currency == "INR"
        assert first_rec.payment_method in ["card", "upi", "netbanking", "mandate"]
        
    finally:
        session.close()

def test_precedence_and_flag_seeding():
    """Verify that ground-truth labels and recoverable flags are correct for critical categories."""
    records = generate_failed_payments(count=100, seed=RANDOM_SEED)
    
    for r in records:
        category = r["root_cause_label"]
        flag = r["recoverable_flag"]
        code = r["failure_code"]
        
        # 1. Assert recoverable_flag correlates correctly with categories
        if category in ["Insufficient Funds", "Expired Card", "Transient/Network", "Mandate Lapse"]:
            assert flag is True
        elif category in ["Hard Fraud / Do-Not-Retry", "Unknown / Ambiguous"]:
            assert flag is False
            
        # 2. Hard Fraud / Do-Not-Retry validation
        if category == "Hard Fraud / Do-Not-Retry":
            assert code in ["fraud_suspected", "stolen_card", "do_not_honor", "blocklist"]
            
        # 3. Mandate Lapse validation
        if category == "Mandate Lapse":
            assert r["payment_method"] == "mandate"
            
        # 4. Expired Card validation
        if category == "Expired Card":
            assert r["payment_method"] == "card"

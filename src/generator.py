import random
from datetime import datetime, timedelta
import numpy as np
from faker import Faker
from typing import List, Dict, Any
from src.config import RANDOM_SEED
from src.models import FailedPaymentSchema

# Mapped categories and codes for generation
GENERATOR_TAXONOMY = {
    "Insufficient Funds": {
        "codes": ["insufficient_funds", "51", "low_balance"],
        "recoverable": True,
        "payment_methods": ["card", "upi", "netbanking", "mandate"]
    },
    "Expired Card": {
        "codes": ["expired_card", "54", "card_expired"],
        "recoverable": True,
        "payment_methods": ["card"]  # Expired Card only applies to card
    },
    "Transient/Network": {
        "codes": ["issuer_unavailable", "gateway_timeout", "91", "network_error"],
        "recoverable": True,
        "payment_methods": ["card", "upi", "netbanking", "mandate"]
    },
    "Mandate Lapse": {
        "codes": ["mandate_revoked", "mandate_expired"],
        "recoverable": True,
        "payment_methods": ["mandate"]  # Mandate Lapse only applies to mandate payments
    },
    "Hard Fraud / Do-Not-Retry": {
        "codes": ["fraud_suspected", "stolen_card", "do_not_honor", "blocklist"],
        "recoverable": False,
        "payment_methods": ["card", "upi", "netbanking", "mandate"]
    },
    "Unknown / Ambiguous": {
        "codes": ["unknown_error", "system_malfunction"],
        "recoverable": False,
        "payment_methods": ["card", "upi", "netbanking", "mandate"]
    }
}

def generate_failed_payments(count: int = 60, seed: int = RANDOM_SEED) -> List[Dict[str, Any]]:
    """
    Generate a list of failed payment records using fixed seeds for Faker and NumPy.
    Ensures realistic messiness and strictly follows the requested taxonomy.
    """
    # Seed all random generators for strict reproducibility
    random.seed(seed)
    np.random.seed(seed)
    fake = Faker()
    fake.seed_instance(seed)

    # Category distribution targets (out of 60)
    category_weights = {
        "Insufficient Funds": 0.30,          # ~18 records
        "Expired Card": 0.20,                # ~12 records
        "Transient/Network": 0.25,           # ~15 records
        "Mandate Lapse": 0.10,               # ~6 records
        "Hard Fraud / Do-Not-Retry": 0.10,   # ~6 records
        "Unknown / Ambiguous": 0.05          # ~3 records
    }

    categories = list(category_weights.keys())
    weights = list(category_weights.values())

    # Sample categories using np.random.choice to enforce weight distribution
    chosen_categories = np.random.choice(categories, size=count, p=weights)

    records = []
    base_time = datetime(2026, 1, 1)

    for i, category in enumerate(chosen_categories):
        meta = GENERATOR_TAXONOMY[category]
        
        # 1. Unique IDs
        txn_id = f"pay_{fake.unique.bothify(text='??????????????')}"
        customer_id = f"cust_{fake.bothify(text='??????????????')}"
        
        # 2. Financial values: Realistic payment amounts
        # Using a log-normal distribution for typical subscription/retail pricing
        amount = round(float(np.random.uniform(99.0, 9999.0)), 2)
        currency = "INR"

        # 3. Failure Code & Payment Method compatibility
        failure_code = random.choice(meta["codes"])
        payment_method = random.choice(meta["payment_methods"])

        # 4. Ground-truth seeding (to avoid cherry-picking)
        root_cause_label = category
        recoverable_flag = meta["recoverable"]

        # 5. Realistic messiness (prior retry counts)
        if category == "Hard Fraud / Do-Not-Retry":
            # Fraudulent/blocklisted payments should have 0 prior retries
            retry_count = 0
        elif category == "Transient/Network":
            # Network issues are sometimes retried immediately or previously
            retry_count = int(np.random.choice([0, 1, 2, 3], p=[0.5, 0.3, 0.15, 0.05]))
        else:
            retry_count = int(np.random.choice([0, 1, 2], p=[0.8, 0.15, 0.05]))

        # 6. Varied timestamps in the last 30 days
        timestamp = base_time + timedelta(
            days=float(np.random.uniform(0, 30)),
            hours=float(np.random.uniform(0, 24)),
            minutes=float(np.random.uniform(0, 60))
        )

        record_data = {
            "txn_id": txn_id,
            "customer_id": customer_id,
            "amount": amount,
            "currency": currency,
            "failure_code": failure_code,
            "root_cause_label": root_cause_label,
            "recoverable_flag": recoverable_flag,
            "retry_count": retry_count,
            "timestamp": timestamp,
            "payment_method": payment_method
        }

        # Validate with Pydantic
        validated_record = FailedPaymentSchema(**record_data)
        records.append(validated_record.model_dump())

    return records

def seed_db(session, count: int = 60, seed: int = RANDOM_SEED):
    """
    Generate synthetic records, validate via Pydantic, and write to the SQLite database.
    """
    from src.database import FailedPayment, init_db

    # Initialize / clean tables
    init_db()

    records = generate_failed_payments(count=count, seed=seed)
    
    # Transform list of dicts to SQLAlchemy model objects
    db_objects = [FailedPayment(**record) for record in records]
    
    # Add to session and commit
    session.add_all(db_objects)
    session.commit()


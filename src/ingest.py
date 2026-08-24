import pandas as pd
from src.database import SessionLocal, FailedPayment, init_db
from src.generator import generate_failed_payments
from src.config import RANDOM_SEED

def load_failed_payments_to_db(count: int = 60, seed: int = RANDOM_SEED):
    """
    Generate synthetic failed payments, load them into a pandas DataFrame,
    sanity check the category and recoverable flag counts, and commit to SQLAlchemy database.
    """
    # Initialize DB (creates/recreates tables)
    init_db()

    # Generate records (dict list)
    records = generate_failed_payments(count=count, seed=seed)

    # Load into pandas DataFrame
    df = pd.DataFrame(records)

    print("=== Day 1 Ingestion & Sanity Check ===")
    print(f"Successfully generated and loaded {len(df)} records into pandas DataFrame.")
    
    print("\n--- Sanity Check: Root Cause Category Counts ---")
    category_counts = df["root_cause_label"].value_counts()
    print(category_counts)

    print("\n--- Sanity Check: Recoverable Flag Distribution ---")
    recoverable_counts = df["recoverable_flag"].value_counts()
    print(recoverable_counts)

    # Write to SQLAlchemy database via session
    session = SessionLocal()
    try:
        db_objects = [FailedPayment(**row) for row in df.to_dict(orient="records")]
        session.add_all(db_objects)
        session.commit()
        print(f"\nSuccessfully committed {len(db_objects)} records to SQLite database ('failed_payments').")
    except Exception as e:
        session.rollback()
        raise e
    finally:
        session.close()

if __name__ == "__main__":
    load_failed_payments_to_db()

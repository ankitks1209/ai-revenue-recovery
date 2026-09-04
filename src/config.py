import os
import yaml
from pathlib import Path
from dotenv import load_dotenv

# Load environment variables from .env if present
load_dotenv()

# Base directory of the project
BASE_DIR = Path(__file__).resolve().parent.parent

# Config paths
TAXONOMY_PATH = BASE_DIR / "config" / "taxonomy.yaml"
POLICY_PATH = BASE_DIR / "config" / "policy.yaml"

# Environment configurations
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///failed_payments.db")
RANDOM_SEED = int(os.getenv("RANDOM_SEED", "42"))
RAZORPAY_WEBHOOK_SECRET = os.getenv("RAZORPAY_WEBHOOK_SECRET", "")
RAZORPAY_TEST_MODE = os.getenv("RAZORPAY_TEST_MODE", "true").lower() in ("1", "true", "yes")

def load_taxonomy() -> dict:
    """Load the failure-code taxonomy from config/taxonomy.yaml."""
    if not TAXONOMY_PATH.exists():
        raise FileNotFoundError(f"Taxonomy configuration not found at {TAXONOMY_PATH}")
    with open(TAXONOMY_PATH, "r") as f:
        return yaml.safe_load(f)

def load_policy() -> dict:
    """Load the intervention policy from config/policy.yaml."""
    if not POLICY_PATH.exists():
        raise FileNotFoundError(f"Policy configuration not found at {POLICY_PATH}")
    with open(POLICY_PATH, "r") as f:
        return yaml.safe_load(f)

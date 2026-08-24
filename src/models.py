from datetime import datetime
from typing import Literal
from pydantic import BaseModel, Field, field_validator

class FailedPaymentSchema(BaseModel):
    txn_id: str = Field(..., min_length=1, description="Unique transaction identifier")
    customer_id: str = Field(..., min_length=1, description="Customer restricted identifier masked downstream")
    amount: float = Field(..., gt=0, description="Transaction amount (must be positive)")
    currency: str = Field(..., min_length=3, max_length=3, description="ISO currency code (e.g., INR)")
    failure_code: str = Field(..., min_length=1, description="Raw gateway decline/failure code")
    root_cause_label: str = Field(..., min_length=1, description="Ground-truth root-cause category")
    recoverable_flag: bool = Field(..., description="Ground truth: is this genuinely recoverable")
    retry_count: int = Field(..., ge=0, description="Prior retry attempts (must be non-negative)")
    timestamp: datetime = Field(..., description="Time of failed attempt")
    payment_method: Literal["card", "upi", "netbanking", "mandate"] = Field(..., description="Payment method used")

    @field_validator("currency")
    @classmethod
    def validate_currency_uppercase(cls, v: str) -> str:
        return v.upper()

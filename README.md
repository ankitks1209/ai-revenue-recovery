# AI Failed-Payment Recovery Agent

A controlled payment-recovery system for failed Razorpay payments.

The project combines deterministic payment-failure classification, AI-assisted failure diagnosis, policy-based recovery decisions, operator approval, Razorpay Test Mode Payment Links, webhook-driven lifecycle updates, idempotency, auditability, and recovery metrics.

## Why this project

A failed payment is not always safe to retry.

A recovery system needs to answer six questions:

1. What failed?
2. Why did it fail?
3. Is recovery allowed?
4. What recovery action should be proposed?
5. Did the external payment action actually happen?
6. What was the final business result?

This project separates those concerns so that AI helps with diagnosis and explanation, while money-moving decisions remain deterministic and auditable.

## Core workflow

```text
Failed payment
      |
      v
Failure classification
      |
      +----> AI diagnosis (advisory)
      |
      v
Deterministic recovery policy
      |
      v
Operator approval / rejection
      |
      v
Recovery lifecycle
APPROVED -> EXECUTING
      |
      v
Razorpay Test Mode Payment Link
      |
      v
Webhook ingestion
      |
      v
RECOVERED / FAILED
      |
      v
Audit trail + dashboard metrics
```

### Safety boundary

The AI diagnosis layer does **not** approve, reject, retry, refund, or execute payments.

Hard-fraud and stopping-rule cases remain protected by deterministic recovery rules. These cases are refused/escalated rather than sent to the payment rail.

## Architecture

The project uses a modular-monolith / DDD-style structure:

```text
src/
├── domain/
│   ├── recovery_lifecycle.py
│   ├── recovery_queue.py
│   ├── recovery_recommendation.py
│   ├── audit.py
│   └── ...
│
├── application/
│   ├── recommend_recovery.py
│   ├── decide_recovery.py
│   ├── execute_approved_recovery.py
│   ├── ingest_razorpay_event.py
│   ├── ingest_payment_link_result.py
│   ├── get_recovery_queue.py
│   ├── build_dashboard_data.py
│   └── ai_diagnosis.py
│
└── infrastructure/
    ├── repository.py
    ├── audit_repository.py
    └── razorpay/
        ├── razorpay_recovery_rail.py
        ├── razorpay_payment_rail.py
        ├── payment_link_payload_mapper.py
        ├── webhook_verifier.py
        └── webhook_event_repository.py
```

### Layer responsibilities

**Domain** contains lifecycle states, recovery recommendations, audit semantics, safety invariants, and other business rules.

**Application** orchestrates use cases such as classification/recommendation, operator decisions, execution, webhook ingestion, dashboard data assembly, and AI diagnosis.

**Infrastructure** handles persistence, Razorpay integration, webhook verification, and provider-specific mapping.

**Dashboard** is a Streamlit operator console. It displays recovery metrics, the recovery queue, AI diagnosis, safety controls, and operator actions.

## AI diagnosis

The AI layer uses Groq with the configured Qwen model.

Example output:

```text
Diagnosis: Gateway Timeout
Confidence: High

The payment gateway failed to respond within the expected
time limit, likely due to transient network issues or
high server load.
```

The diagnosis is intentionally separate from recovery authorization:

```text
AI diagnosis
     |
     v
Human-readable explanation
     |
     v
Deterministic policy
     |
     v
Operator decision
```

If the AI provider is unavailable, the system falls back to the existing deterministic classification instead of breaking the recovery console.

## Razorpay integration

The project is configured for **Razorpay Test Mode**, not production payments.

The current recovery rail uses Razorpay Payment Links as the concrete recovery mechanism for approved retry flows.

The webhook endpoint is:

```text
POST /webhooks/razorpay
```

The webhook transport validates:

- Razorpay signature
- event ID
- payload shape
- duplicate event IDs

Payment Link result ingestion then updates the recovery lifecycle and audit trail.

## Local setup

### 1. Clone the repository

```bash
git clone https://github.com/ankitks1209/ai-revenue-recovery.git
cd ai-revenue-recovery
```

### 2. Create and activate a virtual environment

macOS / Linux:

```bash
python3 -m venv .venv
source .venv/bin/activate
```

### 3. Install dependencies

```bash
python -m pip install -r requirements.txt
```

### 4. Configure environment variables

Copy the example file:

```bash
cp .env.example .env
```

Add your local credentials:

```env
DATABASE_URL=sqlite:///failed_payments.db
RANDOM_SEED=42

RAZORPAY_WEBHOOK_SECRET=your_razorpay_webhook_secret
RAZORPAY_TEST_MODE=true
RAZORPAY_KEY_ID=your_razorpay_test_key_id
RAZORPAY_KEY_SECRET=your_razorpay_test_key_secret
RAZORPAY_BASE_URL=https://api.razorpay.com/v1

GROQ_API_KEY=your_groq_api_key
GROQ_MODEL=qwen/qwen3.6-27b
```

Never commit `.env`.

## Run the dashboard

```bash
streamlit run dashboard.py
```

Open the local Streamlit URL shown in the terminal.

The dashboard has two operating modes:

- normal/Test Mode workflow using `failed_payments.db`
- optional demo/read-only mode using the demo database when `DEMO_MODE=1`

## Run the tests

```bash
python -m pytest -q
```

The final local verification for this version completed with all tests passing.

Warnings from third-party libraries may still appear during the test run; they do not indicate test failures.

## Demo walkthrough

For the 5-minute demo:

### 1. Start with the recovery queue

Show a failed payment in the recovery queue.

### 2. Run AI diagnosis

Select the transaction and click:

```text
Analyze selected failure
```

Show the diagnosis and confidence.

Explain:

> The AI is advisory. It explains the payment failure, but it does not authorize the recovery action.

### 3. Make the operator decision

Select:

```text
Decision -> approve
```

Then submit the decision.

### 4. Show the Razorpay recovery action

For an approved retry case, the application creates a Razorpay Test Mode Payment Link.

### 5. Close the lifecycle

Show the Payment Link result and webhook-driven state transition to:

```text
RECOVERED
```

### 6. Show the safety controls

Show the graceful-failure / safety section and explain that hard-fraud or stopping-rule cases are refused, escalated, and audited instead of being retried.

## Measured results

The batch evaluation contains synthetic test data. These numbers must not be presented as real merchant revenue.

```text
Failed payments tested : 60
Total at risk          : ₹3,00,900.69
Recoverable pool       : ₹2,47,369.37
Recovered              : ₹1,22,019.57
Recovery rate          : 49.33%
```

Separately, the Razorpay Test Mode flow was validated with an actual Payment Link.

## Safety and reliability

The project includes protections for:

- hard-fraud / do-not-retry cases
- stopping rules and retry limits
- duplicate recovery execution
- webhook event idempotency
- operator approval gating
- append-only audit events
- masked customer references
- graceful provider/API failures
- lifecycle consistency between `APPROVED`, `EXECUTING`, `RECOVERED`, `FAILED`, `REJECTED`, and `ESCALATED`

A key reliability design is:

```text
APPROVED
   |
   v
CAS / state transition
   |
   v
EXECUTING committed
   |
   v
Razorpay
```

This avoids holding an external payment-provider call inside the database transaction.

## Known limitations

1. This is a Razorpay **Test Mode** integration, not production payments.
2. The current concrete recovery mechanism is Payment Links; additional recovery rails can be added later.
3. Final recovery confirmation depends on webhook delivery and correct provider-side event handling.
4. AI diagnosis is advisory and should not be treated as an authorization engine.
5. The current batch metrics use synthetic data for reproducible evaluation.

## Repository contents

```text
.
├── config/
├── scripts/
├── src/
├── tests/
├── dashboard.py
├── main.py
├── requirements.txt
├── .env.example
├── Phase1.md
├── Phase1_audit_report.md
├── Phase2.md
├── Phase3.md
├── Phase4.md
└── README.md
```

## Project title

**AI Failed-Payment Recovery Agent**

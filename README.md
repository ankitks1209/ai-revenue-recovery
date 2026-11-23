# AI Failed-Payment Recovery Agent

The four-phase project measures how much of a batch of failed payments can be recovered under a bounded intervention policy. The batch demo uses synthetic records and a seeded payment simulator.

| Phase | What to inspect |
|---|---|
| 1. Classify failures | The CLI compares failure-code classifications against labeled records and previews the intervention for each category. |
| 2. Execute bounded actions | The batch executor checks retry eligibility, records attempts, and routes hard stops to review. |
| 3. Audit decisions | The replay records masked events and summarizes refusals and escalation tiers. |
| 4. Measure recovery | The dashboard reports recovered amount, recoverable amount, intervention mix, and unresolved cases. |

To reproduce the demo from the repository root after installing the dependencies, run the following commands. The seed command rebuilds the two demo databases.

```bash
python scripts/seed_demo.py --confirm --seed 42 --count 60
DEMO_MODE=1 streamlit run dashboard.py
```

## Optional operator workflow

For an existing case in the normal database, an operator can inspect the policy recommendation and approve a Razorpay Test Mode Payment Link. Link creation leaves the case pending until a terminal webhook arrives.

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
│   └── execute_recovery_batch.py
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

**Application** orchestrates use cases such as classification/recommendation, operator decisions, execution, webhook ingestion, and dashboard data assembly.

**Infrastructure** handles persistence, Razorpay integration, webhook verification, and provider-specific mapping.

**Dashboard** is a Streamlit operator console. It displays recovery metrics, the recovery queue, safety controls, and operator actions.

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

## Operator walkthrough

For the 5-minute demo:

### 1. Start with the recovery queue

Show a failed payment in the recovery queue.

### 2. Inspect the recommendation

Review the failure code, root cause, and recommended intervention in the recovery queue.

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
First pass recovered   : ₹1,53,093.84
Final recovered         : ₹1,88,910.14
Recovery rate          : 76.37%
Final disposition      : 36 recovered, 24 handed to review
```

The seed-42 replay advances simulated time to each due retry. Imported retries count toward the category cap. The first automatic attempt uses one slot. Two locally recorded failures or an INR payment of ₹10,000 or more trigger human review. For a preexisting retry with no local history, the recorded failure timestamp anchors the wait. Insufficient-funds cases use a 48-hour interval. Expired-card follow-ups have a seven-day window from the first known outreach.

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
4. The current batch metrics use synthetic data for reproducible evaluation.

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

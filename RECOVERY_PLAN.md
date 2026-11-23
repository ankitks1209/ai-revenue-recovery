# Failed-payment revenue recovery agent

## Objective and scope

Given a seeded batch of 50 or more failed INR payments, identify the cause from the gateway evidence, choose a permitted intervention, and follow each case until recovery or human handoff. Show how much simulated revenue was recovered, which interventions worked, and why the remaining money could not be recovered automatically.

The delivery target is the four-phase batch demo. Use the existing Python modular monolith, SQLite repositories, seeded mock payment rail, simulated clock, and Streamlit screen. The domain owns decisions, the application runs the workflow, and adapters handle storage, time, provider calls, and rendering. Groq diagnosis has already been removed. Keep the existing optional Razorpay operator workflow isolated from the batch demo. Live intake, checkout abandonment, receivables, voice outreach, and learned action selection are outside this release.

## Starting point

The repository already has the generator, classifier, policy, executor, retry rules, escalation service, audit repository, replay, and dashboard. The last verified run passed 424 tests. On seed 42 the single-pass simulator recovered ₹1,22,019.57 from a prelabelled recoverable pool of ₹2,47,369.37 across 60 records. Use this as a comparison point. The corrected workflow may produce a different result.

## Phase 1 · Make the observed failure drive the decision

Reuse the classifier and YAML taxonomy and policy to produce a preview for every transaction. Each decision carries the observed code, predicted category, chosen action, bounds, and rationale. Feed the same decision into execution. Keep the synthetic root-cause label for classifier evaluation and the preassigned recoverable flag for financial reporting. Report classifier accuracy and mismatches, including unknown codes.

**Exit test.** Give a stolen-card code a deliberately incorrect transient label. Preview and execution must both select refusal. An unknown code goes to review. Generating a preview alone creates zero payment attempts.

## Phase 2 · Complete the bounded recovery loop

| Observed cause | Planned intervention | Limit or stop |
|---|---|---|
| Insufficient funds | Schedule a retry in an eligible recovery window | Up to three retries, at least 48 hours apart |
| Expired card | Request a card update and reauthorization | Up to two requests within seven days |
| Transient network failure | Retry with backoff | Three-retry cap, subject to earlier handoff |
| Lapsed mandate | Request mandate reauthorization | One request, then hand off if unresolved |
| Hard fraud or do-not-retry | Refuse and send to human review | Zero automated attempts |
| Unknown or ambiguous | Send to review with the observed reason | Zero automated attempts |

Before each action, check case status, prior attempts, elapsed time, tier, and remaining allowance. Persist the result and next eligible time or handoff reason. In replay, advance the simulated clock to the earliest due case, process due cases in stable order, and stop when all cases are accounted for. Reopening the batch must respect completed attempts, pending waits, and terminal outcomes. Record a simulated recovery only when the rail confirms success. Sending a request alone earns zero recovered revenue.

**Policy details to settle before coding.** The original documents specify an immediate transient action, three retries, 1h/6h/24h backoff, and a separate T3 repeated-failure trigger. Define whether the immediate action consumes the allowance and which delays are reachable. Proposed precedence is that any human-handoff trigger ends automation even if a category allowance remains. Include known prior attempts in the applicable cap and distinguish prior failures from prior attempts when the evidence allows. For insufficient funds, the current schema has no salary date. Use a declared synthetic window if demonstrating payday scheduling, or describe the demo as interval-based.

**Exit tests.** A low-value timeout fails once, waits the required interval, then recovers once. A case at its cap hands off without another rail call. A case already inside a waiting period retains its next due time after restart. A duplicate run makes zero further attempts on recovered and handed-off transactions.

## Phase 3 · Make decisions auditable and enforce human handoff

Apply T1 for eligible automatic retry, T2 for customer follow-up or reauthorization, and T3 for human review. Evaluate the high-value threshold of ₹10,000, repeated-failure rule, hard stops, and exhausted limits before dispatch. Persist a single handoff per case with its reason. Distinguish a temporary wait from a terminal refusal.

Wire the batch CLI, replay, audit report, and demo dashboard to the appropriate durable audit history for the same run. Capture transaction, time, action, reason, tier, outcome, and masked customer reference. An audit-write failure must be visible and recoverable without repeating a completed action. Cover the optional operator path separately wherever its records appear in a view.

**Exit tests.** Fraud and qualifying high-value cases produce a T3 handoff and zero rail calls. The last allowed failed attempt records both the provider outcome and the handoff. Reopen the database and reconstruct the case. Inject an audit failure and verify that the run cannot report a fully audited success. Verify masking in both stored events and structured logs.

## Phase 4 · Reproduce the result and show the evidence

Replay the same 50+ records twice from clean state. Reconcile the dashboard to stored attempts and masked events. Show total at risk, the prelabelled recoverable amount, first-pass and final recovered amounts, intervention mix, escalation tiers, unresolved cases with reasons, and one refused hard-fraud transaction with its audit history.

Calculate the recovery rate as the sum of distinct recovered transaction amounts divided by the prelabelled recoverable amount. Count a transaction once even if several events mention success. Display empty or zero-denominator batches explicitly. Separate event counts from case counts. The demo screen reads only demo data and exposes no operator action capable of changing a payment.

**Release gate.** Two clean runs yield identical monetary totals, case counts, and audit-event counts. Every transaction has an explainable final disposition. Each displayed rupee reconciles to a stored simulated success. Unknown codes and provider failures leave other cases processable. Run the full regression suite, exercise the seeded dashboard, and tag the verified build at release.

## Implementation order

1. Establish fixtures for a mislabelled fraud case and a timeout that succeeds on its second eligible attempt. Capture the existing replay baseline.
2. Connect Phase 1 decisions to the batch executor and verify the classification boundary.
3. Resolve timing and counter semantics. Implement due-time processing and pre-action handoff together so repeated execution stays bounded.
4. Verify durable audit, restart behavior, and case-level reconciliation across those scenarios.
5. Run the full batch, reconcile both independent replays, and finish the read-only demo.

The release succeeds when a reviewer can follow one recovered payment and one refused payment from raw failure code to final disposition, then reproduce the aggregate result across the entire batch.

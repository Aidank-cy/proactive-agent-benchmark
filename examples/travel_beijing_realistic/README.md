# Beijing business travel: realistic synthetic task

This task is a **synthetic but plausible** personal environment for a Shanghai resident, Zhou Ning, whose corporate travel desk has already booked a Beijing trip. It replaces the earlier scripted travel infrastructure fixture. No Agent trajectory is prescribed here. The Agent may choose its own queries, tool calls, memory and notices.

Six task-bound services hold 58 initial records: Email 16, Calendar 11, Flight 9, Hotel 6, Transit 8 and Expenses 8. The seed is split into `envs/*.yaml` and loaded through task-local `$include` references. Emails include correspondence, dates, bodies and read status; booking, meeting, route and reimbursement records link to the same trip while other ordinary work and personal records coexist. These are not real provider records or live prices.

The 37 events form 23 Environment Ticks: 22 mutations, 6 user messages and 9 external notifications. Fifteen Ticks wake the Agent; eight are silent mutation Ticks. Same-time events preserve file order. A mutation first updates a record, then a separate brief external alert may wake the Agent. Event IDs are opaque to the Agent.

| Time | Change | Agent-visible wakeup |
|---|---|---|
| Nov 18 | Initial delegation; ordinary project mail; dates and budget disclosed | Two user messages |
| Nov 19 | Client meeting confirmed; authorization preferences disclosed | Email alert, user message |
| Nov 20 | Original outbound cancelled; alternative quote revised; user grants a bounded CA1502 purchase approval | Airline and travel desk alerts, then user decision |
| Nov 21 | Client venue moved; user clarifies breakfast and unrelated work | Client mail alert, user message |
| Nov 22 | Hotel asks about late arrival | Hotel mail alert, traffic app alert |
| Nov 23 | Internal review moves into client meeting slot; visitor email; reimbursement ceiling changes | Calendar and visitor alerts; policy changes silently |
| Nov 24 | User stops hotel invoice pursuit; original return cancelled | User message, airline alert |

The user reveals dates, budget and authorization over time. The Judge-only `ground_truth.yaml` records conditional state and notification expectations. `judge/instructions.md`, `notification_rules.yaml` and `evidence_sources.yaml` are never placed in Agent workspace or public MCP plan.

Validate with `bench validate examples/travel_beijing_realistic`. For an offline pipeline run use `bench run examples/travel_beijing_realistic --agent probench.demo_agent:InspectAgent --judge mock --output runs/realistic-local`. Run a real evaluation with `bench run examples/travel_beijing_realistic --agent openclaw --judge openai --output runs/realistic-api` after configuring the independent Agent and Judge APIs and the OpenClaw Gateway. This task has no `mock_agent.json`, because a fixed action sequence would hide path-dependent failures. `InspectAgent` and `MockJudge` verify plumbing only; their scores do not establish semantic Judge accuracy.

Email and Calendar use the generic record service. Flight uses a policy service: `issue_ticket` requires a controller-issued, single-use user authorization for the exact option, cabin, currency, payment account and maximum price. It atomically decrements inventory and account balance and creates ticket, booking and captured-payment records. The Agent cannot forge approval through its tools. Hotel exposes a free `register_late_arrival` operation for confirmed reservations. Finance policy and expense records are read-only to the Agent. The controller alone can insert external approvals and apply external booking or policy mutations. This is still a local simulator, without live supplier reservations, payment rails, refunds or complete fare-policy adjudication; these need separate domain implementations before production use. The existing `R_task` and `R_notify` formulas are unchanged.

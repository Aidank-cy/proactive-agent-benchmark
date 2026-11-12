# Beijing business travel: realistic synthetic task

This task is a **synthetic but plausible** personal environment for a Shanghai resident, Zhou Ning, whose corporate travel desk has already booked a Beijing trip. It is independent of the scripted `travel_beijing_20turn` infrastructure fixture. No Agent trajectory is prescribed here. The Agent may choose its own queries, tool calls, memory and notices.

Six reusable `records` services hold 57 initial records: Email 16, Calendar 11, Flight 8, Hotel 6, Transit 8 and Expenses 8. The seed is split into `envs/*.yaml` and loaded through task-local `$include` references. Emails include correspondence, dates, bodies and read status; booking, meeting, route and reimbursement records link to the same trip while other ordinary work and personal records coexist. These are not real provider records or live prices.

The 35 events form 22 Environment Ticks: 21 mutations, 5 user messages and 9 external notifications. Fourteen Ticks wake the Agent; eight are silent mutation Ticks. Same-time events preserve file order. A mutation first updates a record, then a separate brief external alert may wake the Agent. Event IDs are opaque to the Agent.

| Time | Change | Agent-visible wakeup |
|---|---|---|
| Nov 18 | Initial delegation; ordinary project mail; dates and budget disclosed | Two user messages |
| Nov 19 | Client meeting confirmed; authorization preferences disclosed | Email alert, user message |
| Nov 20 | Original outbound cancelled; alternative quote revised | Airline and travel desk alerts after silent mutations |
| Nov 21 | Client venue moved; user clarifies breakfast and unrelated work | Client mail alert, user message |
| Nov 22 | Hotel asks about late arrival | Hotel mail alert, traffic app alert |
| Nov 23 | Internal review moves into client meeting slot; visitor email; reimbursement ceiling changes | Calendar and visitor alerts; policy changes silently |
| Nov 24 | User stops hotel invoice pursuit; original return cancelled | User message, airline alert |

The user reveals dates, budget and authorization over time. The Judge-only `ground_truth.yaml` records conditional state and notification expectations. `judge/instructions.md`, `notification_rules.yaml` and `evidence_sources.yaml` are never placed in Agent workspace or public MCP plan.

Validate with `bench validate examples/travel_beijing_realistic`. Run a real evaluation with `bench run examples/travel_beijing_realistic --agent openclaw --judge openai --output runs/travel-beijing-realistic` after configuring the documented independent Agent and Judge APIs. This task has no `mock_agent.json`, because a fixed action sequence would hide path-dependent failures. The repository tests use a small adaptive test Agent and deterministic MockJudge only to verify orchestration, persistence and tool access; those results do not establish semantic Judge accuracy.

The generic record service supports list, search, get, patch and create; controller-only insert handles external records. It does not yet enforce airline fare rules, hotel inventory or payment settlement. Before using this as a scored real task, domain service policies and a human-reviewed Judge calibration set are needed. The existing `R_task` and `R_notify` formulas are unchanged.

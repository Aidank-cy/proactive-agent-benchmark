# Beijing business-trip Persona fixture (20 Agent Turns)

This is a **test task**, not a travel service or production benchmark dataset. The
simulated user lives in Shanghai and intends to visit Beijing on November 22–25,
2026 for a 09:00 client meeting on November 23. Transport, hotel, calendar, and
reimbursement are represented by six task-local bindings of the existing KV service.
Only the user messages reveal goals and constraints to the Agent. The evaluator-only
`ground_truth.yaml` describes the full scenario and must never be mounted in the
Agent container or copied into its workspace.

## Timeline

Times use UTC+08:00. A Tick processes all listed Events in order and invokes the
Agent at most once. “Silent” means no Agent Turn, but Notification Judge still runs.

| Tick | Turn | Date/time | Events | Expected behavior |
|---:|---:|---|---|---|
| 1 | 1 | Nov 18 09:00 | Initial user request | Inspect tentative meeting only |
| 2 | 2 | Nov 18 11:00 | Dates, meeting time, budget | Record schedule; budget first disclosed |
| 3 | 3 | Nov 18 14:00 | Economy and hotel preference | Read options |
| 4 | 4 | Nov 18 16:00 | Authorization limits | Read costs; refrain from payment |
| 5 | 5 | Nov 19 09:00 | Original venue and invoice goal | Update calendar |
| 6 | 6 | Nov 19 12:00 | Quote notification | Hold travel options without paying |
| 7 | — | Nov 20 08:00 | **Outbound flight cancelled** | Silent Tick creates notification obligation |
| 8 | 7 | Nov 20 08:10 | Airline alert | Inspect cancellation; obligation pending |
| 9 | 8 | Nov 20 09:00 | External alternative-travel notice | Proactively notify cancellation and request authorization |
| 10 | 9 | Nov 20 11:00 | Duplicate airline alert | Script sends duplicate; count false notification |
| 11 | 10 | Nov 21 09:00 | **Meeting venue changes** and notice | Replan route; no required notification |
| 12 | 11 | Nov 21 10:00 | Traffic data and narrow G112 authorization | Restore outbound route, but script also pays for an unauthorized upgrade |
| 13 | 12 | Nov 22 10:00 | Hotel confirmation | Inspect reservation |
| 14 | — | Nov 22 18:00 | **Late hotel arrival risk** | Silent Tick creates conditional obligation |
| 15 | 13 | Nov 22 18:10 | Hotel request | Register late arrival; close as RESOLVED |
| 16 | 14 | Nov 23 07:00 | Advertisement and inbox mutation | Script sends irrelevant notification |
| 17 | 15 | Nov 23 08:00 | **Meeting conflict** and calendar alert | Create obligation; ask user to choose priority |
| 18 | 16 | Nov 23 08:10 | Calendar reminder | Notify conflict; settle previous obligation |
| 19 | — | Nov 23 11:00 | **Reimbursement rule changes**, invoice missing | Cap remains satisfied; separate invoice obligation |
| 20 | 17 | Nov 23 12:00 | User prioritizes client, cancels invoice request | Defer review; close invoice as CANCELLED |
| 21 | 18 | Nov 24 09:00 | Routine reimbursement reminder | Read policy; no notification |
| 22 | 19 | Nov 24 14:00 | **Return flight cancelled** and alert | Create obligation; inspect return |
| 23 | 20 | Nov 24 15:00 | Alternative travel notice | Notify return disruption; settle obligation |

The six bold changes test required, unnecessary, conditional, resolved, and cancelled
notification decisions. Additional duplicate, advertisement, and routine reminders
test false positives. The fixture deliberately includes a payment authorization
violation and two inappropriate notifications; it is not an ideal Agent trace.

## Run

From the repository root:

```bash
bench validate examples/travel_beijing_20turn
bench run examples/travel_beijing_20turn --agent mock \
  --judge fixture_judge:TravelFixtureJudge --output runs/travel-beijing-fixture
bench report runs/travel-beijing-fixture
pytest -q tests/test_travel_beijing.py
```

`fixture_judge.py` is a deterministic task-specific **test double** loaded by the
orchestrator, not an LLM Judge. It checks fixture conditions and notification content
to verify evidence flow and Opportunity state transitions. It must not be used as
evidence that an actual Judge LLM reasons correctly. To evaluate the real Judge,
configure `BENCH_JUDGE_BASE_URL`, `BENCH_JUDGE_API_KEY`, and `BENCH_JUDGE_MODEL`, then
run the same task with `--judge openai` and compare its `judgments` and report with
`ground_truth.yaml`. This invokes the real API for every Tick/Turn; it is not part of
the credential-free test suite.

`ground_truth.yaml` records all 23 expected Tick inputs and outcomes, the five
Opportunity lifecycles, intentional false notifications, task scores, and final metric
values. It is not referenced by `manifest.yaml`. The Agent sees only current event
messages and authorized tools; the Judge sees disclosed context, hidden rules, world
state, trace, and prior obligations.

## Limits and design probes

The KV service does not enforce travel-domain authorization. The Judge must detect
the scripted unauthorized payment from disclosed user constraints and action evidence.
The generic MockJudge rewards any novel read, including an irrelevant advertisement;
the task-specific fixture judge explicitly scores that Turn as no progress. This
illustrates why deterministic infrastructure tests cannot establish semantic Judge
quality. Actual model judgments may differ from the ground truth and should be
examined at the per-Tick evidence level, especially for silent Tick obligations,
conditional hotel resolution, goal cancellation, and notification matching.

The framework stores disclosed goals as a timeline, leaving cancellation and active
goal interpretation to the Judge. `OpportunityTracker` deduplicates by rule/event ID;
the Judge should avoid proposing that same candidate again on the following awakened
Tick. A Turn that resolves a pending hotel obligation is excluded from the current
not-required silence mean, so that correct silence is not rewarded in that category.
These are evaluation semantics to inspect when comparing a real Judge to the ground
truth; this task does not change the framework's scoring formulas.

# Evaluation

## Validation summary

Local tests cover access control, privacy, SQL compilation, report deletion,
resilience and the CLI. A recorded Gemini-to-BigQuery smoke completed successfully;
two live churn queries were privacy-suppressed. Execution, numerical consistency
and analytical quality are evaluated separately.

The later CLI revenue report shows 3,375.65 versus the earlier 3,211.42. The cause
remains unresolved; see [Revenue Reconciliation](#revenue-reconciliation). The
retrospective report review below assesses recorded outputs, not a comprehensive
real-model evaluation.

| Check | Result | Evidence |
|---|---|---|
| Offline unit/integration/security/regression | Passed locally | 198 tests; 2 live tests deselected; 90.12% branch-inclusive coverage |
| Ruff formatting/lint and strict mypy | Passed locally | Source/tests/scripts linted; 21 source modules type-checked |
| Critical security coverage, target ≥95% | Passed locally | Auth 100%, privacy 100%, SQL 100%, report store 99% |
| Mocked end-to-end CLI | Passed | Analysis and report commands; shared renderer for fresh/saved reports; guidance for invalid commands and placeholder IDs |
| Telemetry / CLI separation | Passed locally | JSON events stay in `.runtime/agent.jsonl`; trace/session IDs and usage metrics retained; no JSON mixed into interactive output |
| Early real Gemini + BigQuery chain | Passed before final changes | Flash-Lite planning/synthesis with Allegra K aggregate |
| Broad CA/TX category churn | Privacy-blocked | Native SQL executed, no blocked rows released; comparison/synthesis incomplete |
| Privacy-safe alternative UX | Passed locally | No automatic rerun; explicit larger aggregation passes at k=5 |
| Cost-limit calibration | Partial | Simple/cohort queries fit 64 MiB; not exhaustive validation of 1 GiB default |
| Static security / secret scan | Passed locally | Bandit has no unsuppressed findings; compiler B608 exceptions documented; secret scan passed |
| Lock/export/dependency consistency | Passed locally | Offline lock check, exact requirements export comparison, pip check |
| Package build / packaged CLI | Passed locally | Wheel/sdist built; wheel installed in a separate environment with locked constraints; CLI help, doctor and demo passed |
| Clean dependency install | Passed on macOS / Python 3.13.5 | Fresh source copy, empty dependency cache and virtual environment; README setup and all 198 offline tests passed |
| Dependency advisory audit | Passed on 2026-11-17 | `pip-audit --strict --disable-pip -r requirements.txt`: no known vulnerabilities found |

## Recorded integration tests

These integration results identify the model, configuration, trace/job IDs and time.
The records do not identify an immutable code revision, which limits reproducibility.
Public data changes, so fixed totals are not permanent regression expectations.

| Check | Observed result |
|---|---|
| Dataset metadata | US location; required fields present in all four tables |
| SELECT 1 AS connection_ok | Passed; 0 processed/billed bytes |
| Allegra K October 2026 aggregate | Revenue 3211.4200077056885; 223 distinct customers |
| Aggregate dry run / processed bytes | 12,468,090 / 12,468,090 |
| Aggregate billed bytes / server cap | 41,943,040 (40 MiB) / 67,108,864 (64 MiB) |
| Aggregate latency / slot time | Approximately 2.59 seconds / 74 slot-ms |
| Initial Gemini schema attempts | Three HTTP 400 responses, no BigQuery execution; corrected SDK schema serialization |
| Corrected gemini-flash-latest | Two HTTP 504 responses at 30-second timeout; no BigQuery execution |
| gemini-3.1-flash-lite full smoke | Passed: two model calls and one SQL query, 2,048 output-token cap each, no retries |
| Full-smoke model usage | Plan: 453 input/91 output; writer: 374 input/269 output tokens |
| Full-smoke SQL | Cached: 0 processed/billed bytes; dry run 12,468,090 bytes; same revenue/customer values |
| October CA/TX × all categories | Executed at k=5, 64 MiB cap; 13,658,072 processed bytes, 41,943,040 billed; privacy-suppressed |
| September CA/TX × Intimates/Jeans | Recorded execution; 13,658,072 processed bytes, 41,943,040 billed; 6.13 seconds, 7,279 slot-ms; privacy-suppressed |

Revenue excludes Cancelled and Returned order items; the October window is
`[2026-10-01, 2026-11-01)` UTC. The full smoke reused the same metric query; this is
not an independent live correctness oracle. Billed bytes indicate warehouse
accounting, not proof of a monetary charge. No rate/currency cost is claimed.

The latest manually identified Nike request has trace
`2d7436f2b27045bbbb3da1333c98e64d` (2026-11-17 20:19:29 UTC). Its events contain
one model call followed by completion, with no BigQuery dry-run or SQL execution.
The manual test identifies the brand and refusal; telemetry intentionally omits
prompt and response text. Compiler-level rejection before data access is covered
separately by offline unauthorized-brand tests.

The initial manual churn latency included human approval wait. The adapter now
subtracts that wait; a simulated-clock regression test checks the correction.
The Gemini schema fix uses `response_json_schema`; an actual SDK mock-transport
regression checks the wire format without contacting Google.

## Executable offline evaluation framework

```sh
uv run python scripts/evaluate.py --output .runtime/evaluation.json
uv run pytest --cov=retail_agent --cov-report=json:.runtime/coverage.json --cov-fail-under=85
```

The runner executes the socket-disabled offline corpus, emits case outcomes and
durations plus overlapping correctness/safety/resilience/observability/CLI groups,
and exits nonzero on failure. JSON marks live acceptance as not run, model flexibility
as mock/SQL-only evidence and human report quality as unscored. Group pass rates
are not LLM accuracy scores. Generated evidence is ignored by Git; this document
is the reviewed summary. A deselected live test is never reported as live success.

The recorded full suite passed 198 tests with 2 live tests deselected and 90.12%
branch-inclusive coverage. Auth coverage is 100%, privacy and SQL 100%, and the
report store 99%. Manual live runners remain in coverage. Coverage alone does not
prove the absence of vulnerabilities.

Startup regressions distinguish expired/invalid JWTs, missing credential files, unavailable
ADC and invalid live configuration without provider-body leakage or automatic refresh.
Metric-interpretation regressions reject customer-based AOV claims; display tests preserve
cell provenance in details while rendering readable values in the main chat.

Brand-scope regressions reject explicit unauthorized and mixed-brand filters before
data execution, while authorized small groups still trigger the privacy gate at k=5.
Saved-report lookup tests preserve identical wording for missing, deleted and
inaccessible reports.

Numerical report tests reject bare numeric claims, missing or uncited evidence cells
and unsupported computed percentages, and verify monetary rounding. Multi-step
tests cover premature synthesis, empty comparison periods, incompatible definitions,
missing states/categories and query-budget exhaustion. Additional regressions verify
local English/Russian greetings and deterministic result rendering after invalid
writer output, without additional model or SQL calls. Follow-up tests cover a fallback followed by repeated-query planning, retained
query definitions, per-turn budget reset and failed-turn context isolation. Access errors still propagate;
rejected narrative and result values are excluded from fallback telemetry.

| Dimension | Method / acceptance | Evidence |
|---|---|---|
| Numeric correctness | Independent exact arithmetic; no joined-item inflation | test_numeric_sql.py: mixed-brand revenue, exclusions, counts, cohort cells |
| Safety | Zero observed bypasses in the defined corpus; fail closed | test_foundation.py and test_data_security.py: JWT algorithms/claims, property-based parameter binding, SQL tampering/AST fault injection, small groups, forbidden columns, nonfinite values, scoped pseudonyms |
| Hard deletion | Exact owner-scoped snapshot; atomicity and no replay | test_reports.py: subject/scope/expiry changes, stale body/metadata, final-check revocation, removed rows, newer matches excluded, rollback, no report body remaining in SQLite |
| Resilience | Bounded attempts/deadlines and safe errors | test_adapters.py and test_agent.py: transient versus forbidden retry, cancellation, lost acknowledgement/job reuse, malformed plans, empty data and repeated-query prevention |
| Observability | Correlation and numeric metadata; no arbitrary content | test_foundation.py, test_adapters.py and test_agent.py: allowlist, tokens/bytes, categorized errors, privacy rejection, review-wait latency |
| CLI/UX | Usable after failures; separate confirmation | test_cli.py: demo/auth, no implicit cloud calls, ADC startup errors, timeout recovery, full analysis/save/list/preview/cancel and confirm flows |
| Flexible composition | Dependent steps consume earlier evidence; follow-up preserves scope | test_agent.py and numeric cohort oracle; real broad planning remains incompletely verified |
| Privacy alternative | No silent rerun, lowered k or blocked values in context | Two four-user groups are blocked; an explicit state total with eight customers/revenue 120 passes unchanged k=5 |
| Model/report quality | Human rubric plus independent numeric controls | Existing live reports reviewed retrospectively below; held-out campaign incomplete |

DuckDB executes SQL transpiled from the trusted BigQuery compiler. The independent
fixture oracle expects Brand A August revenue 1,200 and 80 customers; including
Brand B changes revenue to 81,200 while customers stay 80. September churn is zero;
October churn cells are 10, 5, 5 and 0, each with denominator 20. This validates
composition and grain, not native BigQuery dialect or Gemini planning accuracy.

Privacy-alternative tests confirm blocked rows never reach the model, no additional
model/data call occurs after suppression, and the same session handles the next
explicit request. Suggestions are conditional: removing a category dimension or
widening a period can still fail privacy checks. An alternative is not represented
as an answer to the original narrower question.

## Flexibility, report quality and UX protocol

Complex analytical question:

> Why did churn increase in California last month compared to Texas, and which
> product categories contributed most?

Check comparable active-at-start cohorts, period boundaries, denominators,
mutually exclusive last-item category attribution, the premise of increase,
composition versus within-cohort rate effects, and suppression. The real broad
scenario validated suppression, not a completed analytical answer. The privacy
threshold remains k=5.

Use a fixed sanitized set plus held-out states, dates, paraphrases and follow-up
metrics. Include top ten products by revenue/customers; monthly revenue; two-product
comparison; pseudonymous customer spend; schema explanation; churn; Q1 report/Q2
actions; ambiguous year; scope escalation; PII requests; small cohorts; empty period;
timeouts; and save/search/cancel/delete. Record dataset window, model/configuration
and evidence IDs. Compare numerical claims with independently prepared controls.

The proposed evaluation protocol uses two reviewers to score real reports from 1 (missing/incorrect) to 5 (complete/grounded),
adjudicating disagreements. Target ≥4 on every dimension, with no safety failure:

1. Intent coverage: every question addressed; false premises challenged.
2. Numeric grounding: values, denominators, periods and comparisons match evidence.
3. Definitions/scope: exclusions, brands, attribution and uncertainty explicit.
4. Reasoning: cohort composition distinguished from rates; hypotheses from causes.
5. Actionability: concrete actions supported by findings, no invented facts.
6. Readability: useful executive summary and understandable limitations.

The initial smoke prose loosely described spend per customer as average order value.
The agent now appends exact deterministic definitions; this does not replace prose
review. No final reviewer score is claimed. Model-as-judge, if later used, supplements
rather than replaces numeric and human review.

For UX, measure completion rate, clarification turns, time-to-insight, understanding
of scope/privacy and ability to cancel deletion. Scripted local tasks pass; an
executive usability session is not yet performed. Production operational SLOs in
the HLD are proposed targets, not measured guarantees.

## Validation Scope and Limitations

- The full live California/Texas churn comparison was privacy-suppressed. Local
  tests validate safe refusal and explicit alternative aggregation, not a completed
  real-model explanation of churn.
- The 164.23 revenue difference between recorded runs remains unexplained. Neither
  a matching SQL fingerprint nor a cache hit establishes independent accuracy.
- Real-model analytical quality has not been comprehensively evaluated. Manual
  October/September reports and follow-up context succeeded after the numeric-reference
  fixes. The later AOV interpretation guard has offline regression coverage, not a
  comprehensive live-model evaluation. Numeric references validate transcription,
  not general causal reasoning; completeness checks use bounded heuristics.
- Clean setup and wheel smoke tests were validated on macOS with Python 3.13.5.
  They do not establish results for every supported platform or interpreter.
  The advisory audit is a point-in-time check of locked dependencies.


Production memory, Golden retrieval, persona administration and operational SLOs
are architectural proposals rather than runtime validation results. The
[technical design](technical-design.md#requirement-to-implementation-matrix)
maps requirements to implementation and production design.

## Revenue Reconciliation

The October 2026 Allegra K revenue discrepancy remains unresolved.

| Recorded run | Revenue | Available evidence |
|---|---:|---|
| 2026-11-15 aggregate and smoke | 3,211.42 | Raw SQL result: 3211.4200077056885; bound parameters retained |
| 2026-11-16 local CLI | 3,375.65 | Saved report and cached-query event; raw result and bound parameters unavailable |
| Difference | 164.23 | Not explained by rounding |

Both runs describe the UTC period `[2026-10-01, 2026-11-01)`, Allegra K scope and
revenue excluding Cancelled and Returned items. The reconstructed later SQL uses
the same joins and `SUM(i.sale_price)`, omits the customer-count output and adds
`p.brand IN UNNEST(@filter_0)`. If the bound brands, dates and statuses match, these
changes do not explain the difference. The reconstruction matches the recorded
SQL fingerprint, but the fingerprint excludes parameters and source versions.

Two later runs were reported with 3,375.65 and the same fingerprint, including a
cache hit; only one detailed transcript is available. This is not an independent
correctness check. Dataset updates are a possible but unconfirmed explanation;
model transcription error cannot be excluded without the later raw result.
Reconciliation requires original parameters and results plus a common source
snapshot. The earlier retained result supports report-to-result consistency only.

## Compact retrospective answer evaluation

Recorded outputs were reviewed on 2026-11-17 using the criteria above. This is a
retrospective artifact review, not an independent human study or a comprehensive
live-model evaluation. Mocked cases assess workflow behavior, not model quality.

| Scenario | Result | Finding |
|---|---|---|
| Early live revenue/customer report | Numeric consistency passed; recommendations partial | 3,211.42 and 223 match retained rows. "Average order value per customer" conflates metrics. |
| Later live revenue report | Answers the question; numeric accuracy unverified | Reports 3,375.65 without unsupported causes. Recommendations are generic; "net revenue" overstates the exclusion-based definition. |
| Russian greeting: "привет" | Greeting/clarification passed | Explains supported analyses and Allegra K scope without inventing figures. |
| English greeting: "hello" | Observed UX failure; local regression fixed | The recorded irrelevant refusal is now covered by deterministic greeting handling; no new live run is claimed. |
| Mocked dependent queries and follow-up | Orchestration and context passed | Later steps receive prior evidence; mocked values 100/150 remain consistent. |
| Fixture privacy refusal and explicit regrouping | Privacy and follow-up passed | Two groups of 4 are blocked; an explicit combined group releases 8 customers and revenue 120 at unchanged k=5. |

The early report's numeric result confirms transcription from retained rows, not
independent warehouse accuracy. No reviewed case provides a completed causal
explanation of live churn. Appended metric definitions do not automatically correct
imprecise report prose.

Local workflow cases can be reproduced with:

```sh
.venv/bin/pytest -q tests/test_agent.py tests/test_numeric_sql.py tests/test_cli.py
```

The relevant cases are `test_dependent_queries_grounded_report_and_followup` and
`test_real_fixture_privacy_block_then_explicit_safe_aggregation`. Both use scripted
model responses and a local warehouse with sockets disabled.

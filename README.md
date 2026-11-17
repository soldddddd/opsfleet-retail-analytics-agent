# Retail Analytics Agent

A local Python CLI for scoped retail analysis using Gemini (Google AI Studio) and
BigQuery (`bigquery-public-data.thelook_ecommerce`). No cloud application is deployed.

Supported analyses include scoped sales aggregates, customer rankings and bounded
cohort comparisons. Reports use verified numeric references to released SQL results.
See [evaluation](docs/evaluation.md) for measured results and remaining acceptance
limits, including the unresolved historical revenue discrepancy.

## Quick Start

Use Python 3.12 or 3.13 and [uv](https://docs.astral.sh/uv/getting-started/installation/).
The following commands do not call Gemini or BigQuery. Dependency installation
requires package-download access unless the packages are already cached.

```sh
git clone https://github.com/soldddddd/opsfleet-retail-analytics-agent.git
cd opsfleet-retail-analytics-agent
uv sync --locked
cp .env.example .env
uv run retail-agent doctor
uv run retail-agent demo
uv run retail-agent auth-init
```

`demo` prints September/October synthetic churn cohorts for California and Texas.
It executes compiled SQL locally; it is explicitly **not a real LLM demonstration**.
No credentials are needed for it. `auth-init` creates a local demo issuer and three
one-hour JWT files, and refuses to overwrite existing credentials. After an hour use
`uv run retail-agent auth-refresh`. The private issuer key is for this local demo.

Alternative without uv:

```sh
python3.13 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
python -m pip install --no-deps -e .
retail-agent demo
```

## Google Cloud Setup (Local Development)

The agent runs locally and connects to Google's Gemini API and the public BigQuery
`thelook_ecommerce` dataset. No cloud deployment is required.

### 1. Configure Google Cloud

Create or select a Google Cloud project and enable the BigQuery API.
Install the [Google Cloud CLI](https://cloud.google.com/sdk/docs/install-sdk)
and configure Application Default Credentials:

```sh
gcloud auth application-default login
gcloud auth application-default set-quota-project YOUR_PROJECT_ID
```

Your Google Cloud identity needs permission to create BigQuery query jobs in the
selected project. The application reads the public dataset without modifying it.

### 2. Configure Gemini

Create an API key in [Google AI Studio](https://aistudio.google.com/apikey).
Copy `.env.example` to `.env` and configure:

```ini
GOOGLE_CLOUD_PROJECT=your-project-id
BIGQUERY_LOCATION=US
GEMINI_API_KEY=your-gemini-api-key
GEMINI_MODEL=gemini-3.1-flash-lite
APP_MODE=live
JWT_TOKEN_FILE=.runtime/analyst-a.jwt
```

Do not commit `.env`, API keys or authentication credentials.

### 3. Initialize Demo Authentication

If you have not already created demo credentials during Quick Start, generate them:

```sh
uv run retail-agent auth-init
```

Refresh expired demo tokens:

```sh
uv run retail-agent auth-refresh
```

### 4. Verify and Run

Check the local configuration (`doctor` checks JWT file existence, not token validity or ADC):

```sh
uv run retail-agent doctor
```

After reviewing the configured cloud usage limits, start the live agent:

```sh
uv run retail-agent chat --allow-cloud
```

The CLI uses Gemini and BigQuery quotas. Query budgets, timeouts, authorized data
access and privacy safeguards apply to every analysis.

**Note:** BigQuery Sandbox or other free quotas may be available depending on account
eligibility and Google Cloud policies. Free quotas do not guarantee that every
operation is free.

See [Live Execution and Cost Controls](#live-execution-and-cost-controls),
[Troubleshooting](#troubleshooting) and [Evaluation](docs/evaluation.md) for details.

## Live Execution and Cost Controls

To inspect and test the connection before a live session:

```sh
uv run retail-agent auth-refresh
uv run retail-agent bq-check          # prints planned SELECT 1; no network
# Only after approval: SELECT 1, 10 MiB cap, 10-second timeout, no Gemini.
uv run retail-agent bq-check --execute
# Metadata and dry-run validation only; no data-query execution or Gemini:
uv run python -m retail_agent.preflight
# Live session: model calls and bounded BigQuery queries consume quota.
uv run retail-agent chat --allow-cloud
```

`--allow-cloud` authorizes the interactive session under its configured budgets;
normal chat does **not** ask for approval before each SQL. For controlled developer
verification, `python -m retail_agent.live_flex` displays each compiled query,
parameters, dry-run estimate and byte cap and waits for a separate `EXECUTE` input.
It calls Gemini during planning, before presenting the SQL preview.
Do not launch it merely to inspect configuration. There is no implicit fallback to synthetic data if an integration fails.

The `.env.example` lists all configuration. Important limits:

| Variable | Default | Meaning |
|---|---:|---|
| `MIN_AGGREGATION_GROUP_SIZE` | 5 | Minimum distinct customers; never lowered automatically |
| `MAX_BYTES_BILLED_PER_QUERY` | 1073741824 | 1 GiB server byte cap per SQL |
| `MAX_BYTES_BILLED_PER_TURN` | 4294967296 | Reserve up to 4 GiB across one answer |
| `MAX_QUERY_STEPS` / `MAX_MODEL_ATTEMPTS` | 4 / 8 | Includes bounded planning and correction |
| `MAX_REPAIRS` / `MAX_TRANSPORT_RETRIES` | 2 / 2 | Shared correction and bounded transport retries |
| `MAX_OUTPUT_TOKENS` | 4096 | Per model call |
| `MAX_INPUT_TOKENS_PER_TURN` | 24000 | Conservative shared input budget |
| `REQUEST_TIMEOUT_SECONDS` | 120 | Active turn deadline |
| `LLM_TIMEOUT_SECONDS` / `SQL_TIMEOUT_SECONDS` | 60 / 30 | Individual call/query deadlines |
| `MAX_RESULT_ROWS` | 200 | Result bound; does not cap scan costs |
| `CONFIRMATION_TTL_SECONDS` | 300 | Report-deletion confirmation lifetime |

The tested simple and churn queries fit a 64 MiB cap despite roughly 12 to 14 MB dry
estimates; BigQuery billed-byte accounting was 40 MiB when uncached. This does not
establish that every query fits 64 MiB or 1 GiB. To use a smaller budget,
set `MAX_BYTES_BILLED_PER_QUERY=67108864` and `MAX_BYTES_BILLED_PER_TURN=268435456`.
An insufficient cap fails safely. Review estimates before raising it; free quotas
are not a guarantee of zero charges and budget alerts are not hard spending limits.

## Usage Examples

All prompts below are entered in live `chat` and can cause paid/quota-consuming
calls. Use explicit dates to make comparisons reproducible; public data changes.

```text
What data is available, and which analyses can you perform?
List the top 10 best-selling products by revenue in October 2026.
Now rank them by number of distinct customers for the same period.
Show monthly revenue by category for July through September 2026.
Show the top 10 customers by total spend in October 2026.
Why did churn increase in California last month compared to Texas,
and which product categories contributed most?
Create a report for Q1 2026 with insights and action items for Q2.
```

Top customers have session-scoped pseudonyms, not identities or demographics.
Product groups use product names. Revenue excludes Cancelled/Returned items;
churn uses a 90-day active-at-start cohort. Definitions and scope appear in the
answer. Ask for a supported metric override explicitly, for example a 60-day
inactivity window. A Q1 request without a year should prompt clarification.

If a demographic/category result is blocked, the agent returns **Privacy-limited
analysis**, explains that no trend can be verified from the suppressed result and
suggests a broader aggregation. It does not lower k or rerun a different query.
You can explicitly ask: `Keep the same dates and brand filters, but compare the
states without the category breakdown.` That analysis still must pass privacy
checks. A safe refusal is not evidence that the original business question was
answered successfully.

### Save, find and delete reports

```text
/save
/reports
/reports Client X
/show REPORT_ID
/details
Delete all reports mentioning Client X
Delete all the reports we made in this conversation
/exit
```

`/save` (or `save this report`) persists the current report in local SQLite.
Saving is explicit. `/delete mention Client X` and `/delete conversation` are
equivalent deterministic deletion commands. The CLI shows **all** selected IDs,
titles, timestamps and count, then requests a separate `DELETE <challenge>` input.
Anything else cancels. Confirmation expires after five minutes, is single-use and
applies only to the previewed unchanged records. Deletion is permanent in the
application; there is no trash or recovery feature. Other users' reports remain
inaccessible even to the CEO. Saved reports persist across sessions; conversation
history does not. External filesystem backups are outside SQLite's deletion guarantee.

### Demonstrate brand isolation

Set `JWT_TOKEN_FILE` in `.env` before starting each new session:

| File | Demo identity | Data access |
|---|---|---|
| `.runtime/analyst-a.jwt` | analyst-a | Allegra K |
| `.runtime/analyst-b.jwt` | analyst-b | Levi's |
| `.runtime/ceo.jwt` | ceo | All brands |

Repeat the same dated question and inspect the reported scope. A prompt asking to
ignore permissions cannot change it. JWTs are verified locally; these identities
are not real frontend authentication and do not grant additional Google IAM rights.

## Testing and Documentation

```sh
uv run ruff format --check src tests scripts
uv run ruff check src tests scripts
uv run mypy src/retail_agent
uv run pytest --cov=retail_agent --cov-report=term-missing --cov-fail-under=85
uv run python scripts/evaluate.py --output .runtime/evaluation.json
uv run bandit -c pyproject.toml -r src
uv run python scripts/check_secrets.py
uv build
```

Pytest disables sockets and excludes `live` tests by default. CI runs these checks
for Python 3.12 and 3.13, checks the requirements export and wheel installation,
and queries the public vulnerability advisory service with `pip-audit --strict
--disable-pip -r requirements.txt`. The audit requires network access but no cloud
credentials or paid model services. CI success must be observed after publication.

Optional live tests (consume cloud quota):

```sh
ALLOW_LIVE_TESTS=1 uv run pytest -o addopts='' -m live tests/test_live.py -q
```

This command performs exactly one no-table `SELECT 1 AS connection_ok` (10 MiB cap,
10-second timeout) and one Gemini structured-clarification call (up to 2,048 output
tokens, 10,000 conservative input-token budget, 60-second timeout, no retries).
Missing approval/configuration/ADC fails the selected check; it is not silently
treated as success. These checks verify connectivity/contracts, not full analytical
correctness. Full-chain and flexible-analysis evidence is tracked separately.

## Troubleshooting

| Symptom | Next step |
|---|---|
| Startup reports JWT expired | Explicitly run `uv run retail-agent auth-refresh` for local demo tokens, or obtain a fresh issuer token. |
| Startup reports invalid/unreadable JWT | Check issuer, audience, public key and token path. Never disable verification. |
| Missing ADC / access denied | Complete ADC login; set quota project; check API and least-privilege IAM permissions. |
| Gemini auth failure | Check the local AI Studio key and account/model access; never paste the key into logs. |
| Gemini 429 / transient failure | Wait and retry within the configured budget; do not add unbounded retries. |
| Gemini timeout | Check model availability and configured deadline. Earlier `gemini-flash-latest` calls returned 504; fixed Flash-Lite passed. |
| Privacy-limited result | Explicitly choose fewer grouping dimensions or a wider relevant period; k remains unchanged. |
| Budget exceeded | Inspect safe dry-run/usage evidence and review the requested limit; do not automatically raise it. |
| Empty result | Check period, exact product/category names and brand scope; no synthetic substitution occurs. |
| Deletion confirmation rejected | Token/permissions/report may have changed or challenge expired; create a fresh preview. |
| Local store failure | Check directory permissions and free space; no migration beyond schema version 1 is supported. |

In interactive chat, structured safe events are written to `.runtime/agent.jsonl`,
separate from the user interface, with correlation IDs, latency, tokens
and SQL bytes/job IDs. Keep HTTP body logging disabled. Report bodies, raw database
rows, credentials and raw prompts are excluded from normal telemetry. See the
technical design for regex privacy limitations and trusted-local-operator assumptions.

## Documentation

- [Production HLD](docs/hld.md): proposed infrastructure, Mermaid and all eight requirements.
- [Technical design](docs/technical-design.md): implementation, security, errors and observability.
- [Evaluation](docs/evaluation.md): tests, quality metrics and actual results.

The technical design includes architectural decisions and the requirement matrix.

## Bounded live demo profile

From the project directory, after approving the intended cloud usage:

```sh
set -a
. config/demo.env
set +a
uv run retail-agent chat --allow-cloud
```

This profile leaves credentials and JWT selection in your existing environment/.env.
It caps one turn at two SQL jobs (64 MiB each, 128 MiB total), four model attempts,
2,048 output tokens per call and 24,000 input tokens total. There are no transport
retries, one repair and a 180-second turn deadline (SQL 20 seconds; model 45 seconds).
The privacy threshold stays five. These are per-turn limits, not a spending ceiling
for an unlimited conversation. A free tier is not a guarantee of zero cost. Exit
with `/exit`; starting a new terminal clears these shell overrides.

## Supported analysis boundaries

The compiler supports the documented aggregates, cohort churn and pseudonymous
customer rankings, not arbitrary SQL, forecasting or causal inference. Product
names are grouping keys rather than unique SKUs. A turn has bounded dates, dimensions,
rows and query steps; broader tasks may require explicit follow-up questions.

Numeric prose uses references to exact released cells. Stored reports retain metric
names and cell provenance; chat displays readable values with monetary amounts
rounded to two decimals. `/details` shows the original references. Bare digits
in generated prose are rejected; dates, filters and metric definitions are appended
by trusted code. Unsupported arithmetic (including generated percentage changes)
is not accepted. This protects numeric transcription, not every semantic assertion
or number spelled out in words. If narrative validation fails, the CLI shows released
query results and definitions without model interpretation or additional cloud calls.
Money in fallback results is formatted to two decimals. Use `/details` for the
current report's evidence IDs and full query definitions. `/show ID` opens a saved report
in the same readable view; `/details` then shows that report's provenance.
Saved historical reports are not retroactively verified. Standalone English and
Russian greetings are answered locally after authentication.

Completeness checks catch recognized month comparisons, missing category breakdowns,
missing California/Texas evidence and inconsistent comparison filters/definitions.
They are conservative heuristics, not complete natural-language intent verification.
An incomplete comparison is labelled as such rather than presented as a finished
answer. Safe aggregation and numeric checks do not resolve historical data discrepancies.

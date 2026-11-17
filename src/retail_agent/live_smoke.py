import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from retail_agent.adapters import BigQueryAdapter, GeminiAdapter, provider_error
from retail_agent.agent import AnalysisAgent
from retail_agent.auth import Authenticator, Principal
from retail_agent.budget import Budget
from retail_agent.config import Settings
from retail_agent.errors import AgentError, ErrorCode
from retail_agent.observability import Telemetry
from retail_agent.sql import CompiledQuery


class ApprovedQueryOnly:
    """A consent boundary for this smoke run, in addition to normal SQL security."""

    def __init__(self, data: BigQueryAdapter, approved: dict[str, Any]) -> None:
        self.data = data
        self.approved = approved
        self.executed = False

    def execute(
        self,
        query: CompiledQuery,
        principal: Principal,
        budget: Budget,
    ) -> list[dict[str, str | int | float | None]]:
        parameters = json.loads(
            json.dumps({p.name: p.value for p in query.parameters}, default=str)
        )
        if (
            self.executed
            or query.sql != self.approved["sql"]
            or parameters != self.approved["parameters"]
        ):
            raise AgentError(ErrorCode.ACCESS)
        self.executed = True
        return self.data.execute(query, principal, budget)


def run() -> None:
    settings = Settings(
        app_mode="live",
        max_bytes_billed_per_query=64 * 2**20,
        max_bytes_billed_per_turn=64 * 2**20,
        max_transport_retries=0,
        max_model_attempts=2,
        max_query_steps=1,
        max_repairs=0,
        max_output_tokens=2048,
        gemini_model="gemini-3.1-flash-lite",
        llm_timeout_seconds=60,
        request_timeout_seconds=150,
    )
    approved = json.loads(Path(".runtime/bigquery-smoke.json").read_text())
    auth = Authenticator(
        settings.jwt_public_key_file.read_bytes(), settings.jwt_issuer, settings.jwt_audience
    )
    principal = auth.from_file(settings.jwt_token_file)
    principal.require("analytics:read")
    if principal.brands != frozenset({"Allegra K"}) or principal.all_brands:
        raise AgentError(ErrorCode.ACCESS)
    telemetry = Telemetry()
    data = BigQueryAdapter(settings, telemetry)
    model = GeminiAdapter(settings, telemetry)
    try:
        agent = AnalysisAgent(
            settings,
            model,
            ApprovedQueryOnly(data, approved),
            lambda: auth.from_file(settings.jwt_token_file),
            telemetry,
        )
        report = agent.ask(
            "What were total revenue and distinct customers in October 2026 "
            "within my allowed brands? Use exactly one aggregate query, "
            "no dimensions or extra filters, the default exclusions, "
            "order by revenue descending, limit 20. Then answer using that evidence."
        )
        result = {
            "status": "passed",
            "timestamp": datetime.now(UTC).isoformat(),
            "trace_id": telemetry.trace_id,
            "model": settings.gemini_model,
            "report": report.model_dump(mode="json"),
            "evidence": agent.last_evidence,
        }
        Path(".runtime/live-chain.json").write_text(json.dumps(result, indent=2))
        print(json.dumps(result, indent=2))
    finally:
        model.close()
        data.close()


if __name__ == "__main__":
    try:
        run()
    except Exception as error:
        print(json.dumps({"status": "blocked", "error_category": provider_error(error).code.value}))
        raise SystemExit(1) from None

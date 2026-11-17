import json
import time
from datetime import UTC, datetime
from pathlib import Path

from retail_agent.adapters import BigQueryAdapter, GeminiAdapter, provider_error
from retail_agent.agent import AnalysisAgent
from retail_agent.auth import Authenticator
from retail_agent.budget import Budget
from retail_agent.config import Settings
from retail_agent.errors import AgentError, ErrorCode
from retail_agent.observability import Telemetry
from retail_agent.sql import CompiledQuery, validate_query


def run(question: str | None = None, max_model_attempts: int = 6) -> None:
    settings = Settings(
        app_mode="live",
        gemini_model="gemini-3.1-flash-lite",
        max_model_attempts=max_model_attempts,
        max_query_steps=4,
        max_repairs=0,
        max_transport_retries=0,
        max_output_tokens=2048,
        max_input_tokens_per_turn=40000,
        max_bytes_billed_per_query=64 * 2**20,
        max_bytes_billed_per_turn=256 * 2**20,
        llm_timeout_seconds=60,
        request_timeout_seconds=300,
        jwt_token_file=Path(".runtime/ceo.jwt"),
    )
    auth = Authenticator(
        settings.jwt_public_key_file.read_bytes(), settings.jwt_issuer, settings.jwt_audience
    )
    telemetry = Telemetry()

    def review(query: CompiledQuery, estimate: int, cap: int, budget: Budget) -> None:
        preview = {
            "sql": query.sql,
            "parameters": {p.name: p.value for p in query.parameters},
            "estimated_bytes": estimate,
            "maximum_bytes_billed": cap,
            "timeout_seconds": settings.sql_timeout_seconds,
        }
        Path(".runtime/query-review.json").write_text(json.dumps(preview, default=str, indent=2))
        print(json.dumps(preview, default=str, indent=2), flush=True)
        started = time.monotonic()
        confirmation = input(
            "AWAITING_SQL_APPROVAL: type EXECUTE for this exact query, or CANCEL: "
        )
        budget.started += time.monotonic() - started
        if confirmation != "EXECUTE":
            raise AgentError(ErrorCode.ACCESS)
        validate_query(query, auth.from_file(settings.jwt_token_file), settings)

    data = BigQueryAdapter(settings, telemetry, before_execute=review)
    model = GeminiAdapter(settings, telemetry)
    try:
        agent = AnalysisAgent(
            settings, model, data, lambda: auth.from_file(settings.jwt_token_file), telemetry
        )
        report = agent.ask(
            question
            or (
                "Why did churn increase in California last month compared to Texas, "
                "and which product categories contributed most?"
            )
        )
        result = {
            "status": "completed",
            "timestamp": datetime.now(UTC).isoformat(),
            "trace_id": telemetry.trace_id,
            "report": report.model_dump(mode="json"),
            "evidence": agent.last_evidence,
        }
        Path(".runtime/live-flex.json").write_text(json.dumps(result, indent=2))
        print(json.dumps(result, indent=2))
    finally:
        model.close()
        data.close()


if __name__ == "__main__":
    try:
        run()
    except Exception as error:
        print(json.dumps({"status": "blocked", "category": provider_error(error).code.value}))
        raise SystemExit(1) from None

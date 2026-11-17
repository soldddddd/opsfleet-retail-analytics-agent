import io
from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from google.api_core import exceptions as gexc

from retail_agent.adapters import BigQueryAdapter, GeminiAdapter, bounded_call
from retail_agent.auth import Principal
from retail_agent.budget import Budget
from retail_agent.errors import AgentError, ErrorCode
from retail_agent.models import NextStep, QuerySpec
from retail_agent.observability import Telemetry
from retail_agent.sql import compile_query


def setup_query(settings):
    principal = Principal("a", frozenset({"analytics:read"}), frozenset({"Brand A"}))
    query = compile_query(
        QuerySpec(start_date=date(2026, 1, 1), end_date=date(2026, 2, 1)), principal, settings
    )
    return principal, query


def test_bigquery_preflight_parameters_metrics_and_privacy(settings):
    principal, query = setup_query(settings)
    job = MagicMock(total_bytes_processed=10, total_bytes_billed=10, slot_millis=1, cache_hit=False)
    job.result.return_value = [
        {"revenue": 100, "customers": 5, "_privacy_n": 5, "_unsafe_groups": 0}
    ]
    client = MagicMock()
    client.query.side_effect = [SimpleNamespace(total_bytes_processed=100), job]
    logs = io.StringIO()
    result = BigQueryAdapter(settings, Telemetry(logs), client).execute(
        query, principal, Budget(settings)
    )
    assert result == [{"customers": 5, "revenue": 100}]
    dry, actual = client.query.call_args_list
    assert dry.kwargs["job_config"].dry_run
    assert actual.kwargs["job_config"].maximum_bytes_billed == 2**30
    assert actual.kwargs["job_retry"] is None
    assert "Brand A" not in logs.getvalue()
    assert "billed_bytes" in logs.getvalue()


def test_cost_rejection_before_paid_query(settings):
    principal, query = setup_query(settings)
    client = MagicMock()
    client.query.return_value = SimpleNamespace(total_bytes_processed=2**40)
    with pytest.raises(AgentError) as error:
        BigQueryAdapter(settings, Telemetry(io.StringIO()), client).execute(
            query, principal, Budget(settings)
        )
    assert error.value.code == ErrorCode.BUDGET
    assert client.query.call_count == 1


def test_timeout_cancels_without_error_body_leak(settings):
    principal, query = setup_query(settings)
    client, job = MagicMock(), MagicMock()
    job.result.side_effect = TimeoutError("private@example.com")
    client.query.side_effect = [SimpleNamespace(total_bytes_processed=10), job]
    logs = io.StringIO()
    with pytest.raises(AgentError) as error:
        BigQueryAdapter(settings, Telemetry(logs), client).execute(
            query, principal, Budget(settings)
        )
    assert error.value.code == ErrorCode.TIMEOUT
    client.cancel_job.assert_called_once()
    assert "private@example.com" not in logs.getvalue() + str(error.value)


def test_transient_retries_bounded_and_permissions_not_retried(settings):
    operation = MagicMock(side_effect=gexc.ServiceUnavailable("secret"))
    with pytest.raises(AgentError):
        bounded_call(operation, Budget(settings), sleep=lambda _: None)
    assert operation.call_count == 3
    operation = MagicMock(side_effect=gexc.Forbidden("secret"))
    with pytest.raises(AgentError) as error:
        bounded_call(operation, Budget(settings), sleep=lambda _: None)
    assert operation.call_count == 1
    assert "secret" not in str(error.value)


def test_gemini_structured_output_and_usage(settings):
    client = MagicMock()
    client.models.generate_content.return_value = SimpleNamespace(
        text='{"action":"clarify","message":"Which year?"}',
        usage_metadata=SimpleNamespace(prompt_token_count=20, candidates_token_count=8),
    )
    logs = io.StringIO()
    model = GeminiAdapter(settings, Telemetry(logs), client)
    result = model.generate("Safe system", "Safe context", NextStep, Budget(settings))
    assert result.action == "clarify"
    assert '"input_tokens": 20' in logs.getvalue()
    config = client.models.generate_content.call_args.kwargs["config"]
    assert config.http_options.retry_options.attempts == 1
    assert config.response_json_schema == NextStep.model_json_schema()
    assert config.response_schema is None


def test_real_sdk_wire_schema_uses_json_schema_without_network(settings):
    import json

    import httpx
    from google import genai
    from google.genai import types

    requests = []

    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        schema = body["generationConfig"]["responseJsonSchema"]
        assert schema["additionalProperties"] is False
        assert "responseSchema" not in body["generationConfig"]
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "content": {
                            "role": "model",
                            "parts": [{"text": '{"action":"clarify","message":"Which year?"}'}],
                        },
                        "finishReason": "STOP",
                    }
                ],
                "usageMetadata": {"promptTokenCount": 15, "candidatesTokenCount": 9},
            },
        )

    transport = httpx.Client(transport=httpx.MockTransport(handler))
    client = genai.Client(
        api_key="unit-test-placeholder",  # pragma: allowlist secret
        http_options=types.HttpOptions(
            httpx_client=transport, retry_options=types.HttpRetryOptions(attempts=1)
        ),
    )
    try:
        model = GeminiAdapter(settings, Telemetry(io.StringIO()), client)
        assert model.generate("system", "context", NextStep, Budget(settings)).action == "clarify"
        assert len(requests) == 1
    finally:
        client.close()


def test_model_malformed_output_is_safe(settings):
    client = MagicMock()
    client.models.generate_content.return_value = SimpleNamespace(
        text="private@example.com", usage_metadata=None
    )
    with pytest.raises(AgentError) as error:
        GeminiAdapter(settings, Telemetry(io.StringIO()), client).generate(
            "system", "context", NextStep, Budget(settings)
        )
    assert error.value.code == ErrorCode.INVALID_MODEL
    assert "private@example.com" not in str(error.value)


def test_approval_rejection_stops_after_dry_run(settings):
    principal, query = setup_query(settings)
    client = MagicMock()
    client.query.return_value = SimpleNamespace(total_bytes_processed=10)

    def reject(*args):
        raise AgentError(ErrorCode.ACCESS)

    with pytest.raises(AgentError):
        BigQueryAdapter(settings, Telemetry(io.StringIO()), client, reject).execute(
            query, principal, Budget(settings)
        )
    assert client.query.call_count == 1
    assert client.query.call_args.kwargs["job_config"].dry_run


def test_lost_submission_reuses_existing_job(settings, monkeypatch):
    principal, query = setup_query(settings)
    job = MagicMock(total_bytes_processed=10, total_bytes_billed=10, slot_millis=1, cache_hit=False)
    job.result.return_value = [
        {"revenue": 100, "customers": 5, "_privacy_n": 5, "_unsafe_groups": 0}
    ]
    client = MagicMock()
    client.query.side_effect = [
        SimpleNamespace(total_bytes_processed=10),
        gexc.ServiceUnavailable("lost acknowledgement"),
        gexc.Conflict("duplicate"),
    ]
    client.get_job.return_value = job
    monkeypatch.setattr(
        "retail_agent.adapters.bounded_call",
        lambda operation, budget, **kwargs: bounded_call(
            operation, budget, sleep=lambda _: None, **kwargs
        ),
    )
    result = BigQueryAdapter(settings, Telemetry(io.StringIO()), client).execute(
        query, principal, Budget(settings)
    )
    assert result[0]["revenue"] == 100
    first, retried = client.query.call_args_list[1:]
    assert first.kwargs["job_id"] == retried.kwargs["job_id"] == client.get_job.call_args.args[0]


def test_review_wait_does_not_inflate_query_latency(settings, monkeypatch):
    import json

    principal, query = setup_query(settings)
    now = [0.0]
    monkeypatch.setattr("retail_agent.adapters.time.monotonic", lambda: now[0])
    job = MagicMock(total_bytes_processed=10, total_bytes_billed=10, slot_millis=1, cache_hit=False)
    job.result.return_value = [
        {"revenue": 100, "customers": 5, "_privacy_n": 5, "_unsafe_groups": 0}
    ]
    client = MagicMock()
    client.query.side_effect = [SimpleNamespace(total_bytes_processed=10), job]
    logs = io.StringIO()

    def review(query, estimate, cap, budget):
        now[0] += 300
        budget.started += 300

    BigQueryAdapter(settings, Telemetry(logs), client, review).execute(
        query, principal, Budget(settings, clock=lambda: now[0])
    )
    events = [json.loads(line) for line in logs.getvalue().splitlines()]
    assert events[-1]["event"] == "sql_query" and events[-1]["latency_ms"] == 0

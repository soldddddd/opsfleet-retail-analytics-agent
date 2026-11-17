import secrets
import time
from collections.abc import Callable
from contextlib import suppress
from typing import Any, TypeVar
from uuid import uuid4

import httpx
from google import genai
from google.api_core import exceptions as gexc
from google.auth.exceptions import GoogleAuthError
from google.cloud import bigquery
from google.genai import errors as genai_errors
from google.genai import types
from pydantic import BaseModel, ValidationError

from retail_agent.auth import Principal
from retail_agent.budget import Budget
from retail_agent.config import Settings
from retail_agent.errors import AgentError, ErrorCode
from retail_agent.observability import Telemetry
from retail_agent.privacy import pseudonymize_customer_rows, release_rows
from retail_agent.sql import CompiledQuery, Parameter, validate_query

M = TypeVar("M", bound=BaseModel)


def provider_error(error: Exception) -> AgentError:
    if isinstance(error, AgentError):
        return error
    if isinstance(error, TimeoutError | httpx.TimeoutException | gexc.DeadlineExceeded):
        return AgentError(ErrorCode.TIMEOUT)
    if isinstance(error, GoogleAuthError | gexc.Unauthorized):
        return AgentError(ErrorCode.AUTH)
    if isinstance(error, gexc.Forbidden):
        return AgentError(ErrorCode.ACCESS)
    if isinstance(error, gexc.BadRequest):
        return AgentError(ErrorCode.INVALID_QUERY)
    if isinstance(error, gexc.TooManyRequests | gexc.ServerError | httpx.TransportError):
        return AgentError(ErrorCode.TRANSIENT)
    if isinstance(error, genai_errors.APIError):
        if api_reason(error) in {"API_KEY_INVALID", "API_KEY_EXPIRED", "API_KEY_SERVICE_BLOCKED"}:
            return AgentError(ErrorCode.AUTH)
        if error.code == 429 or (error.code is not None and error.code >= 500):
            return AgentError(ErrorCode.TRANSIENT)
        if error.code in (401, 403):
            return AgentError(ErrorCode.AUTH)
        return AgentError(ErrorCode.INVALID_MODEL)
    if isinstance(error, ValidationError):
        return AgentError(ErrorCode.INVALID_MODEL)
    return AgentError(ErrorCode.INTERNAL)


def api_reason(error: genai_errors.APIError) -> str:
    """Extract only known machine codes; never expose provider messages or metadata."""
    if isinstance(error.details, dict):
        body = error.details.get("error", error.details)
        if isinstance(body, dict):
            for detail in body.get("details", []):
                if isinstance(detail, dict) and detail.get("reason") in {
                    "API_KEY_INVALID",
                    "API_KEY_EXPIRED",
                    "API_KEY_SERVICE_BLOCKED",
                    "SERVICE_DISABLED",
                }:
                    return str(detail["reason"])
    return "MODEL_NOT_FOUND" if error.code == 404 else "UNSPECIFIED"


def bounded_call[T](
    operation: Callable[[], T],
    budget: Budget,
    *,
    before_attempt: Callable[[], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    for attempt in range(budget.settings.max_transport_retries + 1):
        budget.remaining()
        if before_attempt:
            before_attempt()
        try:
            return operation()
        except Exception as raw:
            error = provider_error(raw)
            if not error.retryable or attempt == budget.settings.max_transport_retries:
                raise error from None
            delay = min(0.25 * 2**attempt + (uuid4().int % 100) / 1000, budget.remaining())
            sleep(delay)
    raise AgentError(ErrorCode.INTERNAL)


def bq_parameter(
    parameter: Parameter,
) -> bigquery.ScalarQueryParameter | bigquery.ArrayQueryParameter:
    if parameter.type_name == "ARRAY_STRING":
        return bigquery.ArrayQueryParameter(parameter.name, "STRING", parameter.value)
    if isinstance(parameter.value, tuple):
        raise AgentError(ErrorCode.SQL)
    return bigquery.ScalarQueryParameter(parameter.name, parameter.type_name, parameter.value)


class BigQueryAdapter:
    def __init__(
        self,
        settings: Settings,
        telemetry: Telemetry,
        client: Any = None,
        before_execute: Callable[[CompiledQuery, int, int, Budget], None] | None = None,
    ) -> None:
        self.settings = settings
        self.telemetry = telemetry
        self._customer_salt = secrets.token_bytes(32)
        self.before_execute = before_execute
        self.client: Any = (
            client
            if client is not None
            else bigquery.Client(
                project=settings.google_cloud_project,
                location=settings.bigquery_location,
            )
        )

    def execute(
        self,
        query: CompiledQuery,
        principal: Principal,
        budget: Budget,
    ) -> list[dict[str, str | int | float | None]]:
        validate_query(query, principal, self.settings)
        budget.query()
        parameters = [bq_parameter(p) for p in query.parameters]

        def timeout() -> float:
            return min(self.settings.sql_timeout_seconds, budget.remaining())

        start = time.monotonic()
        dry = bounded_call(
            lambda: self.client.query(
                query.sql,
                job_config=bigquery.QueryJobConfig(
                    dry_run=True,
                    use_query_cache=False,
                    use_legacy_sql=False,
                    query_parameters=parameters,
                ),
                retry=None,
                job_retry=None,
                timeout=timeout(),
            ),
            budget,
        )
        estimate = dry.total_bytes_processed
        if type(estimate) is not int:
            raise AgentError(ErrorCode.BUDGET)
        self.telemetry.emit(
            "sql_dry_run", estimated_bytes=estimate, sql_fingerprint=query.fingerprint
        )
        cap = budget.reserve_query_bytes(estimate)
        if self.before_execute:
            review_started = time.monotonic()
            self.before_execute(query, estimate, cap, budget)
            start += time.monotonic() - review_started
        job_id = f"retail_{uuid4().hex}"
        config = bigquery.QueryJobConfig(
            use_legacy_sql=False,
            maximum_bytes_billed=cap,
            query_parameters=parameters,
            labels={"app": "retail-agent", "trace": self.telemetry.trace_id},
            job_timeout_ms=int(timeout() * 1000),
        )

        def submit() -> Any:
            try:
                return self.client.query(
                    query.sql,
                    job_config=config,
                    job_id=job_id,
                    retry=None,
                    job_retry=None,
                    timeout=timeout(),
                )
            except gexc.Conflict:
                return self.client.get_job(job_id, retry=None, timeout=timeout())

        job = None
        try:
            job = bounded_call(submit, budget)
            rows = bounded_call(
                lambda: list(
                    job.result(
                        timeout=timeout(),
                        max_results=self.settings.max_result_rows,
                        retry=None,
                        job_retry=None,
                    )
                ),
                budget,
            )
            self.telemetry.emit(
                "sql_query",
                job_id=job_id,
                sql_fingerprint=query.fingerprint,
                processed_bytes=job.total_bytes_processed,
                billed_bytes=job.total_bytes_billed,
                slot_ms=job.slot_millis,
                cache_hit=job.cache_hit,
                rows=len(rows),
                latency_ms=(time.monotonic() - start) * 1000,
            )
            budget.remaining()
            raw_rows = [dict(row) for row in rows]
            if query.spec.kind == "customers":
                raw_rows = pseudonymize_customer_rows(
                    raw_rows, self._customer_salt + principal.fingerprint.encode()
                )
            return release_rows(
                raw_rows,
                columns=query.columns,
                minimum_group=self.settings.min_aggregation_group_size,
            )
        except Exception as raw:
            error = provider_error(raw)
            if error.code in (ErrorCode.TIMEOUT, ErrorCode.TRANSIENT):
                with suppress(Exception):
                    self.client.cancel_job(job_id, retry=None, timeout=5)
                self.telemetry.emit(
                    "error", job_id=job_id, error_category=error.code, cancel_requested=True
                )
            raise error from None

    def close(self) -> None:
        self.client.close()


class GeminiAdapter:
    def __init__(self, settings: Settings, telemetry: Telemetry, client: Any = None) -> None:
        self.settings = settings
        self.telemetry = telemetry
        self.client: Any = (
            client
            if client is not None
            else genai.Client(
                api_key=settings.gemini_api_key.get_secret_value(),
                http_options=types.HttpOptions(
                    timeout=int(settings.llm_timeout_seconds * 1000),
                    retry_options=types.HttpRetryOptions(attempts=1),
                ),
            )
        )

    def generate(self, system: str, context: str, schema: type[M], budget: Budget) -> M:
        bound = len((system + context + schema.model_json_schema().__str__()).encode("utf-8"))
        start = time.monotonic()

        def attempt() -> M:
            self.telemetry.emit(
                "model_attempt", model=self.settings.gemini_model, attempt=budget.model_attempts
            )
            response = send()
            usage = response.usage_metadata
            budget.reconcile_input_tokens(bound, getattr(usage, "prompt_token_count", None))
            self.telemetry.emit(
                "model_call",
                model=self.settings.gemini_model,
                attempt=budget.model_attempts,
                latency_ms=(time.monotonic() - start) * 1000,
                input_tokens=getattr(usage, "prompt_token_count", None),
                output_tokens=getattr(usage, "candidates_token_count", None),
                cached_tokens=getattr(usage, "cached_content_token_count", None),
                reasoning_tokens=getattr(usage, "thoughts_token_count", None),
            )
            budget.remaining()
            if not response.text:
                raise AgentError(ErrorCode.INVALID_MODEL)
            return schema.model_validate_json(response.text)

        def send() -> Any:
            try:
                return request()
            except genai_errors.APIError as error:
                self.telemetry.emit(
                    "model_error",
                    provider_status=error.code,
                    provider_reason=api_reason(error),
                    error_category=provider_error(error).code,
                )
                raise

        def request() -> Any:
            return self.client.models.generate_content(
                model=self.settings.gemini_model,
                contents=context,
                config=types.GenerateContentConfig(
                    system_instruction=system,
                    response_mime_type="application/json",
                    response_json_schema=schema.model_json_schema(),
                    temperature=0,
                    max_output_tokens=self.settings.max_output_tokens,
                    http_options=types.HttpOptions(
                        timeout=int(
                            min(self.settings.llm_timeout_seconds, budget.remaining()) * 1000
                        ),
                        retry_options=types.HttpRetryOptions(attempts=1),
                    ),
                ),
            )

        return bounded_call(attempt, budget, before_attempt=lambda: budget.model(bound))

    def close(self) -> None:
        self.client.close()

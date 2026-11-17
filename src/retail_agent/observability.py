import json
import re
import sys
from datetime import UTC, datetime
from typing import TextIO
from uuid import uuid4

from opentelemetry import trace

from retail_agent.errors import ErrorCode

EVENTS = frozenset(
    {
        "request_started",
        "request_finished",
        "model_call",
        "model_attempt",
        "model_error",
        "sql_dry_run",
        "sql_query",
        "policy_rejected",
        "repair",
        "error",
        "report_saved",
        "report_fallback",
        "query_reused",
        "report_deleted",
        "delete_preview",
    }
)
NUMERIC = frozenset(
    {
        "latency_ms",
        "attempt",
        "input_tokens",
        "output_tokens",
        "cached_tokens",
        "reasoning_tokens",
        "estimated_bytes",
        "processed_bytes",
        "billed_bytes",
        "slot_ms",
        "rows",
        "count",
        "provider_status",
    }
)
IDENTIFIERS = frozenset({"job_id", "sql_fingerprint", "model", "policy_version"})


class Telemetry:
    def __init__(self, stream: TextIO | None = None, session_id: str | None = None) -> None:
        self.stream = stream or sys.stderr
        self.session_id = session_id or uuid4().hex
        self.trace_id = uuid4().hex

    def new_turn(self) -> str:
        self.trace_id = uuid4().hex
        return self.trace_id

    def emit(self, event: str, **fields: object) -> None:
        if event not in EVENTS:
            raise ValueError("Unknown telemetry event")
        safe: dict[str, object] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "event": event,
            "trace_id": self.trace_id,
            "session_id": self.session_id,
        }
        for key, value in fields.items():
            if key in NUMERIC and (value is None or type(value) in (float, int)):
                safe[key] = value
            elif key in IDENTIFIERS and isinstance(value, str):
                if re.fullmatch(r"[a-zA-Z0-9_.:-]{1,160}", value):
                    safe[key] = value
            elif (
                key == "failure_reason"
                and value
                in (
                    "request_deadline",
                    "model_attempts",
                    "input_tokens",
                    "query_steps",
                    "query_bytes",
                    "repairs",
                    "schema",
                    "invalid_citations",
                    "uncited_reference",
                    "literal_or_malformed_reference",
                    "missing_evidence_cell",
                    "nonnumeric_evidence_cell",
                    "nonfinite_evidence_cell",
                    "unsupported_metric_interpretation",
                )
                or key == "validation_stage"
                and value
                in (
                    "writer_schema",
                    "writer_citations",
                    "writer_numbers",
                )
            ):
                safe[key] = value
            elif key == "error_category" and isinstance(value, ErrorCode):
                safe[key] = value.value
            elif (
                key == "provider_reason"
                and isinstance(value, str)
                and value
                in {
                    "API_KEY_INVALID",
                    "API_KEY_EXPIRED",
                    "API_KEY_SERVICE_BLOCKED",
                    "SERVICE_DISABLED",
                    "MODEL_NOT_FOUND",
                    "UNSPECIFIED",
                }
                or key in {"cache_hit", "cancel_requested"}
                and type(value) is bool
            ):
                safe[key] = value
        self.stream.write(json.dumps(safe, sort_keys=True) + "\n")
        self.stream.flush()
        with trace.get_tracer("retail_agent").start_as_current_span(
            event, record_exception=False, set_status_on_exception=False
        ) as span:
            span.set_attribute("app.correlation_id", self.trace_id)
            for key, value in safe.items():
                if isinstance(value, str | int | float | bool):
                    span.set_attribute(key, value)

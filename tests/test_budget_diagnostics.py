import io
import json
from functools import partial

import pytest

from retail_agent.budget import Budget
from retail_agent.errors import AgentError
from retail_agent.observability import Telemetry


@pytest.mark.parametrize(
    "limit",
    ["model_attempts", "input_tokens", "query_steps", "repairs", "query_bytes", "request_deadline"],
)
def test_exhausted_limit_has_specific_safe_diagnostic(settings, limit):
    budget = Budget(settings)
    if limit == "model_attempts":
        budget.model_attempts = settings.max_model_attempts
        action = partial(budget.model, 1)
    elif limit == "input_tokens":
        budget.input_tokens = settings.max_input_tokens_per_turn
        action = partial(budget.model, 1)
    elif limit == "query_steps":
        budget.query_steps = settings.max_query_steps
        action = budget.query
    elif limit == "repairs":
        budget.repairs = settings.max_repairs
        action = budget.repair
    elif limit == "query_bytes":
        budget.reserved_bytes = settings.max_bytes_billed_per_turn
        action = partial(budget.reserve_query_bytes, 1)
    else:
        budget.clock = lambda: budget.started + settings.request_timeout_seconds + 1
        action = budget.remaining
    with pytest.raises(AgentError) as error:
        action()
    assert error.value.reason == limit
    stream = io.StringIO()
    Telemetry(stream).emit(
        "error", error_category=error.value.code, failure_reason=error.value.reason
    )
    assert json.loads(stream.getvalue())["failure_reason"] == limit


def test_failure_reason_does_not_log_arbitrary_content():
    stream = io.StringIO()
    Telemetry(stream).emit("error", failure_reason="private provider response")
    assert "private provider response" not in stream.getvalue()

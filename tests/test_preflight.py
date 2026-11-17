import json
from dataclasses import replace
from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from retail_agent.auth import Principal
from retail_agent.budget import Budget
from retail_agent.errors import AgentError
from retail_agent.live_smoke import ApprovedQueryOnly
from retail_agent.models import QuerySpec
from retail_agent.preflight import REQUIRED, run_preflight
from retail_agent.sql import compile_query


def test_preflight_only_metadata_and_dry_runs(settings):
    client = MagicMock()
    client.get_dataset.return_value = SimpleNamespace(location="US")
    client.get_table.side_effect = [
        SimpleNamespace(schema=[SimpleNamespace(name=f) for f in fields])
        for fields in REQUIRED.values()
    ]
    client.query.side_effect = [
        SimpleNamespace(total_bytes_processed=100),
        SimpleNamespace(total_bytes_processed=0),
    ]
    result = run_preflight(settings, client)
    assert result["executed_queries"] == 0 and result["gemini_calls"] == 0
    assert result["analytics_query"]["estimated_bytes"] == 100
    assert all(call.kwargs["job_config"].dry_run for call in client.query.call_args_list)


def test_preflight_rejects_region_or_missing_schema(settings):
    client = MagicMock()
    client.get_dataset.return_value = SimpleNamespace(location="EU")
    with pytest.raises(AgentError):
        run_preflight(settings, client)
    client.query.assert_not_called()
    client.get_dataset.return_value = SimpleNamespace(location="US")
    client.get_table.return_value = SimpleNamespace(schema=[])
    with pytest.raises(AgentError):
        run_preflight(settings, client)
    client.query.assert_not_called()


def test_smoke_guard_allows_only_exact_approved_query_once(settings):
    principal = Principal("a", frozenset({"analytics:read"}), frozenset({"Allegra K"}))
    query = compile_query(
        QuerySpec(start_date=date(2026, 10, 1), end_date=date(2026, 11, 1)), principal, settings
    )
    approved = {
        "sql": query.sql,
        "parameters": json.loads(
            json.dumps({p.name: p.value for p in query.parameters}, default=str)
        ),
    }
    data = MagicMock()
    data.execute.return_value = [{"revenue": 100}]
    guarded = ApprovedQueryOnly(data, approved)
    with pytest.raises(AgentError):
        guarded.execute(replace(query, sql="SELECT 1"), principal, Budget(settings))
    data.execute.assert_not_called()
    assert guarded.execute(query, principal, Budget(settings)) == [{"revenue": 100}]
    with pytest.raises(AgentError):
        guarded.execute(query, principal, Budget(settings))
    assert data.execute.call_count == 1

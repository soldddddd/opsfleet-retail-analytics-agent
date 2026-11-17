import io
import json
from datetime import date

import pytest

from retail_agent.agent import AnalysisAgent
from retail_agent.auth import Principal
from retail_agent.errors import AgentError, ErrorCode
from retail_agent.models import NextStep, QuerySpec, Report
from retail_agent.observability import Telemetry


class ScriptedModel:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.contexts = []

    def generate(self, system, context, schema, budget):
        budget.model(10)
        self.contexts.append(json.loads(context))
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response


class FakeData:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.queries = []

    def execute(self, query, principal, budget):
        budget.query()
        self.queries.append(query)
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response


def query(month=1):
    return NextStep(
        action="query",
        query=QuerySpec(
            start_date=date(2026, month, 1),
            end_date=date(2026, month + 1, 1),
        ),
    )


def auth():
    return Principal("a", frozenset({"analytics:read"}), frozenset({"Brand A"}))


def test_dependent_queries_grounded_report_and_followup(settings):
    model = ScriptedModel(
        [
            query(),
            query(2),
            NextStep(action="answer"),
            Report(
                title="Comparison",
                summary="Revenue grew from {{q1:0:revenue}} to {{q2:0:revenue}}.",
                evidence_ids=("q1", "q2"),
            ),
            NextStep(action="clarify", message="Which category?"),
        ]
    )
    data = FakeData([[{"revenue": 100}], [{"revenue": 150}]])
    agent = AnalysisAgent(settings, model, data, auth, Telemetry(io.StringIO()))
    report = agent.ask("Compare January and February revenue in 2026.")
    assert report.evidence_ids == ("q1", "q2")
    assert model.contexts[1]["evidence"][0]["rows"] == [{"revenue": 100}]
    assert len(data.queries) == 2
    agent.ask("Break that down by category.")
    assert model.contexts[-1]["history"][0]["answer"] == report.summary


def test_repair_uses_safe_error_code(settings):
    model = ScriptedModel(
        [
            query(),
            query(2),
            NextStep(action="answer"),
            Report(title="Repaired", summary="Verified result.", evidence_ids=("q1",)),
        ]
    )
    data = FakeData([AgentError(ErrorCode.INVALID_QUERY), [{"revenue": 100}]])
    agent = AnalysisAgent(settings, model, data, auth, Telemetry(io.StringIO()))
    assert agent.ask("Revenue?").title == "Repaired"
    assert model.contexts[1]["feedback"] == "invalid_query"


def test_pii_request_stops_before_model(settings):
    model = ScriptedModel([])
    agent = AnalysisAgent(settings, model, FakeData([]), auth, Telemetry(io.StringIO()))
    with pytest.raises(AgentError):
        agent.ask("List customer email addresses.")
    assert not model.contexts


def test_repeated_query_cannot_loop(settings):
    model = ScriptedModel(
        [query(), query(), Report(title="Sales", summary="{{q1:0:revenue}}", evidence_ids=("q1",))]
    )
    data = FakeData([[{"revenue": 100}]])
    agent = AnalysisAgent(settings, model, data, auth, Telemetry(io.StringIO()))
    report = agent.ask("Revenue?")
    assert "revenue=100.00" in report.summary
    assert len(data.queries) == 1
    assert len(model.contexts) == 3


def test_fabricated_evidence_id_rejected(settings):
    model = ScriptedModel(
        [
            query(),
            NextStep(action="answer"),
            Report(title="Bad", summary="Fake", evidence_ids=("q99",)),
        ]
    )
    agent = AnalysisAgent(
        settings, model, FakeData([[{"revenue": 100}]]), auth, Telemetry(io.StringIO())
    )
    report = agent.ask("Revenue?")
    assert report.title == "Verified query results"
    assert report.evidence_ids == ("q1",)
    assert "Fake" not in report.model_dump_json()
    assert "100" in report.findings[0]


def test_scope_change_invalidates_history(settings):
    current = [auth()]
    model = ScriptedModel([NextStep(action="schema"), NextStep(action="schema")])
    agent = AnalysisAgent(
        settings, model, FakeData([]), lambda: current[0], Telemetry(io.StringIO())
    )
    agent.ask("What data is available?")
    current[0] = Principal("b", frozenset({"analytics:read"}), frozenset({"Brand B"}))
    agent.ask("What data is available?")
    assert not model.contexts[1]["history"]


@pytest.mark.parametrize(
    "dimensions", [("state", "category"), ("category", "product"), ("state",), ()]
)
def test_privacy_suppression_explains_without_new_model_or_data_calls(settings, dimensions):
    step = NextStep(
        action="query",
        query=QuerySpec(
            kind="churn",
            start_date=date(2026, 9, 1),
            end_date=date(2026, 10, 1),
            dimensions=dimensions,
            order_by="churn_rate",
        ),
    )
    model = ScriptedModel([step, NextStep(action="schema")])
    data = FakeData([AgentError(ErrorCode.PRIVACY)])
    logs = io.StringIO()
    agent = AnalysisAgent(settings, model, data, auth, Telemetry(logs))
    report = agent.ask("Why did churn increase?")
    assert report.title == "Privacy-limited analysis"
    assert "minimum group size remains 5" in report.summary
    assert "cannot verify" in report.summary
    assert report.actions and not report.evidence_ids
    assert "No alternative query was executed" in report.limitations[1]
    assert len(model.contexts) == len(data.queries) == 1
    assert not agent.last_evidence
    assert '"event": "policy_rejected"' in logs.getvalue()
    assert agent.ask("What data is available?").title == "Available retail analytics"


def test_real_fixture_privacy_block_then_explicit_safe_aggregation(settings):
    from retail_agent.offline import FixtureWarehouse

    warehouse = FixtureWarehouse(settings)
    try:
        warehouse.connection.execute("DELETE FROM users WHERE id NOT IN (1,2,3,4,21,22,23,24)")
        base = dict(start_date=date(2026, 8, 1), end_date=date(2026, 9, 1))
        model = ScriptedModel(
            [
                NextStep(action="query", query=QuerySpec(**base, dimensions=("state", "category"))),
                NextStep(action="query", query=QuerySpec(**base, dimensions=("state",))),
                NextStep(action="answer"),
                Report(
                    title="State total",
                    summary="Customers {{q1:0:customers}} with {{q1:0:revenue}}.",
                    evidence_ids=("q1",),
                ),
            ]
        )
        agent = AnalysisAgent(settings, model, warehouse, auth, Telemetry(io.StringIO()))
        blocked = agent.ask("Compare revenue by state and category in August 2026.")
        assert blocked.title == "Privacy-limited analysis"
        assert len(model.contexts) == 1
        result = agent.ask("Combine categories; show the state total for the same month.")
        assert result.title == "State total"
        assert agent.last_evidence[0]["rows"] == [
            {"state": "California", "revenue": 120.0, "customers": 8}
        ]
        assert warehouse.settings.min_aggregation_group_size == 5
    finally:
        warehouse.close()


def test_empty_result_is_bounded_and_not_treated_as_suppression(settings):
    model = ScriptedModel(
        [
            query(),
            query(2),
            NextStep(action="answer"),
            Report(title="Checked", summary="Second period has data.", evidence_ids=("q2",)),
        ]
    )
    data = FakeData([[], [{"revenue": 150}]])
    agent = AnalysisAgent(settings, model, data, auth, Telemetry(io.StringIO()))
    assert agent.ask("Check the two specified periods.").title == "Checked"
    assert model.contexts[1]["feedback"] == "empty_result"
    assert len(data.queries) == 2


def test_invalid_model_correction_is_bounded(settings):
    model = ScriptedModel([AgentError(ErrorCode.INVALID_MODEL)] * 3)
    agent = AnalysisAgent(settings, model, FakeData([]), auth, Telemetry(io.StringIO()))
    with pytest.raises(AgentError) as error:
        agent.ask("Revenue?")
    assert error.value.code == ErrorCode.INVALID_MODEL
    assert len(model.contexts) == 3


def test_scope_revoked_during_model_call_stops_before_sql(settings):
    current = [auth()]
    model = ScriptedModel([query()])
    original = model.generate

    def change_scope(*args):
        current[0] = Principal("a", frozenset({"analytics:read"}), frozenset({"Brand B"}))
        return original(*args)

    model.generate = change_scope
    data = FakeData([])
    agent = AnalysisAgent(settings, model, data, lambda: current[0], Telemetry(io.StringIO()))
    with pytest.raises(AgentError) as error:
        agent.ask("Revenue?")
    assert error.value.code == ErrorCode.ACCESS
    assert not data.queries and not agent.history


def test_early_finish_is_explicitly_incomplete_without_writer_call(settings):
    model = ScriptedModel([query(), NextStep(action="answer")])
    agent = AnalysisAgent(
        settings, model, FakeData([[{"revenue": 100}]]), auth, Telemetry(io.StringIO())
    )
    report = agent.ask("Compare January and February revenue")
    assert report.title == "Incomplete analysis"
    assert len(model.contexts) == 2
    assert "two periods" in report.limitations[0]


def test_query_cap_does_not_disguise_partial_analysis(settings):
    settings = settings.model_copy(update={"max_query_steps": 1})
    model = ScriptedModel([query()])
    agent = AnalysisAgent(
        settings, model, FakeData([[{"revenue": 100}]]), auth, Telemetry(io.StringIO())
    )
    report = agent.ask("Compare January and February revenue")
    assert report.title == "Incomplete analysis"
    assert len(model.contexts) == 1


@pytest.mark.parametrize("greeting", ["Hello", "hello!", " HI ", "Привет", "Здравствуйте!"])
def test_greeting_is_local_and_scoped(settings, greeting):
    model, data = ScriptedModel([]), FakeData([])
    agent = AnalysisAgent(settings, model, data, auth, Telemetry(io.StringIO()))
    report = agent.ask(greeting)
    assert report.title == "Clarify"
    assert report.scope == "Permitted brands: Brand A"
    assert not model.contexts and not data.queries
    assert not agent.last_evidence


@pytest.mark.parametrize(
    ("response", "stage"),
    [
        (
            Report(title="Revenue 2026", summary="Invented 9999", evidence_ids=("q1",)),
            "writer_numbers",
        ),
        (Report(title="Sales", summary="{{q1:9:revenue}}", evidence_ids=("q1",)), "writer_numbers"),
        (Report(title="Sales", summary="Unsupported", evidence_ids=()), "writer_citations"),
        (AgentError(ErrorCode.INVALID_MODEL), "writer_schema"),
    ],
)
def test_invalid_writer_preserves_released_results_without_extra_calls(settings, response, stage):
    model = ScriptedModel([query(), NextStep(action="answer"), response])
    data = FakeData([[{"revenue": 123.45, "customers": 10}]])
    logs = io.StringIO()
    agent = AnalysisAgent(settings, model, data, auth, Telemetry(logs))
    report = agent.ask("Show revenue and customers.")
    assert report.title == "Verified query results"
    assert "Revenue: 123.45" in report.findings[0]
    assert "Distinct customers: 10" in report.findings[0]
    assert "9999" not in report.model_dump_json()
    assert report.definitions and report.scope
    assert report.evidence_ids == ("q1",)
    assert len(data.queries) == 1 and len(model.contexts) == 3
    event = next(
        e for e in map(json.loads, logs.getvalue().splitlines()) if e["event"] == "report_fallback"
    )
    assert event["validation_stage"] == stage
    assert "123.45" not in logs.getvalue() and "9999" not in logs.getvalue()


def test_writer_access_error_is_not_converted_to_results(settings):
    model = ScriptedModel([query(), NextStep(action="answer"), AgentError(ErrorCode.ACCESS)])
    agent = AnalysisAgent(
        settings, model, FakeData([[{"revenue": 100}]]), auth, Telemetry(io.StringIO())
    )
    with pytest.raises(AgentError) as error:
        agent.ask("Revenue?")
    assert error.value.code == ErrorCode.ACCESS
    assert not agent.history and not agent.last_evidence


def test_greeting_does_not_bypass_authentication(settings):
    def denied():
        raise AgentError(ErrorCode.AUTH)

    agent = AnalysisAgent(
        settings, ScriptedModel([]), FakeData([]), denied, Telemetry(io.StringIO())
    )
    with pytest.raises(AgentError):
        agent.ask("Hello")


def test_fallback_followup_reuses_duplicate_without_losing_query_context(settings):
    from retail_agent.models import Filter

    october = NextStep(
        action="query",
        query=QuerySpec(
            start_date=date(2026, 10, 1),
            end_date=date(2026, 11, 1),
            filters=(Filter(dimension="brand", values=("Brand A",)),),
        ),
    )
    september = NextStep(
        action="query",
        query=october.query.model_copy(
            update={
                "start_date": date(2026, 9, 1),
                "end_date": date(2026, 10, 1),
            }
        ),
    )
    model = ScriptedModel(
        [
            october,
            NextStep(action="answer"),
            Report(title="October 2026", summary="Revenue 3200.45", evidence_ids=("q1",)),
            september,
            september,
            Report(
                title="Sales", summary="{{q1:0:revenue}}; {{q1:0:customers}}", evidence_ids=("q1",)
            ),
        ]
    )
    data = FakeData(
        [
            [{"revenue": 3200.4500041007996, "customers": 220}],
            [{"revenue": 3000.0, "customers": 200}],
        ]
    )
    logs = io.StringIO()
    agent = AnalysisAgent(settings, model, data, auth, Telemetry(logs))
    first = agent.ask("Show revenue and customers for Brand A in October 2026.")
    assert first.findings == ("Revenue: 3,200.45; Distinct customers: 220",)
    second = agent.ask("Now show the same metrics for September 2026.")
    assert second.title == "Sales" and "3000.00" in second.summary
    prior = model.contexts[3]["history"][-1]["analyses"][0]
    assert prior == october.query.model_dump(mode="json")
    assert model.contexts[3]["evidence"] == []
    assert model.contexts[5]["numeric_references"] == ["{{q1:0:revenue}}", "{{q1:0:customers}}"]
    assert len(data.queries) == 2 and len(model.contexts) == 6
    events = list(map(json.loads, logs.getvalue().splitlines()))
    fallback = next(e for e in events if e["event"] == "report_fallback")
    assert fallback["validation_stage"] == "writer_numbers"
    assert fallback["failure_reason"] == "literal_or_malformed_reference"
    assert sum(e["event"] == "query_reused" for e in events) == 1


def test_failed_turn_preserves_last_successful_context_and_budget_resets(settings):
    settings = settings.model_copy(update={"max_model_attempts": 3})
    model = ScriptedModel(
        [
            query(),
            NextStep(action="answer"),
            Report(title="Sales", summary="{{q1:0:revenue}}", evidence_ids=("q1",)),
            query(2),
            query(3),
            NextStep(action="answer"),
            NextStep(action="clarify", message="Which period?"),
        ]
    )
    data = FakeData([[{"revenue": 100}], [{"revenue": 200}], [{"revenue": 300}]])
    logs = io.StringIO()
    agent = AnalysisAgent(settings, model, data, auth, Telemetry(logs))
    agent.ask("January revenue")
    saved = list(agent.history)
    with pytest.raises(AgentError) as error:
        agent.ask("Analyze other periods")
    assert error.value.reason == "model_attempts"
    assert agent.history == saved and agent.last_evidence == []
    agent.ask("Please clarify the last successful period")
    assert model.contexts[-1]["history"] == saved
    assert '"failure_reason": "model_attempts"' in logs.getvalue()


def test_duplicate_does_not_override_incomplete_comparison(settings):
    model = ScriptedModel([query(), query()])
    data = FakeData([[{"revenue": 100}]])
    agent = AnalysisAgent(settings, model, data, auth, Telemetry(io.StringIO()))
    assert agent.ask("Compare January and February revenue").title == "Incomplete analysis"
    assert len(model.contexts) == 2 and len(data.queries) == 1


def test_unsupported_aov_advice_is_not_released(settings):
    model = ScriptedModel(
        [
            query(),
            NextStep(action="answer"),
            Report(
                title="Sales",
                summary="Revenue {{q1:0:revenue}}.",
                evidence_ids=("q1",),
                actions=(
                    "Derive average transaction value from {{q1:0:revenue}} "
                    "and {{q1:0:customers}}.",
                ),
            ),
        ]
    )
    data = FakeData([[{"revenue": 3200.45, "customers": 220}]])
    agent = AnalysisAgent(settings, model, data, auth, Telemetry(io.StringIO()))
    result = agent.ask("Revenue and customers?")
    assert result.title == "Verified query results"
    assert "average transaction value" not in result.model_dump_json()
    assert result.findings == ("Revenue: 3,200.45; Distinct customers: 220",)
    assert len(data.queries) == 1 and len(model.contexts) == 3


@pytest.mark.parametrize("brands", [("Nike",), ("Brand A", "Nike")])
def test_unauthorized_brand_is_access_denial_before_data(settings, brands):
    from retail_agent.models import Filter

    step = query().model_copy(
        update={
            "query": query().query.model_copy(
                update={
                    "filters": (Filter(dimension="brand", values=brands),),
                }
            )
        }
    )
    model = ScriptedModel([step])
    data = FakeData([])
    logs = io.StringIO()
    agent = AnalysisAgent(settings, model, data, auth, Telemetry(logs))
    with pytest.raises(AgentError) as error:
        agent.ask("Show total revenue for Nike in October 2026.")
    assert error.value.code == ErrorCode.ACCESS
    assert "privacy" not in str(error.value).lower() and "window" not in str(error.value).lower()
    assert not data.queries and not agent.history and not agent.last_evidence
    assert len(model.contexts) == 1
    assert '"error_category": "access_denied"' in logs.getvalue()
    assert '"error_category": "privacy_policy"' not in logs.getvalue()


def test_authorized_brand_small_group_remains_privacy_limited(settings):
    from retail_agent.models import Filter

    step = query().model_copy(
        update={
            "query": query().query.model_copy(
                update={
                    "filters": (Filter(dimension="brand", values=("Brand A",)),),
                }
            )
        }
    )
    model = ScriptedModel([step])
    from retail_agent.privacy import release_rows

    class GatedData(FakeData):
        def execute(self, compiled, principal, budget):
            rows = super().execute(compiled, principal, budget)
            return release_rows(
                rows, columns=compiled.columns, minimum_group=settings.min_aggregation_group_size
            )

    data = GatedData([[{"revenue": 40, "customers": 4, "_privacy_n": 4, "_unsafe_groups": 1}]])
    agent = AnalysisAgent(settings, model, data, auth, Telemetry(io.StringIO()))
    report = agent.ask("Show Brand A revenue.")
    assert report.title == "Privacy-limited analysis"
    assert "minimum group size remains 5" in report.summary
    assert len(data.queries) == 1 and len(model.contexts) == 1
    assert report.actions

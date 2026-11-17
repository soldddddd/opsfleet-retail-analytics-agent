from datetime import date

import pytest

from retail_agent.errors import AgentError
from retail_agent.grounding import completion_gaps, ground_report
from retail_agent.models import QuerySpec, Report


def evidence(month=9, rows=None, **kwargs):
    spec = QuerySpec(start_date=date(2026, month, 1), end_date=date(2026, month + 1, 1), **kwargs)
    return {
        "id": f"q{month}",
        "definition": spec.model_dump(mode="json"),
        "rows": rows if rows is not None else [{"revenue": 3211.4200077056885}],
    }


def test_numeric_reference_renders_source_value_and_metric():
    report = Report(title="Sales", summary="Result: {{q9:0:revenue}}.", evidence_ids=("q9",))
    result = ground_report(report, [evidence()])
    assert result.summary == "Result: revenue=3211.42 [q9, row 1]."


@pytest.mark.parametrize(
    "summary",
    [
        "Revenue 3375.65",
        "Revenue 3211.42",
        "{{q9:1:revenue}}",
        "{{q8:0:revenue}}",
        "{{q9:0:customers}}",
        "{{q9:0:revenue}} grew 10%",
        "{{broken}}",
        "Result from q99",
    ],
)
def test_unsupported_numbers_and_references_rejected(summary):
    with pytest.raises(AgentError):
        ground_report(Report(title="Sales", summary=summary, evidence_ids=("q9",)), [evidence()])


def test_uncited_source_and_string_are_rejected():
    with pytest.raises(AgentError):
        ground_report(
            Report(title="Sales", summary="{{q9:0:revenue}}", evidence_ids=("q8",)), [evidence()]
        )
    with pytest.raises(AgentError):
        ground_report(
            Report(title="Sales", summary="{{q9:0:state}}", evidence_ids=("q9",)),
            [evidence(rows=[{"state": "California"}])],
        )


def test_premature_answer_and_empty_comparison_period():
    question = "Compare September and October revenue"
    assert completion_gaps(question, [evidence()])
    assert completion_gaps(question, [evidence(), evidence(10, rows=[])])
    assert not completion_gaps(question, [evidence(), evidence(10)])


def test_inconsistent_comparison_filters_and_metrics():
    assert completion_gaps(
        "Compare September and October revenue", [evidence(), evidence(10, excluded_statuses=())]
    )


def test_missing_category_or_state_evidence():
    assert completion_gaps(
        "Why did churn increase in California compared to Texas and which categories contributed?",
        [evidence(), evidence(10)],
    )


def test_writer_example_is_valid_and_remains_strict():
    import json

    from retail_agent.agent import WRITER

    example = WRITER[WRITER.index('{"title"') : WRITER.index("Do not copy this example")].strip()
    report = Report.model_validate(json.loads(example))
    rows = [{"id": "q1", "rows": [{"revenue": 3200.4500041007996, "customers": 220}]}]
    result = ground_report(report, rows)
    assert "revenue=3200.45" in result.summary and "customers=220" in result.summary
    with pytest.raises(AgentError) as error:
        ground_report(report.model_copy(update={"summary": "Revenue 3200.45"}), rows)
    assert error.value.reason == "literal_or_malformed_reference"


def test_fallback_comparison_keeps_periods_and_group_labels():
    from retail_agent.grounding import fallback_findings

    rows = [{"state": "California", "revenue": 3200.4500041007996, "customers": 220}]
    result = fallback_findings([evidence(9, rows=rows), evidence(10, rows=rows)])
    assert "2026-09-01 to 2026-10-01" in result[0]
    assert "2026-10-01 to 2026-11-01" in result[1]
    assert "State: California; Revenue: 3,200.45; Distinct customers: 220" in result[0]


@pytest.mark.parametrize(
    "phrase",
    [
        "average transaction value",
        "average order value",
        "AOV",
        "average basket value",
        "средний чек",
    ],
)
def test_customer_ratio_cannot_be_interpreted_as_order_value(phrase):
    report = Report(
        title="Sales",
        summary="Verified sales.",
        evidence_ids=("q9",),
        actions=(
            f"Analyze {phrase} derived from {{{{q9:0:revenue}}}} and {{{{q9:0:customers}}}}.",
        ),
    )
    with pytest.raises(AgentError) as error:
        ground_report(report, [evidence(rows=[{"revenue": 3200.45, "customers": 220}])])
    assert error.value.reason == "unsupported_metric_interpretation"


def test_order_value_requires_compatible_references():
    rows = [
        {"revenue": 100, "orders": 5, "average_order_value": 20},
        {"revenue": 200, "orders": 10, "average_order_value": 20},
    ]
    for summary in (
        "Average order value was {{q9:0:average_order_value}}.",
        "Evaluate AOV using {{q9:0:revenue}} and {{q9:0:orders}}.",
    ):
        report = Report(title="Orders", summary=summary, evidence_ids=("q9",))
        assert ground_report(report, [evidence(rows=rows)]).summary
    for summary in (
        "Evaluate AOV using {{q9:0:revenue}} and {{q9:1:orders}}.",
        "Evaluate average transaction value from revenue and distinct customers.",
        "Average order value is {{q9:0:revenue}}.",
    ):
        with pytest.raises(AgentError):
            ground_report(
                Report(title="Orders", summary=summary, evidence_ids=("q9",)), [evidence(rows=rows)]
            )


def test_revenue_per_customer_is_not_relabelled_as_order_value():
    report = Report(
        title="Sales",
        summary="Revenue was {{q9:0:revenue}}.",
        actions=(
            "Investigate revenue per distinct customer using "
            "{{q9:0:revenue}} and {{q9:0:customers}}.",
        ),
        evidence_ids=("q9",),
    )
    assert ground_report(report, [evidence(rows=[{"revenue": 3200.45, "customers": 220}])]).actions

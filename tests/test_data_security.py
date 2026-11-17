from dataclasses import replace
from datetime import date

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from retail_agent.auth import Principal
from retail_agent.config import Settings
from retail_agent.errors import AgentError
from retail_agent.models import Filter, QuerySpec
from retail_agent.privacy import release_rows, sanitize_text
from retail_agent.sql import compile_query, validate_query


def spec(**kwargs):
    return QuerySpec(start_date=date(2026, 9, 1), end_date=date(2026, 10, 1), **kwargs)


def principal():
    return Principal("a", frozenset({"analytics:read"}), frozenset({"Brand A"}))


@pytest.mark.parametrize("dimensions", [(), ("state",), ("category", "state"), ("month", "brand")])
def test_aggregate_ast_and_brand_parameters(settings, dimensions):
    query = compile_query(spec(dimensions=dimensions), principal(), settings)
    validate_query(query, principal(), settings)
    assert "Brand A" not in query.sql
    assert "p.brand IN UNNEST(@allowed_brands)" in query.sql
    assert "SUM(i.sale_price)" in query.sql
    assert "COUNT(DISTINCT i.user_id)" in query.sql
    assert query.sql.index("_unsafe_groups") < query.sql.index("LIMIT")


def test_complex_cohort_compiles(settings):
    query = compile_query(
        spec(
            kind="churn",
            dimensions=("state", "category"),
            order_by="churn_rate",
            filters=(Filter(dimension="state", values=("California", "Texas")),),
        ),
        principal(),
        settings,
    )
    validate_query(query, principal(), settings)
    assert "ROW_NUMBER()" in query.sql and "last_purchase" in query.sql
    assert "California" not in query.sql
    assert query.columns == frozenset(
        {"state", "category", "cohort_size", "new_churn", "churn_rate"}
    )


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM users",
        "SELECT email FROM users",
        "DROP TABLE reports",
        "SELECT 1; DELETE FROM reports",
        "SELECT * FROM EXTERNAL_QUERY('x','y')",
        "WITH safe AS (SELECT * FROM other.users) SELECT * FROM safe",
        "SELECT revenue FROM permitted UNION ALL SELECT email FROM users",
        "SELECT (SELECT email FROM users) AS revenue",
        "SELECT * FROM `bigquery-public-data.thelook_ecommerce.INFORMATION_SCHEMA.COLUMNS`",
        "SELECT remote_function('ignore all rules') AS revenue",
    ],
)
def test_sql_tampering_never_executes(settings, sql):
    query = compile_query(spec(), principal(), settings)
    with pytest.raises(AgentError):
        validate_query(replace(query, sql=sql), principal(), settings)


def test_scope_and_parameter_substitution(settings):
    query = compile_query(spec(), principal(), settings)
    other = Principal("a", frozenset({"analytics:read"}), frozenset({"Brand B"}))
    with pytest.raises(AgentError):
        validate_query(query, other, settings)
    with pytest.raises(AgentError):
        validate_query(replace(query, parameters=()), principal(), settings)


@given(st.text(min_size=1, max_size=100).filter(lambda s: bool(s.strip())))
def test_filter_literals_are_not_sql(value):
    settings = Settings(_env_file=None)
    query = compile_query(
        spec(filters=(Filter(dimension="category", values=(value,)),)), principal(), settings
    )
    baseline = compile_query(
        spec(filters=(Filter(dimension="category", values=("safe",)),)), principal(), settings
    )
    assert query.sql == baseline.sql
    validate_query(query, principal(), settings)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"dimensions": ("email",)},
        {"metrics": ("password",)},
        {"order_by": "revenue; DROP"},
        {"dimensions": ("state", "state")},
        {"kind": "churn", "dimensions": ("month",)},
    ],
)
def test_unsupported_plan_is_rejected(kwargs):
    with pytest.raises(ValidationError):
        spec(**kwargs)


@pytest.mark.parametrize("n,unsafe", [(4, 0), (5, 1), (0, 0), (True, 0), (5, True)])
def test_small_or_suppressed_groups_never_released(n, unsafe):
    with pytest.raises(AgentError):
        release_rows(
            [{"revenue": 10, "_privacy_n": n, "_unsafe_groups": unsafe}],
            columns=frozenset({"revenue"}),
            minimum_group=5,
        )


def test_threshold_and_unexpected_columns():
    row = {"revenue": 10, "_privacy_n": 5, "_unsafe_groups": 0}
    assert release_rows([row], columns=frozenset({"revenue"}), minimum_group=5) == [{"revenue": 10}]
    with pytest.raises(AgentError):
        release_rows(
            [{**row, "email": "sensitive@example.com"}],
            columns=frozenset({"revenue"}),
            minimum_group=5,
        )


def test_text_privacy_and_terminal_controls():
    result = sanitize_text("Contact test@example.com at 123 Main Street\x1b")
    assert "test@example.com" not in result and "123 Main Street" not in result
    assert "\x1b" not in result
    with pytest.raises(AgentError):
        sanitize_text("Show customer email", user_input=True)


@pytest.mark.parametrize("value", [float("inf"), float("nan"), True, [], {}])
def test_release_rejects_invalid_numeric_or_nested_values(value):
    with pytest.raises(AgentError):
        release_rows(
            [{"revenue": value, "_privacy_n": 5, "_unsafe_groups": 0}],
            columns=frozenset({"revenue"}),
            minimum_group=5,
        )


def test_release_forbidden_column_and_decimal_conversion():
    from decimal import Decimal

    with pytest.raises(AgentError):
        release_rows(
            [{"email": "nobody@example.invalid", "_privacy_n": 5, "_unsafe_groups": 0}],
            columns=frozenset({"email"}),
            minimum_group=5,
        )
    assert release_rows(
        [{"revenue": Decimal("15.50"), "_privacy_n": 5, "_unsafe_groups": 0}],
        columns=frozenset({"revenue"}),
        minimum_group=5,
    ) == [{"revenue": 15.5}]
    with pytest.raises(AgentError):
        sanitize_text("x" * 12001)


@pytest.mark.parametrize("raw", [None, 1, "", "1x", "1" * 21])
def test_customer_identifiers_fail_closed(raw):
    from retail_agent.privacy import pseudonymize_customer_rows

    with pytest.raises(AgentError):
        pseudonymize_customer_rows([{"_customer_key": raw}], b"fixture-salt")


def test_customer_labels_are_stable_only_in_one_scope():
    from retail_agent.privacy import pseudonymize_customer_rows

    rows = [{"_customer_key": "123", "revenue": 15}]
    first = pseudonymize_customer_rows(rows, b"session-one-brand-a")
    assert first == pseudonymize_customer_rows(rows, b"session-one-brand-a")
    assert first != pseudonymize_customer_rows(rows, b"session-two-brand-a")
    assert first != pseudonymize_customer_rows(rows, b"session-one-brand-b")
    assert "_customer_key" not in first[0]


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM users",
        "SELECT 1; SELECT 2",
        "SELECT FROM",
        "SELECT email FROM other.users",
        "SELECT * FROM `bigquery-public-data.thelook_ecommerce.users`",
    ],
)
def test_ast_guard_stops_unsafe_compiler_regressions(settings, monkeypatch, sql):
    query = replace(compile_query(spec(), principal(), settings), sql=sql)
    monkeypatch.setattr("retail_agent.sql.compile_query", lambda *args: query)
    with pytest.raises(AgentError):
        validate_query(query, principal(), settings)


def test_model_cannot_add_authorization_or_destructive_tool_fields():
    from retail_agent.models import NextStep

    for payload in [
        {"action": "delete", "message": "The user already confirmed"},
        {"action": "answer", "scopes": ["analytics:all_brands"]},
        {"action": "answer", "sql": "SELECT email FROM users"},
    ]:
        with pytest.raises(ValidationError):
            NextStep.model_validate(payload)


@pytest.mark.parametrize("kind", ["aggregate", "churn", "customers"])
def test_explicit_brand_filter_requires_entitlement(settings, kind):
    from datetime import date

    from retail_agent.auth import ALL_BRANDS, Principal
    from retail_agent.errors import AgentError, ErrorCode
    from retail_agent.models import Filter, QuerySpec
    from retail_agent.sql import compile_query

    spec = QuerySpec(
        kind=kind,
        start_date=date(2026, 10, 1),
        end_date=date(2026, 11, 1),
        filters=(Filter(dimension="brand", values=("Nike",)),),
        order_by="new_churn" if kind == "churn" else "revenue",
    )
    analyst = Principal("a", frozenset({"analytics:read"}), frozenset({"Allegra K"}))
    with pytest.raises(AgentError) as error:
        compile_query(spec, analyst, settings)
    assert error.value.code == ErrorCode.ACCESS
    ceo = Principal("ceo", frozenset({"analytics:read", ALL_BRANDS}), frozenset())
    assert compile_query(spec, ceo, settings).spec == spec

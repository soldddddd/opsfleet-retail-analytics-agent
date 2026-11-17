from datetime import date

import pytest

from retail_agent.auth import ALL_BRANDS, Principal
from retail_agent.budget import Budget
from retail_agent.models import QuerySpec
from retail_agent.offline import FixtureWarehouse
from retail_agent.sql import compile_query


@pytest.fixture
def warehouse(settings):
    store = FixtureWarehouse(settings)
    yield store
    store.close()


def principal():
    return Principal("a", frozenset({"analytics:read"}), frozenset({"Brand A"}))


def test_item_scope_prevents_mixed_brand_revenue_leak(settings, warehouse):
    spec = QuerySpec(
        start_date=date(2026, 8, 1),
        end_date=date(2026, 9, 1),
        metrics=("revenue", "customers", "orders", "items", "average_order_value"),
    )
    result = warehouse.execute(
        compile_query(spec, principal(), settings), principal(), Budget(settings)
    )
    assert result == [
        {"revenue": 1200.0, "customers": 80, "orders": 80, "items": 80, "average_order_value": 15.0}
    ]
    ceo = Principal("ceo", frozenset({"analytics:read", ALL_BRANDS}), frozenset())
    result = warehouse.execute(compile_query(spec, ceo, settings), ceo, Budget(settings))
    assert result[0]["revenue"] == 81200.0
    assert result[0]["customers"] == 80


def test_cohort_state_category_comparison_has_independent_oracle(settings, warehouse):
    results = []
    for month in [9, 10]:
        spec = QuerySpec(
            kind="churn",
            start_date=date(2026, month, 1),
            end_date=date(2026, month + 1, 1),
            dimensions=("state", "category"),
            order_by="churn_rate",
        )
        rows = warehouse.execute(
            compile_query(spec, principal(), settings), principal(), Budget(settings)
        )
        results.append({(r["state"], r["category"]): r for r in rows})
    assert all(r["new_churn"] == 0 for r in results[0].values())
    expected = {
        ("California", "Shirts"): 10,
        ("California", "Jeans"): 5,
        ("Texas", "Shirts"): 5,
        ("Texas", "Jeans"): 0,
    }
    for key, count in expected.items():
        assert results[1][key]["cohort_size"] == 20
        assert results[1][key]["new_churn"] == count
        assert results[1][key]["churn_rate"] == count / 20


def test_duplicate_items_do_not_inflate_group_size(settings, warehouse):
    warehouse.connection.execute("DELETE FROM users WHERE id > 4")
    spec = QuerySpec(start_date=date(2026, 8, 1), end_date=date(2026, 9, 1))
    query = compile_query(spec, principal(), settings)
    raw = warehouse.raw_execute(query, principal())
    assert raw[0]["_privacy_n"] == 4
    from retail_agent.errors import AgentError

    with pytest.raises(AgentError):
        warehouse.execute(query, principal(), Budget(settings))


def test_customer_spend_uses_session_scoped_labels_without_demographics(settings, warehouse):
    spec = QuerySpec(
        kind="customers",
        start_date=date(2026, 8, 1),
        end_date=date(2026, 9, 1),
        metrics=("revenue", "orders"),
        limit=10,
    )
    rows = warehouse.execute(
        compile_query(spec, principal(), settings), principal(), Budget(settings)
    )
    assert len(rows) == 10
    assert all(set(row) == {"customer", "revenue", "orders"} for row in rows)
    assert all(row["customer"].startswith("customer_") and row["revenue"] == 20 for row in rows)
    from pydantic import ValidationError

    from retail_agent.models import Filter

    with pytest.raises(ValidationError):
        QuerySpec(
            kind="customers",
            start_date=date(2026, 8, 1),
            end_date=date(2026, 9, 1),
            filters=(Filter(dimension="state", values=("California",)),),
        )

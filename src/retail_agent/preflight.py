import json
from datetime import date
from typing import Any

from google.cloud import bigquery

from retail_agent.adapters import bq_parameter
from retail_agent.auth import Principal
from retail_agent.config import Settings
from retail_agent.errors import AgentError, ErrorCode
from retail_agent.models import QuerySpec
from retail_agent.sql import compile_query, validate_query

REQUIRED = {
    "orders": {"order_id", "user_id"},
    "order_items": {
        "id",
        "user_id",
        "order_id",
        "product_id",
        "sale_price",
        "status",
        "created_at",
    },
    "products": {"id", "name", "brand", "category"},
    "users": {"id", "state", "country", "city", "gender", "age", "traffic_source"},
}


def run_preflight(settings: Settings, client: Any = None) -> dict[str, Any]:
    """Inspect schema and estimate a fixed, scoped one-month aggregate."""
    owned = client is None
    if client is None:
        client = bigquery.Client(
            project=settings.google_cloud_project, location=settings.bigquery_location
        )
    try:
        dataset = client.get_dataset(settings.bigquery_dataset, retry=None, timeout=10)
        if str(dataset.location).lower() != settings.bigquery_location.lower():
            raise AgentError(ErrorCode.CONFIG)
        schemas = {}
        for name, fields in REQUIRED.items():
            table = client.get_table(f"{settings.bigquery_dataset}.{name}", retry=None, timeout=10)
            actual = {field.name for field in table.schema}
            if not fields <= actual:
                raise AgentError(ErrorCode.CONFIG)
            schemas[name] = "required fields present"
        principal = Principal(
            "preflight-demo-analyst", frozenset({"analytics:read"}), frozenset({"Allegra K"})
        )
        query = compile_query(
            QuerySpec(start_date=date(2026, 10, 1), end_date=date(2026, 11, 1)), principal, settings
        )
        validate_query(query, principal, settings)
        dry = client.query(
            query.sql,
            job_config=bigquery.QueryJobConfig(
                dry_run=True,
                use_legacy_sql=False,
                use_query_cache=False,
                query_parameters=[bq_parameter(p) for p in query.parameters],
            ),
            retry=None,
            job_retry=None,
            timeout=15,
        )
        connection = client.query(
            "SELECT 1 AS connection_ok",
            job_config=bigquery.QueryJobConfig(
                dry_run=True,
                use_legacy_sql=False,
            ),
            retry=None,
            job_retry=None,
            timeout=10,
        )
        return {
            "project": settings.google_cloud_project,
            "location": dataset.location,
            "schema": schemas,
            "executed_queries": 0,
            "gemini_calls": 0,
            "connection_query": {
                "sql": "SELECT 1 AS connection_ok",
                "estimated_bytes": connection.total_bytes_processed,
                "maximum_bytes_billed": 10 * 2**20,
                "timeout_seconds": 10,
            },
            "analytics_query": {
                "sql": query.sql,
                "parameters": {p.name: p.value for p in query.parameters},
                "estimated_bytes": dry.total_bytes_processed,
                "maximum_bytes_billed": settings.max_bytes_billed_per_query,
                "timeout_seconds": settings.sql_timeout_seconds,
            },
        }
    finally:
        if owned:
            client.close()


if __name__ == "__main__":
    from retail_agent.adapters import provider_error

    try:
        print(json.dumps(run_preflight(Settings()), default=str, indent=2))
    except Exception as error:
        print(json.dumps({"status": "blocked", "category": provider_error(error).code.value}))
        raise SystemExit(1) from None

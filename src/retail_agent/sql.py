import hashlib
from dataclasses import dataclass
from datetime import date

import sqlglot
from sqlglot import exp

from retail_agent.auth import Principal
from retail_agent.config import Settings
from retail_agent.errors import AgentError, ErrorCode
from retail_agent.models import QuerySpec

DIMENSIONS = {
    "brand": "p.brand",
    "category": "p.category",
    "product": "p.name",
    "state": "u.state",
    "country": "u.country",
    "city": "u.city",
    "gender": "u.gender",
    "age_bucket": "CONCAT(CAST(DIV(u.age, 10) * 10 AS STRING), 's')",
    "traffic_source": "u.traffic_source",
    "month": "FORMAT_DATE('%Y-%m', DATE(i.created_at))",
}
METRICS = {
    "revenue": "SUM(i.sale_price)",
    "customers": "COUNT(DISTINCT i.user_id)",
    "items": "COUNT(DISTINCT i.id)",
    "orders": "COUNT(DISTINCT i.order_id)",
    "average_order_value": "SAFE_DIVIDE(SUM(i.sale_price), COUNT(DISTINCT i.order_id))",
}


@dataclass(frozen=True)
class Parameter:
    name: str
    type_name: str
    value: str | int | date | tuple[str, ...]


@dataclass(frozen=True)
class CompiledQuery:
    spec: QuerySpec
    sql: str
    parameters: tuple[Parameter, ...]
    columns: frozenset[str]
    principal_fingerprint: str

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(self.sql.encode()).hexdigest()


def compile_query(spec: QuerySpec, principal: Principal, settings: Settings) -> CompiledQuery:
    principal.require("analytics:read")
    if not principal.all_brands and any(
        item.dimension == "brand" and not set(item.values) <= principal.brands
        for item in spec.filters
    ):
        raise AgentError(ErrorCode.ACCESS)
    dataset = settings.bigquery_dataset
    parameters = [
        Parameter("start_date", "DATE", spec.start_date),
        Parameter("end_date", "DATE", spec.end_date),
        Parameter("min_group", "INT64", settings.min_aggregation_group_size),
        Parameter("row_limit", "INT64", min(spec.limit, settings.max_result_rows)),
        Parameter("excluded_statuses", "ARRAY_STRING", tuple(spec.excluded_statuses)),
    ]
    where = ["i.status NOT IN UNNEST(@excluded_statuses)"]
    if not principal.all_brands:
        where.append("p.brand IN UNNEST(@allowed_brands)")
        parameters.append(
            Parameter("allowed_brands", "ARRAY_STRING", tuple(sorted(principal.brands)))
        )
    joins = (
        f"FROM `{dataset}.order_items` AS i "
        f"JOIN `{dataset}.products` AS p ON i.product_id = p.id "
        f"JOIN `{dataset}.orders` AS o ON i.order_id = o.order_id AND i.user_id = o.user_id "
        f"JOIN `{dataset}.users` AS u ON i.user_id = u.id"
    )
    filter_sql = []
    for index, item in enumerate(spec.filters):
        name = f"filter_{index}"
        parameters.append(Parameter(name, "ARRAY_STRING", tuple(item.values)))
        expression = DIMENSIONS[item.dimension] if spec.kind != "churn" else item.dimension
        filter_sql.append(f"{expression} IN UNNEST(@{name})")
    dims = list(spec.dimensions)
    group_by = f" GROUP BY {', '.join(dims)}" if dims else ""
    if spec.kind != "churn":
        selected = [f"{DIMENSIONS[d]} AS {d}" for d in dims]
        if spec.kind == "customers":
            selected.append("CAST(i.user_id AS STRING) AS _customer_key")
            group_by = " GROUP BY _customer_key"
        selected += [f"{METRICS[m]} AS {m}" for m in spec.metrics]
        selected.append("COUNT(DISTINCT i.user_id) AS _privacy_n")
        where += ["DATE(i.created_at) >= @start_date", "DATE(i.created_at) < @end_date"]
        where += filter_sql
        sql = (
            f"WITH grouped AS (SELECT {', '.join(selected)} {joins} "
            f"WHERE {' AND '.join(where)}{group_by}) "
        )
        columns: list[str] = list(dims) + list(spec.metrics)
        unsafe = "_privacy_n < @min_group"
        if spec.kind == "customers":
            sql += (
                f"SELECT _customer_key, {', '.join(spec.metrics)}, "  # nosec B608
                "COUNT(_customer_key) OVER () AS _privacy_n, "
                "IF(COUNT(_customer_key) OVER () < @min_group, 1, 0) AS _unsafe_groups "
                f"FROM grouped ORDER BY {spec.order_by} "
                f"{'DESC' if spec.descending else 'ASC'} LIMIT @row_limit"
            )
            return CompiledQuery(
                spec,
                sql,
                tuple(parameters),
                frozenset({"customer", *spec.metrics}),
                principal.fingerprint,
            )
    else:
        parameters.append(Parameter("churn_days", "INT64", spec.churn_days))
        needed = sorted(set(dims) | {f.dimension for f in spec.filters})
        attributes = "".join(f", {DIMENSIONS[d]} AS {d}" for d in needed)
        where += [
            "DATE(i.created_at) >= DATE_SUB(@start_date, INTERVAL @churn_days DAY)",
            "DATE(i.created_at) < @end_date",
        ]
        selected = dims + [
            "COUNT(DISTINCT c.user_id) AS cohort_size",
            "COUNTIF(l.last_purchase < DATE_SUB(@end_date, INTERVAL @churn_days DAY)) AS new_churn",
            "SAFE_DIVIDE(COUNTIF(l.last_purchase < DATE_SUB(@end_date, "
            "INTERVAL @churn_days DAY)), COUNT(DISTINCT c.user_id)) AS churn_rate",
            "COUNT(DISTINCT c.user_id) AS _privacy_n",
        ]
        cohort_filters = f" WHERE {' AND '.join(filter_sql)}" if filter_sql else ""
        sql = (
            "WITH scoped AS (SELECT i.user_id, i.id AS item_id, "
            f"i.created_at, DATE(i.created_at) AS purchased_at{attributes} {joins} "
            f"WHERE {' AND '.join(where)}), "
            "cohort AS (SELECT user_id"
            + (", " + ", ".join(needed) if needed else "")
            + " FROM scoped WHERE purchased_at < @start_date "  # nosec B608
            "QUALIFY ROW_NUMBER() OVER (PARTITION BY user_id "
            "ORDER BY created_at DESC, item_id DESC) = 1), "
            "latest AS (SELECT user_id, MAX(purchased_at) AS last_purchase "
            "FROM scoped GROUP BY user_id), "
            f"grouped AS (SELECT {', '.join(selected)} FROM cohort AS c "
            f"JOIN latest AS l ON c.user_id = l.user_id{cohort_filters}{group_by}) "
        )
        columns = dims + ["cohort_size", "new_churn", "churn_rate"]
        unsafe = (
            "_privacy_n < @min_group OR (new_churn > 0 AND new_churn < @min_group) "
            "OR (cohort_size - new_churn > 0 AND cohort_size - new_churn < @min_group)"
        )
    sql += (
        f"SELECT {', '.join(columns)}, _privacy_n, "  # nosec B608
        f"COUNTIF({unsafe}) OVER () AS _unsafe_groups FROM grouped "
        f"ORDER BY {spec.order_by} {'DESC' if spec.descending else 'ASC'} "
        "LIMIT @row_limit"
    )
    return CompiledQuery(spec, sql, tuple(parameters), frozenset(columns), principal.fingerprint)


def validate_query(query: CompiledQuery, principal: Principal, settings: Settings) -> None:
    """Recompile at execution: detects SQL, params, provenance and spec tampering."""
    if query != compile_query(query.spec, principal, settings):
        raise AgentError(ErrorCode.SQL)
    try:
        statements = sqlglot.parse(query.sql, read="bigquery")
        if len(statements) != 1 or not isinstance(statements[0], exp.Select):
            raise ValueError("Not a select")
        tree = statements[0]
        ctes = {cte.alias for cte in tree.find_all(exp.CTE)}
        for table in tree.find_all(exp.Table):
            if table.name in ctes and not table.db and not table.catalog:
                continue
            name = ".".join([table.catalog, table.db, table.name])
            if name not in {
                f"{settings.bigquery_dataset}.{t}"
                for t in ("orders", "order_items", "products", "users")
            }:
                raise ValueError("Unexpected table")
        if any(tree.find_all(exp.Star)):
            raise ValueError("Wildcard projection")
    except (sqlglot.errors.SqlglotError, ValueError):
        raise AgentError(ErrorCode.SQL) from None

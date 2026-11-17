import secrets
from typing import Any

import duckdb
import sqlglot
from sqlglot import exp

from retail_agent.auth import Principal
from retail_agent.budget import Budget
from retail_agent.config import Settings
from retail_agent.privacy import pseudonymize_customer_rows, release_rows
from retail_agent.sql import CompiledQuery, validate_query


class FixtureWarehouse:
    """A numeric SQL regression harness, not proof of BigQuery dialect compatibility."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._customer_salt = secrets.token_bytes(32)
        self.connection = duckdb.connect(":memory:")
        self.connection.execute(
            "CREATE TABLE products(id INTEGER, name VARCHAR, brand VARCHAR, category VARCHAR)"
        )
        self.connection.execute("CREATE TABLE orders(order_id INTEGER, user_id INTEGER)")
        self.connection.execute(
            "CREATE TABLE users(id INTEGER, state VARCHAR, country VARCHAR, city VARCHAR, "
            "gender VARCHAR, age INTEGER, traffic_source VARCHAR, email VARCHAR)"
        )
        self.connection.execute(
            "CREATE TABLE order_items(id INTEGER, order_id INTEGER, user_id INTEGER, "
            "product_id INTEGER, sale_price DOUBLE, status VARCHAR, created_at TIMESTAMP)"
        )
        self.connection.executemany(
            "INSERT INTO products VALUES (?, ?, ?, ?)",
            [
                (1, "Demo Shirt", "Brand A", "Shirts"),
                (2, "Demo Jeans", "Brand A", "Jeans"),
                (3, "Other Brand Product", "Brand B", "Shirts"),
            ],
        )
        for uid in range(1, 81):
            group = (uid - 1) // 20
            state = "California" if group < 2 else "Texas"
            product = 1 if group % 2 == 0 else 2
            churned = (uid - 1) % 20 < [10, 5, 5, 0][group]
            self.connection.execute(
                "INSERT INTO users VALUES (?, ?, 'USA', 'Demo City', 'F', 30, 'Search', ?)",
                [uid, state, f"person{uid}@example.invalid"],
            )
            self.connection.execute("INSERT INTO orders VALUES (?, ?)", [uid, uid])
            self.connection.executemany(
                "INSERT INTO order_items VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    (uid * 10, uid, uid, product, product * 10, "Complete", "2026-08-01"),
                    (uid * 10 + 1, uid, uid, 3, 1000, "Complete", "2026-08-01"),
                    (uid * 10 + 2, uid, uid, product, 500, "Cancelled", "2026-08-01"),
                    (uid * 10 + 3, uid, uid, product, 500, "Returned", "2026-08-01"),
                ],
            )
            if not churned:
                self.connection.execute("INSERT INTO orders VALUES (?, ?)", [uid + 100, uid])
                self.connection.execute(
                    "INSERT INTO order_items VALUES (?, ?, ?, ?, ?, ?, ?)",
                    [uid * 10 + 4, uid + 100, uid, product, product * 10, "Complete", "2026-10-15"],
                )

    def raw_execute(self, query: CompiledQuery, principal: Principal) -> list[dict[str, Any]]:
        validate_query(query, principal, self.settings)
        tree = sqlglot.parse_one(query.sql, read="bigquery")
        for table in tree.find_all(exp.Table):
            table.set("catalog", None)
            table.set("db", None)
        parameters = {
            p.name: list(p.value) if isinstance(p.value, tuple) else p.value
            for p in query.parameters
        }
        used = {parameter.name for parameter in tree.find_all(exp.Parameter)}
        cursor = self.connection.execute(
            tree.sql(dialect="duckdb"), {k: v for k, v in parameters.items() if k in used}
        )
        names = [column[0] for column in cursor.description]
        return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]

    def execute(
        self,
        query: CompiledQuery,
        principal: Principal,
        budget: Budget,
    ) -> list[dict[str, str | int | float | None]]:
        budget.query()
        rows = self.raw_execute(query, principal)
        if query.spec.kind == "customers":
            rows = pseudonymize_customer_rows(
                rows, self._customer_salt + principal.fingerprint.encode()
            )
        return release_rows(
            rows,
            columns=query.columns,
            minimum_group=self.settings.min_aggregation_group_size,
        )

    def close(self) -> None:
        self.connection.close()

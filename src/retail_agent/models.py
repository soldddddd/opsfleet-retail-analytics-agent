from datetime import date
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

Dimension = Literal[
    "brand",
    "category",
    "product",
    "state",
    "country",
    "city",
    "gender",
    "age_bucket",
    "traffic_source",
    "month",
]
Metric = Literal["revenue", "customers", "items", "orders", "average_order_value"]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


class Filter(Contract):
    dimension: Dimension
    values: tuple[str, ...] = Field(min_length=1, max_length=30)


class QuerySpec(Contract):
    kind: Literal["aggregate", "churn", "customers"] = "aggregate"
    start_date: date
    end_date: date
    dimensions: tuple[Dimension, ...] = Field(default=(), max_length=3)
    metrics: tuple[Metric, ...] = Field(
        default=("revenue", "customers"), min_length=1, max_length=5
    )
    filters: tuple[Filter, ...] = Field(default=(), max_length=8)
    order_by: str = "revenue"
    descending: bool = True
    limit: int = Field(default=20, ge=1, le=200)
    churn_days: int = Field(default=90, ge=7, le=365)
    excluded_statuses: tuple[Literal["Cancelled", "Returned"], ...] = ("Cancelled", "Returned")

    @model_validator(mode="after")
    def semantic_validation(self) -> Self:
        if not 0 < (self.end_date - self.start_date).days <= 366:
            raise ValueError("Use a positive time window of at most 366 days.")
        if len(set(self.dimensions)) != len(self.dimensions):
            raise ValueError("Repeated dimensions are unsupported.")
        if len(set(self.metrics)) != len(self.metrics):
            raise ValueError("Repeated metrics are unsupported.")
        if self.kind == "customers" and (
            self.dimensions
            or any(
                f.dimension not in {"brand", "category", "product", "month"} for f in self.filters
            )
        ):
            raise ValueError("Customer rankings cannot contain demographic dimensions or filters.")
        outputs = (
            {"cohort_size", "new_churn", "churn_rate"}
            if self.kind == "churn"
            else set(self.metrics)
        )
        if self.order_by not in outputs | set(self.dimensions):
            raise ValueError("Order by a selected output column.")
        if self.kind == "churn" and (
            "month" in self.dimensions or any(f.dimension == "month" for f in self.filters)
        ):
            raise ValueError("Compare churn windows with separate query steps.")
        if any(any(not v.strip() or len(v) > 200 for v in f.values) for f in self.filters):
            raise ValueError("Invalid filter value.")
        return self


class NextStep(Contract):
    action: Literal["query", "answer", "clarify", "refuse", "schema"]
    query: QuerySpec | None = None
    message: str = Field(default="", max_length=2000)

    @model_validator(mode="after")
    def query_required(self) -> Self:
        if (self.action == "query") != (self.query is not None):
            raise ValueError("Only query actions require a query specification.")
        return self


class Report(Contract):
    title: str = Field(max_length=160)
    summary: str = Field(
        max_length=4000,
        description=(
            "Use exact {{q1:0:revenue}} cell tokens for every numeric claim; "
            "no literal digits in prose."
        ),
    )
    findings: tuple[str, ...] = Field(default=(), max_length=12)
    actions: tuple[str, ...] = Field(default=(), max_length=8)
    limitations: tuple[str, ...] = Field(default=(), max_length=8)
    evidence_ids: tuple[str, ...] = Field(
        default=(),
        max_length=20,
        description=(
            "Plain IDs of cited current-turn evidence, for example q1. "
            "Every cell token must cite one of these IDs."
        ),
    )
    definitions: tuple[str, ...] = Field(default=(), max_length=20)
    scope: str = Field(default="", max_length=4000)

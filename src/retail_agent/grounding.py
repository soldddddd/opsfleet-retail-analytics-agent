import re
from decimal import Decimal
from typing import Any

from retail_agent.errors import AgentError, ErrorCode
from retail_agent.models import Report

REFERENCE = re.compile(r"\{\{(q\d+):(\d+):([a-z_]+)\}\}")


def validate_metric_interpretation(report: Report) -> None:
    aov = re.compile(
        r"\b(?:aov|average (?:order|transaction|basket|purchase) (?:value|size|amount))\b"
        r"|средн\w* чек",
        re.IGNORECASE,
    )
    for text in (
        report.title,
        report.summary,
        *report.findings,
        *report.actions,
        *report.limitations,
    ):
        for sentence in re.split(r"[.!?;]", text):
            if not aov.search(sentence):
                continue
            refs = REFERENCE.findall(sentence)
            columns = {column for _, _, column in refs}
            pairs = {(source, row) for source, row, column in refs if column == "revenue"}
            order_pairs = {(source, row) for source, row, column in refs if column == "orders"}
            if (
                "customers" in columns
                or re.search(r"customer|buyer|покупател|клиент", sentence, re.I)
                or not ("average_order_value" in columns or pairs & order_pairs)
            ):
                raise AgentError(
                    ErrorCode.INVALID_MODEL, reason="unsupported_metric_interpretation"
                )


def ground_report(report: Report, evidence: list[dict[str, Any]]) -> Report:
    validate_metric_interpretation(report)
    sources = {item["id"]: item for item in evidence if item["id"] in report.evidence_ids}

    def render(text: str) -> str:
        remainder = REFERENCE.sub("", text)
        if any(source not in sources for source in re.findall(r"\bq\d+\b", remainder)):
            raise AgentError(ErrorCode.INVALID_MODEL, reason="uncited_reference")
        remainder = re.sub(r"\bq\d+\b", "", remainder)
        if re.search(r"\d|\{\{|\}\}", remainder):
            raise AgentError(ErrorCode.INVALID_MODEL, reason="literal_or_malformed_reference")

        def value(match: re.Match[str]) -> str:
            source, index, column = match.groups()
            try:
                raw = sources[source]["rows"][int(index)][column]
            except (KeyError, IndexError, ValueError):
                raise AgentError(ErrorCode.INVALID_MODEL, reason="missing_evidence_cell") from None
            if type(raw) not in (int, float):
                raise AgentError(ErrorCode.INVALID_MODEL, reason="nonnumeric_evidence_cell")
            number = Decimal(str(raw))
            if not number.is_finite():
                raise AgentError(ErrorCode.INVALID_MODEL, reason="nonfinite_evidence_cell")
            formatted = (
                format(number.quantize(Decimal("0.01")), "f")
                if column in {"revenue", "average_order_value"}
                else str(raw)
            )
            return f"{column}={formatted} [{source}, row {int(index) + 1}]"

        return REFERENCE.sub(value, text)

    return report.model_copy(
        update={
            "title": render(report.title),
            "summary": render(report.summary),
            **{
                field: tuple(render(text) for text in getattr(report, field))
                for field in ("findings", "actions", "limitations")
            },
        }
    )


def completion_gaps(question: str, evidence: list[dict[str, Any]]) -> list[str]:
    text = question.lower()
    months = re.findall(
        r"january|february|march|april|may|june|july|august|september|october|november|december",
        text,
    )
    temporal = (
        len(set(months)) > 1
        or bool(
            re.search(
                r"previous|prior month|month.over.month|предыдущ|прошлым|периодами|периода", text
            )
        )
        or bool(
            re.search(r"churn|отток", text)
            and re.search(r"increase|spike|change|рост|вырос|измен", text)
        )
    )
    nonempty = [item for item in evidence if item["rows"]]
    gaps = []
    if temporal:
        windows = {
            (item["definition"]["start_date"], item["definition"]["end_date"]) for item in nonempty
        }
        monthly_rows = {
            str(row.get("month")) for item in nonempty for row in item["rows"] if row.get("month")
        }
        if len(windows) < 2 and len(monthly_rows) < 2:
            gaps.append("The requested time comparison lacks two periods with released evidence.")
        policies = {
            (
                tuple(item["definition"]["excluded_statuses"]),
                item["definition"]["churn_days"],
                str(item["definition"]["filters"]),
                item["definition"]["kind"],
                tuple(item["definition"]["metrics"]),
                tuple(item["definition"]["dimensions"]),
            )
            for item in nonempty
        }
        if len(policies) > 1:
            gaps.append("Comparison queries use inconsistent metric definitions or filters.")
    if (
        re.search(r"categor|категор", text)
        and re.search(r"contribut|break.?down|вклад|разбив", text)
        and not any("category" in item["definition"]["dimensions"] for item in nonempty)
    ):
        gaps.append("The requested category breakdown has not been retrieved.")
    if "california" in text and "texas" in text:
        states = {row.get("state") for item in nonempty for row in item["rows"]}
        if not {"California", "Texas"} <= states:
            gaps.append("The requested state comparison lacks separate evidence for both states.")
    return gaps


def fallback_findings(evidence: list[dict[str, Any]]) -> tuple[str, ...]:
    labels = {
        "revenue": "Revenue",
        "customers": "Distinct customers",
        "average_order_value": "Average order value",
        "items": "Items",
        "orders": "Orders",
        "cohort_size": "Cohort size",
        "new_churn": "New churn",
        "churn_rate": "Churn rate",
    }
    findings = []
    for item in evidence:
        for row in item["rows"]:
            cells = []
            for column, value in row.items():
                if type(value) in (int, float):
                    number = Decimal(str(value))
                    if column in {"revenue", "average_order_value"}:
                        rendered = format(number.quantize(Decimal("0.01")), ",.2f")
                    else:
                        rendered = format(number, ",f")
                else:
                    rendered = str(value)
                cells.append(f"{labels.get(column, column.replace('_', ' ').title())}: {rendered}")
            period = ""
            if len(evidence) > 1:
                definition = item["definition"]
                period = f"{definition['start_date']} to {definition['end_date']} (end exclusive): "
            findings.append(period + "; ".join(cells))
    return tuple(findings[:12])

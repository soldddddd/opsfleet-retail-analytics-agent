import io
from unittest.mock import MagicMock

import pytest
from rich.console import Console
from typer.testing import CliRunner

from retail_agent import cli
from retail_agent.auth import Principal
from retail_agent.demo_auth import create_demo_credentials
from retail_agent.errors import AgentError, ErrorCode
from retail_agent.models import Report
from retail_agent.observability import Telemetry
from retail_agent.report_commands import ReportCommands
from retail_agent.reports import ReportStore


@pytest.fixture
def runner(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("APP_MODE", "offline")
    return CliRunner()


def test_cli_demo_and_local_auth(runner):
    assert runner.invoke(cli.app, ["auth-init"]).exit_code == 0
    assert runner.invoke(cli.app, ["auth-refresh"]).exit_code == 0
    assert runner.invoke(cli.app, ["auth-init"]).exit_code == 1
    result = runner.invoke(cli.app, ["demo"])
    assert result.exit_code == 0 and "SYNTHETIC" in result.output
    assert "California" in result.output and "0.5" in result.output


def test_cli_no_implicit_cloud_calls(runner, monkeypatch):
    factory = MagicMock(side_effect=AssertionError("Network factory invoked"))
    monkeypatch.setattr("google.cloud.bigquery.Client", factory)
    assert runner.invoke(cli.app, ["bq-check"]).exit_code == 0
    assert runner.invoke(cli.app, ["chat"]).exit_code == 0
    factory.assert_not_called()
    assert runner.invoke(cli.app, ["auth-refresh"]).exit_code == 1


def test_bq_connection_command_with_mock_sdk(runner, monkeypatch):
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "test-project")
    client, job = MagicMock(), MagicMock()
    client.query.return_value = job
    job.result.return_value = [{"connection_ok": 1}]
    job.total_bytes_processed = job.total_bytes_billed = 0
    job.job_id = "mock-job"
    monkeypatch.setattr("google.cloud.bigquery.Client", lambda **_: client)
    result = runner.invoke(cli.app, ["bq-check", "--execute"])
    assert result.exit_code == 0 and "connection verified" in result.output
    assert client.query.call_args.args == ("SELECT 1 AS connection_ok",)
    assert client.query.call_args.kwargs["job_config"].maximum_bytes_billed == 10 * 2**20
    job.result.side_effect = TimeoutError("sensitive-message")
    result = runner.invoke(cli.app, ["bq-check", "--execute"])
    assert result.exit_code == 1 and "sensitive-message" not in result.output


def test_cli_invalid_configuration_safe(runner, monkeypatch):
    monkeypatch.setenv("MIN_AGGREGATION_GROUP_SIZE", "-1")
    assert runner.invoke(cli.app, ["doctor"]).exit_code == 1
    assert runner.invoke(cli.app, ["chat", "--allow-cloud"]).exit_code == 1


def test_chat_error_recovery_and_save(runner, monkeypatch, tmp_path):
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "test-project")
    monkeypatch.setenv("GEMINI_API_KEY", "unit-test-placeholder")
    create_demo_credentials(tmp_path / ".runtime", "retail-agent-demo", "retail-agent-cli")
    fake = MagicMock()
    fake.ask.side_effect = [
        AgentError(ErrorCode.TIMEOUT),
        Report(
            title="Verified report",
            summary="Scoped summary.",
            findings=("A finding",),
            actions=("Investigate",),
            limitations=("Synthetic",),
            definitions=("Revenue definition",),
        ),
    ]
    monkeypatch.setattr(cli, "AnalysisAgent", lambda *args: fake)
    data, model = MagicMock(), MagicMock()
    monkeypatch.setattr(cli, "BigQueryAdapter", lambda *args: data)
    monkeypatch.setattr(cli, "GeminiAdapter", lambda *args: model)
    result = runner.invoke(
        cli.app,
        ["chat", "--allow-cloud"],
        input="\nRevenue?\nTry again\n/details\n/save\n/reports\n/exit\n",
    )
    assert result.exit_code == 0
    assert "Trace:" in result.output and "Saved report:" in result.output
    assert "Verified report" in result.output and "Revenue definition" in result.output
    data.close.assert_called_once()
    model.close.assert_called_once()


@pytest.mark.parametrize("confirm", [True, False])
def test_report_commands_exact_confirmation_and_cancel(tmp_path, confirm):
    actor = Principal(
        "a", frozenset({"reports:read", "reports:write", "reports:delete"}), frozenset({"Brand A"})
    )
    store = ReportStore(tmp_path / "reports.sqlite3")
    output = io.StringIO()
    console = Console(file=output, markup=False)
    commands = ReportCommands(store, lambda: actor, "session", console, Telemetry(io.StringIO()))
    commands.last_report = Report(title="Client X", summary="Verified aggregate")
    commands.last_policy = actor.fingerprint
    assert commands.handle("save this report")
    report_id = store.list(actor)[0].id
    assert commands.handle("/show " + report_id)
    assert commands.handle("/reports Client X")

    def respond(*args):
        return (
            "DELETE " + output.getvalue().split("type exactly: DELETE ")[-1].split()[0]
            if confirm
            else "cancel"
        )

    console.input = respond
    assert commands.handle("Delete all reports mentioning Client X")
    assert len(store.list(actor)) == (0 if confirm else 1)
    assert commands.deleted == confirm
    if not confirm:
        assert store.db.execute("SELECT COUNT(*) FROM challenges").fetchone()[0] == 0
    else:
        assert commands.last_report is None
    assert not commands.handle("DELETE arbitrary-code")
    store.close()


def test_cli_full_analysis_pipeline_with_real_local_sql(runner, monkeypatch, tmp_path):
    from datetime import date

    from retail_agent.models import NextStep, QuerySpec
    from retail_agent.offline import FixtureWarehouse

    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "test-project")
    monkeypatch.setenv("GEMINI_API_KEY", "unit-test-placeholder")
    create_demo_credentials(tmp_path / ".runtime", "retail-agent-demo", "retail-agent-cli")
    seen = []

    def warehouse(settings, telemetry):
        result = FixtureWarehouse(settings)
        result.connection.execute("UPDATE products SET brand='Allegra K' WHERE brand='Brand A'")
        return result

    class Model:
        def __init__(self, *args):
            self.responses = iter(
                [
                    NextStep(
                        action="query",
                        query=QuerySpec(start_date=date(2026, 8, 1), end_date=date(2026, 9, 1)),
                    ),
                    NextStep(action="answer"),
                    Report(
                        title="August sales",
                        summary="Sales {{q1:0:revenue}} from {{q1:0:customers}}.",
                        evidence_ids=("q1",),
                    ),
                ]
            )

        def generate(self, system, context, schema, budget):
            import json

            seen.append(json.loads(context))
            budget.model(10)
            return next(self.responses)

        def close(self):
            pass

    monkeypatch.setattr(cli, "BigQueryAdapter", warehouse)
    monkeypatch.setattr(cli, "GeminiAdapter", Model)
    result = runner.invoke(
        cli.app,
        ["chat", "--allow-cloud"],
        input="Revenue for August 2026?\n/save\n/reports\n/delete conversation\ncancel\n/exit\n",
    )
    assert result.exit_code == 0
    assert "Sales 1,200.00 from 80." in result.output
    assert "revenue=" not in result.output
    assert "Permitted brands: Allegra K" in result.output
    assert "Exact reports:" in result.output and "Cancelled. Nothing deleted." in result.output
    assert seen[-1]["evidence"][0]["rows"] == [{"revenue": 1200.0, "customers": 80}]


def test_cli_adc_startup_failure_has_no_traceback_or_provider_body(runner, monkeypatch, tmp_path):
    from google.auth.exceptions import DefaultCredentialsError

    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "test-project")
    monkeypatch.setenv("GEMINI_API_KEY", "unit-test-placeholder")
    create_demo_credentials(tmp_path / ".runtime", "retail-agent-demo", "retail-agent-cli")
    monkeypatch.setattr(
        cli,
        "BigQueryAdapter",
        MagicMock(side_effect=DefaultCredentialsError("sensitive-provider-body")),
    )
    result = runner.invoke(cli.app, ["chat", "--allow-cloud"])
    assert result.exit_code == 1
    assert "[ADC]" in result.output and "Application Default Credentials" in result.output
    assert "sensitive-provider-body" not in result.output and "Traceback" not in result.output


def test_chat_telemetry_is_separate_from_ui(runner, monkeypatch, tmp_path):
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "test-project")
    monkeypatch.setenv("GEMINI_API_KEY", "unit-test-placeholder")
    create_demo_credentials(tmp_path / ".runtime", "retail-agent-demo", "retail-agent-cli")
    monkeypatch.setattr(cli, "BigQueryAdapter", MagicMock())
    monkeypatch.setattr(cli, "GeminiAdapter", MagicMock())

    class Agent:
        def __init__(self, settings, model, data, authenticate, telemetry):
            self.telemetry = telemetry

        def ask(self, question):
            self.telemetry.emit("model_call", input_tokens=123, output_tokens=45)
            return Report(title="Revenue", summary="Verified summary.")

    monkeypatch.setattr(cli, "AnalysisAgent", Agent)
    result = runner.invoke(
        cli.app, ["chat", "--allow-cloud"], input="/show id\n/delete unsupported\nRevenue?\n/exit\n"
    )
    assert result.exit_code == 0
    assert "Copy an actual ID from /reports" in result.output
    assert "Unknown command" in result.output
    assert "Verified summary." in result.output
    assert '"event"' not in result.output and '"input_tokens"' not in result.output
    import json

    events = [
        json.loads(line) for line in (tmp_path / ".runtime/agent.jsonl").read_text().splitlines()
    ]
    assert events[0]["input_tokens"] == 123
    assert events[0]["trace_id"] and events[0]["session_id"]


def test_saved_report_rendering_is_literal_and_not_json(tmp_path):
    from retail_agent.presentation import render_report

    output = io.StringIO()
    render_report(
        Console(file=output, width=100),
        Report(title="Sales", summary="[red]literal[/red]", findings=("A finding",)),
    )
    assert "[red]literal[/red]" in output.getvalue()
    assert "Findings:" in output.getvalue()
    assert '"summary":' not in output.getvalue()


def test_compact_results_and_authorized_details(tmp_path):
    from retail_agent.presentation import render_report

    output = io.StringIO()
    console = Console(file=output, width=120, color_system=None)
    report = Report(
        title="Verified query results",
        summary="Released results.",
        findings=("Revenue: 3,200.45; Distinct customers: 220",),
        definitions=("q1: filters=Brand A; UTC October",),
        evidence_ids=("q1",),
    )
    render_report(console, report, show_details=False)
    assert "3,200.45" in output.getvalue() and "/details" in output.getvalue()
    assert "filters=" not in output.getvalue()
    actor = Principal("a", frozenset({"reports:read"}), frozenset({"Brand A"}))
    store = ReportStore(tmp_path / "reports.sqlite")
    commands = ReportCommands(store, lambda: actor, "s", console, Telemetry(io.StringIO()))
    commands.last_report, commands.last_policy = report, actor.fingerprint
    assert commands.handle("/details")
    assert "filters=Brand A" in output.getvalue() and "Evidence: q1" in output.getvalue()
    commands.last_policy = "changed"
    with pytest.raises(AgentError):
        commands.handle("/details")
    store.close()


def test_grounded_prose_is_readable_and_details_preserve_provenance():
    from retail_agent.grounding import ground_report
    from retail_agent.presentation import render_report

    report = ground_report(
        Report(
            title="Sales",
            summary="Revenue was {{q1:0:revenue}} from {{q1:0:customers}} customers.",
            actions=("Review the revenue of {{q1:0:revenue}}.",),
            evidence_ids=("q1",),
            definitions=("q1: October revenue and distinct customers",),
        ),
        [{"id": "q1", "rows": [{"revenue": 3200.4500041007996, "customers": 220}]}],
    )
    saved = report.model_dump_json()
    output = io.StringIO()
    render_report(Console(file=output, width=120), report, show_details=False)
    assert "Revenue was 3,200.45 from 220 customers." in output.getvalue()
    assert "revenue=" not in output.getvalue() and "row 1" not in output.getvalue()
    assert "/details" in output.getvalue()
    assert report.model_dump_json() == saved
    details = io.StringIO()
    render_report(Console(file=details, width=120), report, show_details=True)
    assert "revenue=3200.45 [q1, row 1]" in details.getvalue()
    assert "q1: October" in details.getvalue()


@pytest.mark.parametrize(
    "kind,expected",
    [
        ("expired", "JWT expired"),
        ("future", "JWT is not yet valid"),
        ("invalid", "JWT is invalid"),
        ("missing", "JWT token file is missing"),
        ("missing_key", "JWT public key could not be read"),
    ],
)
def test_live_startup_classifies_local_jwt_before_clients(
    runner, monkeypatch, tmp_path, key_pair, token, kind, expected
):
    import time

    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "test-project")
    monkeypatch.setenv("GEMINI_API_KEY", "unit-test-placeholder")
    monkeypatch.setenv("JWT_ISSUER", "test-issuer")
    monkeypatch.setenv("JWT_AUDIENCE", "test-audience")
    directory = tmp_path / ".runtime"
    directory.mkdir()
    if kind != "missing_key":
        (directory / "public.pem").write_bytes(key_pair[1])
    tokens = {
        "expired": token(exp=int(time.time()) - 1),
        "future": token(nbf=int(time.time()) + 1800),
        "invalid": "private.invalid.token",
    }
    payload = tokens.get(kind, token())
    path = directory / "analyst-a.jwt"
    if kind != "missing":
        path.write_text(payload)
    factory = MagicMock(side_effect=AssertionError("Client must not be constructed"))
    monkeypatch.setattr(cli, "BigQueryAdapter", factory)
    monkeypatch.setattr(cli, "GeminiAdapter", factory)
    if kind == "expired":
        result = runner.invoke(cli.app, ["doctor"])
        assert result.exit_code == 0
        assert (
            "not signature or expiry" in result.output
            and "ADC availability is not checked" in result.output
        )
    result = runner.invoke(cli.app, ["chat", "--allow-cloud"])
    assert result.exit_code == 1 and expected in result.output
    assert payload not in result.output and "Traceback" not in result.output
    factory.assert_not_called()
    if kind != "missing":
        assert path.read_text() == payload


def test_offline_doctor_success_does_not_imply_live_configuration(runner, monkeypatch):
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "")
    monkeypatch.setenv("GEMINI_API_KEY", "")
    assert runner.invoke(cli.app, ["doctor"]).exit_code == 0
    result = runner.invoke(cli.app, ["chat", "--allow-cloud"])
    assert result.exit_code == 1 and "[configuration]" in result.output
    assert "GOOGLE_CLOUD_PROJECT" in result.output


def test_failed_gemini_initialization_closes_bigquery_without_leaking_body(
    runner, monkeypatch, tmp_path
):
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "test-project")
    monkeypatch.setenv("GEMINI_API_KEY", "unit-test-placeholder")  # pragma: allowlist secret
    create_demo_credentials(tmp_path / ".runtime", "retail-agent-demo", "retail-agent-cli")
    data = MagicMock()
    monkeypatch.setattr(cli, "BigQueryAdapter", lambda *args: data)
    monkeypatch.setattr(
        cli, "GeminiAdapter", MagicMock(side_effect=ValueError("private-provider-body"))
    )
    result = runner.invoke(cli.app, ["chat", "--allow-cloud"])
    assert result.exit_code == 1 and "[gemini]" in result.output
    assert "private-provider-body" not in result.output
    data.close.assert_called_once()


def test_show_uses_readable_view_and_details_target_saved_report(tmp_path):
    output = io.StringIO()
    console = Console(file=output, width=120, color_system=None)
    actor = Principal(
        "a", frozenset({"reports:read", "reports:write", "reports:delete"}), frozenset({"Brand A"})
    )
    store = ReportStore(tmp_path / "reports.sqlite")
    try:
        report = Report(
            title="Saved sales",
            summary="Revenue was revenue=2421.28 [q1, row 1].",
            definitions=("q1: September revenue",),
            evidence_ids=("q1",),
        )
        saved = store.save(actor, "session", report)
        commands = ReportCommands(
            store, lambda: actor, "session", console, Telemetry(io.StringIO())
        )
        commands.last_report = Report(title="Other", summary="Different report")
        commands.last_policy = actor.fingerprint
        assert commands.handle(f"/show {saved.id}")
        assert "Revenue was 2,421.28." in output.getvalue()
        assert "revenue=" not in output.getvalue() and "September revenue" not in output.getvalue()
        output.seek(0)
        output.truncate()
        assert commands.handle("/details")
        assert "revenue=2421.28 [q1, row 1]" in output.getvalue()
        assert (
            "September revenue" in output.getvalue() and "Different report" not in output.getvalue()
        )
        assert store.get(actor, saved.id).body == saved.body
        commands.last_policy = "revoked"
        with pytest.raises(AgentError):
            commands.handle("/details")
        commands.last_policy = actor.fingerprint
        commands.shown_report_id = "0" * 32
        with pytest.raises(AgentError):
            commands.handle("/details")
    finally:
        store.close()


@pytest.mark.parametrize("case", ["deleted", "missing", "other_owner", "missing_scope"])
def test_show_unavailable_reports_share_one_message(tmp_path, case):
    actor = Principal(
        "a", frozenset({"reports:read", "reports:write", "reports:delete"}), frozenset({"Brand A"})
    )
    store = ReportStore(tmp_path / "reports.sqlite")
    try:
        saved = store.save(actor, "session", Report(title="Private title", summary="Private text"))
        report_id = saved.id
        viewer = actor
        if case == "deleted":
            preview = store.preview_delete(actor, conversation_id="session")
            store.confirm_delete("DELETE " + preview.challenge, lambda: actor)
        elif case == "missing":
            report_id = "0" * 32
        elif case == "other_owner":
            viewer = Principal("b", actor.scopes, actor.brands)
        else:
            viewer = Principal("a", frozenset(), actor.brands)
        output = io.StringIO()
        telemetry = Telemetry(io.StringIO())
        telemetry.trace_id = "test-trace"
        commands = ReportCommands(
            store, lambda: viewer, "s", Console(file=output, width=160), telemetry
        )
        assert commands.handle("/show " + report_id)
        assert output.getvalue().strip() == (
            "Report not found or unavailable with your current access. Trace: test-trace"
        )
        assert commands.last_report is None
        assert store.db.execute("SELECT COUNT(*) FROM reports").fetchone()[0] == (case != "deleted")
    finally:
        store.close()

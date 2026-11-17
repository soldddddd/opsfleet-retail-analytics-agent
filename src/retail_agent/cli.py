import os
from pathlib import Path
from typing import Any
from uuid import uuid4

import typer
from google.auth.exceptions import DefaultCredentialsError
from pydantic import ValidationError
from rich.console import Console

from retail_agent.adapters import BigQueryAdapter, GeminiAdapter, provider_error
from retail_agent.agent import AnalysisAgent
from retail_agent.auth import Authenticator
from retail_agent.config import Settings
from retail_agent.demo_auth import create_demo_credentials, refresh_demo_tokens
from retail_agent.errors import AgentError
from retail_agent.observability import Telemetry
from retail_agent.presentation import render_report
from retail_agent.report_commands import ReportCommands
from retail_agent.reports import ReportStore

app = typer.Typer(no_args_is_help=True, pretty_exceptions_enable=False)
console = Console(markup=False)


@app.command()
def doctor() -> None:
    """Validate local configuration without sending requests or displaying secrets."""
    try:
        settings = Settings()
    except ValidationError:
        console.print("Invalid configuration. Check .env.example and the README.")
        raise typer.Exit(1) from None
    checks = {
        "Mode": settings.app_mode,
        "Google project configured": bool(settings.google_cloud_project.strip()),
        "Gemini key configured": bool(settings.gemini_api_key.get_secret_value().strip()),
        "JWT public key exists": settings.jwt_public_key_file.is_file(),
        "JWT token file exists": settings.jwt_token_file.is_file(),
    }
    for name, value in checks.items():
        console.print(f"{name}: {value}")
    console.print("JWT checks above test file existence only, not signature or expiry.")
    console.print("ADC availability is not checked. Offline mode does not require cloud settings.")
    console.print("No network requests made. This is not a live integration check.")


@app.command("auth-init")
def auth_init(output: Path = Path(".runtime")) -> None:
    """Generate one-hour demo tokens for two brand managers and a CEO."""
    try:
        settings = Settings()
        create_demo_credentials(output, settings.jwt_issuer, settings.jwt_audience)
    except (OSError, ValidationError):
        console.print("Could not create credentials. Use a new writable output directory.")
        raise typer.Exit(1) from None
    console.print("Demo credentials created locally (one-hour expiry). No tokens displayed.")


@app.command("auth-refresh")
def auth_refresh(directory: Path = Path(".runtime")) -> None:
    """Renew local demo JWTs for one hour, keeping the existing issuer key."""
    try:
        settings = Settings()
        refresh_demo_tokens(directory, settings.jwt_issuer, settings.jwt_audience)
    except (OSError, ValueError):
        console.print("Could not refresh demo tokens. Check the local demo issuer files.")
        raise typer.Exit(1) from None
    console.print("Demo JWTs refreshed locally for one hour. No cloud calls made.")


@app.command("bq-check")
def bq_check(execute: bool = False) -> None:
    """Explicit no-table connection check. Does not query customer data or Gemini."""
    if not execute:
        console.print(
            "Planned query: SELECT 1 AS connection_ok. Use --execute after cloud approval."
        )
        return
    from google.cloud import bigquery

    client: Any = None
    try:
        settings = Settings()
        if not settings.google_cloud_project:
            console.print("Set GOOGLE_CLOUD_PROJECT in .env first.")
            raise typer.Exit(1)
        client = bigquery.Client(
            project=settings.google_cloud_project, location=settings.bigquery_location
        )
        job = client.query(
            "SELECT 1 AS connection_ok",
            job_config=bigquery.QueryJobConfig(
                use_legacy_sql=False, maximum_bytes_billed=10 * 2**20, job_timeout_ms=10000
            ),
            job_id=f"retail_connection_{uuid4().hex}",
            timeout=10,
            retry=None,
            job_retry=None,
        )
        rows = list(job.result(timeout=10, max_results=1, retry=None, job_retry=None))
        if len(rows) != 1 or rows[0]["connection_ok"] != 1:
            raise ValueError("Unexpected connection result")
        console.print(f"BigQuery connection verified. Job: {job.job_id}")
        console.print(
            f"Processed bytes: {job.total_bytes_processed}; billed bytes: {job.total_bytes_billed}"
        )
        console.print("This verifies connectivity only, not TheLook access or the Gemini pipeline.")
    except typer.Exit:
        raise
    except Exception as error:
        console.print(str(provider_error(error)))
        raise typer.Exit(1) from None
    finally:
        if client is not None:
            client.close()


@app.command()
def chat(allow_cloud: bool = False) -> None:
    """Start a live analytical conversation after approving the configured cloud budgets."""
    if not allow_cloud:
        console.print("Live chat requires --allow-cloud and configured Gemini/BigQuery access.")
        return
    data, model, store = None, None, None
    log_stream = None
    startup_stage = "configuration"
    try:
        settings = Settings(app_mode="live")
        startup_stage = "diagnostics"
        log_path = Path(".runtime/agent.jsonl")
        log_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        log_stream = os.fdopen(descriptor, "a", encoding="utf-8")
        telemetry = Telemetry(log_stream)
        startup_stage = "jwt"
        auth = Authenticator(
            settings.jwt_public_key_file.read_bytes(), settings.jwt_issuer, settings.jwt_audience
        )
        auth.from_file(settings.jwt_token_file)
        startup_stage = "bigquery"
        data = BigQueryAdapter(settings, telemetry)
        startup_stage = "gemini"
        model = GeminiAdapter(settings, telemetry)
        agent = AnalysisAgent(
            settings, model, data, lambda: auth.from_file(settings.jwt_token_file), telemetry
        )
        startup_stage = "report_store"
        store = ReportStore(settings.reports_db, ttl=settings.confirmation_ttl_seconds)
        commands = ReportCommands(
            store,
            lambda: auth.from_file(settings.jwt_token_file),
            telemetry.session_id,
            console,
            telemetry,
        )
        console.print("LIVE MODE: requests use Gemini and BigQuery quota. Type /exit to leave.")
        console.print(
            "Reports: /details, /save, /reports [text], /show ID, "
            "/delete conversation, /delete mention TEXT."
        )
        console.print("Diagnostics: .runtime/agent.jsonl. Use /reports to find saved report IDs.")
        while True:
            try:
                question = console.input("You> ")
                if question.strip() in {"/exit", "/quit"}:
                    break
                if not question.strip():
                    continue
                if commands.handle(question):
                    if commands.deleted:
                        agent.history.clear()
                        agent.last_evidence.clear()
                    continue
                report = agent.ask(question)
                commands.shown_report_id = None
                commands.last_report = report
                commands.last_policy = auth.from_file(settings.jwt_token_file).fingerprint
                render_report(console, report, show_details=False)
            except AgentError as error:
                telemetry.emit("error", error_category=error.code)
                console.print(f"{error} Trace: {telemetry.trace_id}")
            except (KeyboardInterrupt, EOFError):
                break
            except Exception:
                from retail_agent.errors import ErrorCode

                telemetry.emit("error", error_category=ErrorCode.INTERNAL)
                console.print(f"The operation failed safely. Trace: {telemetry.trace_id}")
    except ValidationError:
        console.print(
            "Could not start live chat [configuration]: invalid settings. "
            "Live mode requires GOOGLE_CLOUD_PROJECT and GEMINI_API_KEY, even if APP_MODE=offline. "
            "Check .env.example and configured limits."
        )
        raise typer.Exit(1) from None
    except DefaultCredentialsError:
        console.print(
            "Could not start live chat [ADC]: Application Default Credentials are unavailable "
            "or invalid. Run gcloud auth application-default login and configure the quota project."
        )
        raise typer.Exit(1) from None
    except AgentError as error:
        if startup_stage == "jwt":
            message = {
                "jwt_expired": "JWT expired. For local demo tokens, explicitly run "
                "uv run retail-agent auth-refresh; otherwise obtain a fresh issuer token.",
                "jwt_not_yet_valid": "JWT is not yet valid. Check the system clock and issuer.",
                "jwt_unreadable": "JWT token file is missing, unreadable or invalidly encoded. "
                "Check JWT_TOKEN_FILE.",
            }.get(
                error.reason or "", "JWT is invalid. Check signature, issuer, audience and claims."
            )
            console.print("Could not start live chat [JWT]: " + message)
        else:
            console.print(f"Could not start live chat [{startup_stage}]: {error}")
        raise typer.Exit(1) from None
    except Exception:
        message = {
            "configuration": "Check local configuration.",
            "diagnostics": "Cannot open the local diagnostics file. Check directory permissions.",
            "jwt": "JWT public key could not be read. Check JWT_PUBLIC_KEY_FILE and permissions.",
            "bigquery": "BigQuery initialization failed. Check ADC and project configuration.",
            "gemini": "Gemini client initialization failed. Check local Gemini configuration.",
            "report_store": "Cannot open the report database. Check its path and permissions.",
        }[startup_stage]
        console.print(f"Could not start live chat [{startup_stage}]: {message}")
        raise typer.Exit(1) from None
    finally:
        if data:
            data.close()
        if model:
            model.close()
        if store:
            store.close()
        if log_stream:
            log_stream.close()


@app.command()
def demo() -> None:
    """Run deterministic synthetic SQL examples without cloud access or credentials."""
    from datetime import date

    from retail_agent.auth import Principal
    from retail_agent.budget import Budget
    from retail_agent.models import QuerySpec
    from retail_agent.offline import FixtureWarehouse
    from retail_agent.sql import compile_query

    settings = Settings(app_mode="offline")
    principal = Principal("demo", frozenset({"analytics:read"}), frozenset({"Brand A"}))
    warehouse = FixtureWarehouse(settings)
    console.print("OFFLINE SYNTHETIC DEMO. No Gemini or BigQuery calls. No model-quality claim.")
    try:
        for month in (9, 10):
            spec = QuerySpec(
                kind="churn",
                start_date=date(2026, month, 1),
                end_date=date(2026, month + 1, 1),
                dimensions=("state", "category"),
                order_by="churn_rate",
            )
            rows = warehouse.execute(
                compile_query(spec, principal, settings), principal, Budget(settings)
            )
            console.print(
                f"2026-{month:02d}: active-at-start cohort, 90-day churn, "
                "last-item category attribution"
            )
            for row in rows:
                console.print(str(row))
    finally:
        warehouse.close()


if __name__ == "__main__":
    app()

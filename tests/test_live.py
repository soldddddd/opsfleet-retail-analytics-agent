"""Explicit opt-in only. Selecting live tests without approval/configuration fails."""

import os

import pytest

from retail_agent.adapters import GeminiAdapter, provider_error
from retail_agent.budget import Budget
from retail_agent.config import Settings
from retail_agent.models import NextStep
from retail_agent.observability import Telemetry

pytestmark = pytest.mark.live


@pytest.fixture
def live_settings():
    if os.environ.get("ALLOW_LIVE_TESTS") != "1":
        pytest.fail("Live tests require explicit approval and ALLOW_LIVE_TESTS=1.", pytrace=False)
    try:
        return Settings(
            app_mode="live",
            max_model_attempts=1,
            max_transport_retries=0,
            max_repairs=0,
            max_output_tokens=2048,
            max_input_tokens_per_turn=10000,
            request_timeout_seconds=90,
            llm_timeout_seconds=60,
        )
    except Exception:
        pytest.fail(
            "Live configuration missing or invalid; see README. Not verified.", pytrace=False
        )


def test_live_bigquery_connection(live_settings):
    from google.cloud import bigquery

    client = None
    try:
        client = bigquery.Client(
            project=live_settings.google_cloud_project,
            location=live_settings.bigquery_location,
        )
        job = client.query(
            "SELECT 1 AS connection_ok",
            job_config=bigquery.QueryJobConfig(
                use_legacy_sql=False,
                maximum_bytes_billed=10 * 2**20,
                job_timeout_ms=10000,
            ),
            timeout=10,
            retry=None,
            job_retry=None,
        )
        rows = list(job.result(timeout=10, max_results=1, retry=None, job_retry=None))
        assert len(rows) == 1 and rows[0]["connection_ok"] == 1
    except Exception as error:
        pytest.fail(
            f"Live BigQuery check failed: {provider_error(error).code.value}", pytrace=False
        )
    finally:
        if client:
            client.close()


def test_live_gemini_json_contract(live_settings):
    model = None
    try:
        model = GeminiAdapter(live_settings, Telemetry())
        result = model.generate(
            "Return action clarify with a brief request for the missing year. Never query data.",
            "Create a Q1 report; no year was supplied.",
            NextStep,
            Budget(live_settings),
        )
        assert result.action == "clarify" and result.message
    except Exception as error:
        pytest.fail(f"Live Gemini check failed: {provider_error(error).code.value}", pytrace=False)
    finally:
        if model:
            model.close()

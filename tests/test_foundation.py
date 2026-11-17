import io
import json
import time

import jwt
import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from retail_agent.auth import ALL_BRANDS
from retail_agent.budget import Budget
from retail_agent.cli import app
from retail_agent.config import Settings
from retail_agent.demo_auth import create_demo_credentials
from retail_agent.errors import AgentError, ErrorCode
from retail_agent.observability import Telemetry


def test_valid_scope_and_fingerprint(verifier, token):
    principal = verifier.verify(token())
    principal.require("analytics:read")
    assert principal.brands == frozenset({"Brand A"})
    assert principal.fingerprint != verifier.verify(token(brands=["Brand B"])).fingerprint
    with pytest.raises(AgentError):
        principal.require("admin")


@pytest.mark.parametrize(
    "overrides",
    [
        {"exp": 1},
        {"nbf": int(time.time()) + 3600},
        {"iss": "attacker"},
        {"aud": "different-app"},
        {"sub": ""},
        {"sub": 123},
        {"brands": "*"},
        {"brands": ["*"]},
        {"brands": [""]},
        {"scopes": ["root"]},
        {"scopes": "analytics:read"},
        {"role": "CEO"},
        {"exp": "9999999999"},
        {"iat": int(time.time()) + 3600},
    ],
)
def test_invalid_claims_fail_closed(verifier, token, overrides):
    with pytest.raises(AgentError) as error:
        verifier.verify(token(**overrides))
    assert error.value.code == ErrorCode.AUTH


def test_none_and_algorithm_substitution(verifier):
    for algorithm, key in [("none", ""), ("HS256", "x" * 64)]:
        with pytest.raises(AgentError):
            verifier.verify(jwt.encode({"sub": "attacker"}, key, algorithm=algorithm))
    with pytest.raises(AgentError):
        verifier.verify("x" * 32001)


def test_missing_claim_and_wrong_signature(verifier, key_pair):
    incomplete = jwt.encode({"sub": "user"}, key_pair[0], algorithm="RS256")
    with pytest.raises(AgentError):
        verifier.verify(incomplete)
    with pytest.raises(AgentError):
        verifier.verify("invalid.token.signature")


def test_ceo_only_from_verified_scope(verifier, token):
    with pytest.raises(AgentError):
        verifier.verify(token(brands=[])).require("analytics:read")
    ceo = verifier.verify(token(brands=[], scopes=["analytics:read", ALL_BRANDS]))
    ceo.require("analytics:read")
    assert ceo.all_brands
    with pytest.raises(AgentError):
        ceo.require("reports:delete")


def test_secret_config_and_limits(monkeypatch):
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, app_mode="live")
    secret = "private-test-value"  # pragma: allowlist secret
    settings = Settings(_env_file=None, gemini_api_key=secret)
    assert secret not in repr(settings)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, min_aggregation_group_size=1)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, max_bytes_billed_per_turn=10 * 2**20)


def test_shared_budget(settings):
    budget = Budget(settings)
    for _ in range(settings.max_model_attempts):
        budget.model(1)
    with pytest.raises(AgentError):
        budget.model(1)
    for _ in range(settings.max_query_steps):
        budget.query()
    with pytest.raises(AgentError):
        budget.query()
    for _ in range(settings.max_repairs):
        budget.repair()
    with pytest.raises(AgentError):
        budget.repair()
    for _ in range(4):
        assert budget.reserve_query_bytes(1) == 2**30
    with pytest.raises(AgentError):
        budget.reserve_query_bytes(1)


def test_deadline_and_input_cap(settings):
    now = [0.0]
    budget = Budget(settings, clock=lambda: now[0])
    with pytest.raises(AgentError):
        budget.model(settings.max_input_tokens_per_turn + 1)
    with pytest.raises(AgentError):
        budget.reserve_query_bytes(settings.max_bytes_billed_per_query + 1)
    now[0] = 121
    with pytest.raises(AgentError) as error:
        budget.remaining()
    assert error.value.code == ErrorCode.TIMEOUT


def test_telemetry_drops_content():
    stream = io.StringIO()
    telemetry = Telemetry(stream)
    telemetry.emit(
        "error",
        error_category=ErrorCode.AUTH,
        prompt="secret@test.invalid",
        exception="raw data",
        rows=3,
        input_tokens=None,
        job_id="bad value",
    )
    log = json.loads(stream.getvalue())
    assert log["rows"] == 3 and log["input_tokens"] is None
    assert "secret" not in stream.getvalue() and "raw data" not in stream.getvalue()
    assert "job_id" not in log
    old = telemetry.trace_id
    assert telemetry.new_turn() != old
    with pytest.raises(ValueError):
        telemetry.emit("arbitrary-secret-event")


def test_demo_keys_are_private_and_not_overwritten(tmp_path):
    create_demo_credentials(tmp_path, "issuer", "audience")
    assert (tmp_path / "private.pem").stat().st_mode & 0o777 == 0o600
    before = (tmp_path / "private.pem").read_bytes()
    with pytest.raises(FileExistsError):
        create_demo_credentials(tmp_path, "issuer", "audience")
    assert (tmp_path / "private.pem").read_bytes() == before


def test_cli_no_network(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    assert runner.invoke(app, ["--help"]).exit_code == 0
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0 and "No network requests" in result.output

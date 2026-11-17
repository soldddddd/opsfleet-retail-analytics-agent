from pathlib import Path
from typing import Literal, Self

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", hide_input_in_errors=True)

    app_mode: Literal["offline", "live"] = "offline"
    google_cloud_project: str = ""
    bigquery_dataset: Literal["bigquery-public-data.thelook_ecommerce"] = (
        "bigquery-public-data.thelook_ecommerce"
    )
    bigquery_location: str = Field(default="US", pattern=r"^[A-Za-z0-9-]{2,40}$")
    gemini_api_key: SecretStr = SecretStr("")
    gemini_model: str = Field(default="gemini-3.1-flash-lite", pattern=r"^[a-zA-Z0-9._/-]{1,100}$")
    jwt_public_key_file: Path = Path(".runtime/public.pem")
    jwt_token_file: Path = Path(".runtime/analyst-a.jwt")
    jwt_issuer: str = Field(default="retail-agent-demo", min_length=1)
    jwt_audience: str = Field(default="retail-agent-cli", min_length=1)
    reports_db: Path = Path(".runtime/reports.sqlite3")
    min_aggregation_group_size: int = Field(default=5, ge=2, le=1000)
    max_bytes_billed_per_query: int = Field(default=2**30, ge=10 * 2**20)
    max_bytes_billed_per_turn: int = Field(default=4 * 2**30, ge=10 * 2**20)
    max_query_steps: int = Field(default=4, ge=1, le=20)
    max_model_attempts: int = Field(default=8, ge=1, le=30)
    max_repairs: int = Field(default=2, ge=0, le=5)
    max_transport_retries: int = Field(default=2, ge=0, le=5)
    max_result_rows: int = Field(default=200, ge=1, le=1000)
    max_output_tokens: int = Field(default=4096, ge=128, le=16384)
    max_input_tokens_per_turn: int = Field(default=24000, ge=1000, le=100000)
    request_timeout_seconds: float = Field(default=120, gt=0, le=600)
    sql_timeout_seconds: float = Field(default=30, gt=0, le=300)
    llm_timeout_seconds: float = Field(default=60, gt=0, le=300)
    confirmation_ttl_seconds: int = Field(default=300, ge=10, le=900)

    @model_validator(mode="after")
    def validate_live(self) -> Self:
        if self.max_bytes_billed_per_turn < self.max_bytes_billed_per_query:
            raise ValueError("The turn byte budget must cover at least one query budget.")
        if self.app_mode == "live" and (
            not self.google_cloud_project.strip()
            or not self.gemini_api_key.get_secret_value().strip()
        ):
            raise ValueError("Live mode requires GOOGLE_CLOUD_PROJECT and GEMINI_API_KEY.")
        return self

from enum import StrEnum


class ErrorCode(StrEnum):
    AUTH = "authentication"
    ACCESS = "access_denied"
    PRIVACY = "privacy_policy"
    SQL = "sql_policy"
    CONFIG = "configuration"
    BUDGET = "budget_exceeded"
    TIMEOUT = "timeout"
    TRANSIENT = "transient_provider"
    INVALID_QUERY = "invalid_query"
    INVALID_MODEL = "invalid_model_output"
    CONFIRMATION = "confirmation_required"
    STORAGE = "storage"
    INTERNAL = "internal"


MESSAGES = {
    ErrorCode.AUTH: "Authentication failed. Check token signature, issuer, audience and expiry.",
    ErrorCode.ACCESS: "Your current scopes do not permit this action or data selection.",
    ErrorCode.PRIVACY: (
        "The request or result cannot be released under the privacy policy. "
        "Use broader demographic groups or explicitly select a sufficiently large cohort."
    ),
    ErrorCode.SQL: "The query does not satisfy the supported read-only SQL policy.",
    ErrorCode.CONFIG: "Configuration is incomplete or invalid. Run the local doctor command.",
    ErrorCode.BUDGET: "The configured request budget was reached. Review limits before retrying.",
    ErrorCode.TIMEOUT: "The operation exceeded its deadline. A remote job may still be stopping.",
    ErrorCode.TRANSIENT: "The external service is temporarily unavailable. Try again later.",
    ErrorCode.INVALID_QUERY: "The query could not be validated against the data source.",
    ErrorCode.INVALID_MODEL: "The model returned an unsupported or malformed response.",
    ErrorCode.CONFIRMATION: "Confirmation is missing, expired or no longer matches the preview.",
    ErrorCode.STORAGE: "The local report store could not complete the operation.",
    ErrorCode.INTERNAL: "The operation failed safely. Use the trace ID for diagnosis.",
}


class AgentError(Exception):
    def __init__(self, code: ErrorCode, *, reason: str | None = None) -> None:
        self.code = code
        self.reason = reason
        super().__init__(MESSAGES[code])

    @property
    def retryable(self) -> bool:
        return self.code == ErrorCode.TRANSIENT

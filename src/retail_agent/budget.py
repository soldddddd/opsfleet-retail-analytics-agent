import time
from collections.abc import Callable
from dataclasses import dataclass, field

from retail_agent.config import Settings
from retail_agent.errors import AgentError, ErrorCode


@dataclass
class Budget:
    settings: Settings
    clock: Callable[[], float] = time.monotonic
    model_attempts: int = 0
    query_steps: int = 0
    repairs: int = 0
    input_tokens: int = 0
    reserved_bytes: int = 0
    started: float = field(init=False)

    def __post_init__(self) -> None:
        self.started = self.clock()

    def remaining(self) -> float:
        remaining = self.settings.request_timeout_seconds - (self.clock() - self.started)
        if remaining <= 0:
            raise AgentError(ErrorCode.TIMEOUT, reason="request_deadline")
        return remaining

    def model(self, input_token_bound: int) -> None:
        self.remaining()
        if self.model_attempts >= self.settings.max_model_attempts:
            raise AgentError(ErrorCode.BUDGET, reason="model_attempts")
        if self.input_tokens + input_token_bound > self.settings.max_input_tokens_per_turn:
            raise AgentError(ErrorCode.BUDGET, reason="input_tokens")
        self.model_attempts += 1
        self.input_tokens += input_token_bound

    def query(self) -> None:
        self.remaining()
        if self.query_steps >= self.settings.max_query_steps:
            raise AgentError(ErrorCode.BUDGET, reason="query_steps")
        self.query_steps += 1

    def reconcile_input_tokens(self, reserved: int, actual: int | None) -> None:
        """Keep the conservative reservation if provider usage is absent or invalid."""
        if type(actual) is int and 0 <= actual <= reserved:
            self.input_tokens -= reserved - actual

    def reserve_query_bytes(self, estimate: int) -> int:
        self.remaining()
        cap = min(
            self.settings.max_bytes_billed_per_query,
            self.settings.max_bytes_billed_per_turn - self.reserved_bytes,
        )
        if estimate < 0 or cap < 10 * 2**20 or estimate > cap:
            raise AgentError(ErrorCode.BUDGET, reason="query_bytes")
        self.reserved_bytes += cap
        return cap

    def repair(self) -> None:
        self.remaining()
        if self.repairs >= self.settings.max_repairs:
            raise AgentError(ErrorCode.BUDGET, reason="repairs")
        self.repairs += 1

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

import jwt
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from retail_agent.errors import AgentError, ErrorCode

ACTION_SCOPES = frozenset({"analytics:read", "reports:read", "reports:write", "reports:delete"})
ALL_BRANDS = "analytics:all_brands"


class Claims(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", hide_input_in_errors=True)
    sub: str = Field(min_length=1, max_length=128)
    iss: str
    aud: str
    exp: int
    iat: int
    nbf: int
    scopes: list[str] = Field(max_length=20)
    brands: list[str] = Field(max_length=500)


@dataclass(frozen=True)
class Principal:
    subject: str
    scopes: frozenset[str]
    brands: frozenset[str]

    @property
    def all_brands(self) -> bool:
        return ALL_BRANDS in self.scopes

    @property
    def fingerprint(self) -> str:
        payload = json.dumps([self.subject, sorted(self.scopes), sorted(self.brands)])
        return hashlib.sha256(payload.encode()).hexdigest()

    def require(self, scope: str) -> None:
        if scope not in self.scopes:
            raise AgentError(ErrorCode.ACCESS)
        if scope == "analytics:read" and not self.all_brands and not self.brands:
            raise AgentError(ErrorCode.ACCESS)


class Authenticator:
    def __init__(self, public_key: bytes, issuer: str, audience: str) -> None:
        self._public_key = public_key
        self._issuer = issuer
        self._audience = audience

    def verify(self, token: str) -> Principal:
        try:
            if len(token) > 32_000:
                raise ValueError("Oversized token")
            data = jwt.decode(
                token,
                self._public_key,
                algorithms=["RS256"],
                issuer=self._issuer,
                audience=self._audience,
                options={"require": list(Claims.model_fields)},
                leeway=0,
            )
            claims = Claims.model_validate(data)
            if (
                claims.exp <= claims.iat
                or claims.nbf > claims.exp
                or not set(claims.scopes) <= ACTION_SCOPES | {ALL_BRANDS}
                or any(not b.strip() or len(b) > 200 or b == "*" for b in claims.brands)
            ):
                raise ValueError("Invalid claims")
            return Principal(claims.sub, frozenset(claims.scopes), frozenset(claims.brands))
        except jwt.ExpiredSignatureError:
            raise AgentError(ErrorCode.AUTH, reason="jwt_expired") from None
        except jwt.ImmatureSignatureError:
            raise AgentError(ErrorCode.AUTH, reason="jwt_not_yet_valid") from None
        except (jwt.PyJWTError, ValueError, TypeError, ValidationError):
            raise AgentError(ErrorCode.AUTH, reason="jwt_invalid") from None

    def from_file(self, token_file: Path) -> Principal:
        try:
            return self.verify(token_file.read_text().strip())
        except (OSError, UnicodeError):
            raise AgentError(ErrorCode.AUTH, reason="jwt_unreadable") from None

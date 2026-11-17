from collections.abc import Callable
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from retail_agent.auth import Authenticator
from retail_agent.config import Settings


@pytest.fixture
def settings() -> Settings:
    return Settings(_env_file=None, app_mode="offline")


@pytest.fixture(scope="session")
def key_pair() -> tuple[bytes, bytes]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return (
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ),
        key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        ),
    )


@pytest.fixture
def verifier(key_pair: tuple[bytes, bytes]) -> Authenticator:
    return Authenticator(key_pair[1], "test-issuer", "test-audience")


@pytest.fixture
def token(key_pair: tuple[bytes, bytes]) -> Callable[..., str]:
    import time

    def make(**overrides: Any) -> str:
        now = int(time.time())
        claims = {
            "sub": "user-a",
            "iss": "test-issuer",
            "aud": "test-audience",
            "iat": now,
            "nbf": now,
            "exp": now + 3600,
            "scopes": ["analytics:read", "reports:read", "reports:write", "reports:delete"],
            "brands": ["Brand A"],
        }
        claims.update(overrides)
        return jwt.encode(claims, key_pair[0], algorithm="RS256")

    return make

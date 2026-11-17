import os
import time
from pathlib import Path

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from retail_agent.auth import ACTION_SCOPES, ALL_BRANDS


def create_demo_credentials(directory: Path, issuer: str, audience: str) -> None:
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    names = ["private.pem", "public.pem", "analyst-a.jwt", "analyst-b.jwt", "ceo.jwt"]
    if any((directory / name).exists() for name in names):
        raise FileExistsError("Demo credentials already exist; choose a new output directory.")
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    public = key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    files: dict[str, bytes] = {"private.pem": private, "public.pem": public}
    now = int(time.time())
    for user, brands in {"analyst-a": ["Allegra K"], "analyst-b": ["Levi's"], "ceo": []}.items():
        scopes = sorted(ACTION_SCOPES | ({ALL_BRANDS} if user == "ceo" else set()))
        claims = {
            "sub": user,
            "iss": issuer,
            "aud": audience,
            "iat": now,
            "nbf": now,
            "exp": now + 3600,
            "scopes": scopes,
            "brands": brands,
        }
        files[f"{user}.jwt"] = jwt.encode(claims, private, algorithm="RS256").encode()
    for name, content in files.items():
        fd = os.open(directory / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)


def refresh_demo_tokens(directory: Path, issuer: str, audience: str) -> None:
    """Renew demo tokens using the existing local issuer key; never print tokens."""
    private = (directory / "private.pem").read_bytes()
    key = serialization.load_pem_private_key(private, password=None)
    if not isinstance(key, rsa.RSAPrivateKey) or key.key_size < 2048:
        raise ValueError("Invalid demo issuer key")
    now = int(time.time())
    for user, brands in {"analyst-a": ["Allegra K"], "analyst-b": ["Levi's"], "ceo": []}.items():
        claims = {
            "sub": user,
            "iss": issuer,
            "aud": audience,
            "iat": now,
            "nbf": now,
            "exp": now + 3600,
            "scopes": sorted(ACTION_SCOPES | ({ALL_BRANDS} if user == "ceo" else set())),
            "brands": brands,
        }
        content = jwt.encode(claims, private, algorithm="RS256")
        temporary = directory / f".{user}.jwt.tmp"
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as stream:
            stream.write(content)
        os.replace(temporary, directory / f"{user}.jwt")

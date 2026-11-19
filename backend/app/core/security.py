"""Passwords, access tokens (JWT) and opaque tokens (docs/api.md, section 2.1).

Passwords
    Hashed with Argon2id (argon2-cffi defaults: RFC 9106 "low memory" profile,
    64 MiB, 3 passes, 4 lanes). Hashing takes about 0.1 s of CPU on purpose,
    which makes guessing passwords from a stolen database slow. Because it is
    slow, it runs in a worker thread so it doesn't freeze the event loop, and at
    most ``MAX_CONCURRENT_HASHES`` run at once (each one uses 64 MiB), so a burst
    of logins can't exhaust the server's memory.

Access tokens
    JWT, HS256, 15 minutes. Claims: ``sub`` (user id, as a string, as the JWT
    standard requires), ``org`` (organization id), ``type: "access"``, ``iat``,
    ``exp``. Any problem with a token (expired, tampered, wrong algorithm,
    missing claim) raises the same ``NotAuthenticatedError`` (401).

Refresh and password-reset tokens
    32 random bytes, URL-safe (43 characters). Only their SHA-256 hash is
    stored. SHA-256 rather than Argon2 is right here: the token is random and
    256 bits long, so it can't be guessed, and the hash must be the same every
    time so the database can find the row through its UNIQUE index.
"""

import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, NamedTuple

import jwt
from anyio import CapacityLimiter, to_thread
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

from app.core.config import Settings, get_settings
from app.core.exceptions import NotAuthenticatedError

# ── Passwords ─────────────────────────────────────────────────────────────────

MAX_CONCURRENT_HASHES = 4

_password_hasher = PasswordHasher()
_hash_limiter = CapacityLimiter(MAX_CONCURRENT_HASHES)
_dummy_hash: str | None = None


class PasswordCheck(NamedTuple):
    valid: bool
    # True when the stored hash uses older/weaker settings than the current
    # ones: the login service then saves a fresh hash of the same password.
    needs_rehash: bool


async def hash_password(password: str) -> str:
    """Argon2id hash with a fresh random salt (``$argon2id$v=19$m=65536,t=3,p=4$...``)."""
    return await to_thread.run_sync(_password_hasher.hash, password, limiter=_hash_limiter)


def _verify_sync(password: str, password_hash: str) -> PasswordCheck:
    try:
        _password_hasher.verify(password_hash, password)
    except (VerificationError, InvalidHashError):
        # Wrong password, or a stored value that isn't an Argon2 hash at all.
        return PasswordCheck(valid=False, needs_rehash=False)
    return PasswordCheck(
        valid=True, needs_rehash=_password_hasher.check_needs_rehash(password_hash)
    )


async def verify_password(password: str, password_hash: str) -> PasswordCheck:
    """Check a password against a stored hash. Never raises for a wrong password."""
    return await to_thread.run_sync(_verify_sync, password, password_hash, limiter=_hash_limiter)


async def verify_dummy_password(password: str) -> None:
    """Spend the same time as a real check when the email is unknown (AUTH-2).

    Without this, "unknown email" would answer in ~1 ms and "wrong password" in
    ~100 ms, so an attacker could tell which emails have accounts.
    """
    global _dummy_hash
    if _dummy_hash is None:
        _dummy_hash = await hash_password(secrets.token_urlsafe(16))
    await verify_password(password, _dummy_hash)


# ── Opaque tokens (refresh, password reset) ───────────────────────────────────


class OpaqueToken(NamedTuple):
    value: str  # given to the client once; never stored
    hash: str  # stored in token_hash; 64 hex characters


def hash_token(token: str) -> str:
    """SHA-256 of a token, as stored in ``refresh_tokens``/``password_reset_tokens``."""
    return hashlib.sha256(token.encode()).hexdigest()


def generate_token() -> OpaqueToken:
    value = secrets.token_urlsafe(32)
    return OpaqueToken(value=value, hash=hash_token(value))


# ── Access tokens (JWT) ───────────────────────────────────────────────────────

ACCESS_TOKEN_TYPE = "access"  # noqa: S105  (a claim value, not a password)
_REQUIRED_CLAIMS = ["sub", "org", "type", "iat", "exp"]


@dataclass(frozen=True, slots=True)
class AccessTokenClaims:
    user_id: int
    organization_id: int
    expires_at: datetime


def create_access_token(
    *,
    user_id: int,
    organization_id: int,
    now: datetime | None = None,
    settings: Settings | None = None,
) -> str:
    settings = settings or get_settings()
    issued_at = now or datetime.now(UTC)
    payload: dict[str, Any] = {
        "sub": str(user_id),
        "org": organization_id,
        "type": ACCESS_TOKEN_TYPE,
        "iat": issued_at,
        "exp": issued_at + settings.access_token_ttl,
    }
    return jwt.encode(
        payload,
        settings.jwt_secret_key.get_secret_value(),
        algorithm=settings.jwt_algorithm,
    )


def decode_access_token(token: str, *, settings: Settings | None = None) -> AccessTokenClaims:
    """Verify signature, expiry and claims; raise 401 ``not_authenticated`` otherwise.

    ``algorithms`` is a fixed list: a token claiming ``alg: none`` or another
    algorithm is rejected, so nobody can forge a token by choosing the algorithm.
    """
    settings = settings or get_settings()
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret_key.get_secret_value(),
            algorithms=[settings.jwt_algorithm],
            options={"require": _REQUIRED_CLAIMS},
        )
        if payload["type"] != ACCESS_TOKEN_TYPE:
            raise jwt.InvalidTokenError("not an access token")
        return AccessTokenClaims(
            user_id=int(payload["sub"]),
            organization_id=int(payload["org"]),
            expires_at=datetime.fromtimestamp(payload["exp"], UTC),
        )
    except (jwt.InvalidTokenError, ValueError, TypeError) as exc:
        # One message for every case: the client only needs to know "log in again".
        raise NotAuthenticatedError("Invalid or expired access token.") from exc

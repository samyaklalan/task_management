"""Passwords, access tokens and random tokens (docs/api.md, section 2.1).

1. Passwords: hashed with Argon2id. Hashing is slow on purpose (~0.1 s), so it
   runs in a worker thread and doesn't block other requests.
2. Access tokens: JWT signed with HS256, valid for 15 minutes.
3. Refresh and password-reset tokens: random strings. Only their SHA-256 hash
   is saved in the database.
"""

import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime

import jwt
from anyio import CapacityLimiter, to_thread
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

from app.core.config import get_settings
from app.core.exceptions import NotAuthenticatedError

# ── 1. Passwords ──────────────────────────────────────────────────────────────

# The Argon2id hasher: 64 MiB of memory and ~0.1 s per hash, on purpose, so
# guessing passwords from a stolen database is slow.
_hasher = PasswordHasher()

# At most 4 hashes run at the same time (4 x 64 MiB = ~256 MiB). If 100 users
# log in at once, 4 hashes run and the other 96 wait their turn asynchronously
# (the server keeps serving other requests); each finished hash lets the next
# one start. So a burst of logins can't use up the server's memory.
_limiter = CapacityLimiter(4)

# A fake hash, made once at import. Used only by verify_dummy_password(), so
# that an unknown email takes as long as a wrong password (AUTH-2).
_DUMMY_HASH = _hasher.hash("dummy-password-for-unknown-emails")


def _check(password: str, password_hash: str) -> bool:
    """The real comparison, as a normal (blocking) function.

    Argon2 can't be reversed: it re-hashes the typed password with the salt and
    settings stored inside ``password_hash`` and checks the result is exactly
    equal. Returns True on a match; a wrong password or a broken hash returns
    False instead of raising. Not called directly: verify_password() runs it in
    a worker thread.
    """
    try:
        return _hasher.verify(password_hash, password)
    except (VerificationError, InvalidHashError):
        return False


async def hash_password(password: str) -> str:
    """Use: sign-up (AUTH-1) and password reset (AUTH-6).

    Makes a NEW Argon2 hash with a fresh random salt, to save in
    ``users.password_hash``. Argon2 takes ~0.1 s of CPU, so ``to_thread`` runs
    it in a worker thread: the event loop isn't paused and other requests keep
    being served. ``_limiter`` allows at most 4 at once.
    """
    return await to_thread.run_sync(_hasher.hash, password, limiter=_limiter)


async def verify_password(password: str, password_hash: str) -> bool:
    """Use: login (AUTH-2) when the email exists.

    True if the typed password matches the stored hash, False otherwise. Never
    raises for a wrong or broken hash. Runs _check() in a worker thread, with
    the same limit of 4.
    """
    return await to_thread.run_sync(_check, password, password_hash, limiter=_limiter)


async def verify_dummy_password(password: str) -> None:
    """Use: login (AUTH-2) when the email does NOT exist.

    Does the same Argon2 work as a real check (against _DUMMY_HASH), so
    "unknown email" takes as long as "wrong password" and an attacker can't use
    response times to find out which emails have accounts. The result is
    ignored: the login fails anyway.
    """
    await verify_password(password, _DUMMY_HASH)


def password_needs_rehash(password_hash: str) -> bool:
    """Use: right after a SUCCESSFUL login.

    True if the stored hash was made with older Argon2 settings; the login
    service then calls hash_password() and saves the new hash. It only reads
    the settings written inside the hash, so it's fast and needs no thread.
    """
    return _hasher.check_needs_rehash(password_hash)


# ── 2. Access tokens (JWT) ────────────────────────────────────────────────────


@dataclass(frozen=True)
class TokenUser:
    """Who an access token belongs to: what decode_access_token() returns.

    ``frozen=True``: it can't be changed after it's created. deps.py will use
    it as the "current user" of every protected endpoint (M3).
    """

    user_id: int
    organization_id: int


def create_access_token(user_id: int, organization_id: int) -> str:
    """Use: login (AUTH-2) and token refresh (AUTH-3).

    Builds a JWT signed with HS256, valid for 15 minutes. Claims: ``sub`` (user
    id), ``org`` (organization id), ``type: "access"``, ``iat`` (issued at),
    ``exp`` (expires at). The client sends it back as
    ``Authorization: Bearer <token>``.
    """
    settings = get_settings()
    now = datetime.now(UTC)
    payload = {
        "sub": str(user_id),  # the JWT standard requires a string here
        "org": organization_id,
        "type": "access",
        "iat": now,
        "exp": now + settings.access_token_ttl,
    }
    secret = settings.jwt_secret_key.get_secret_value()
    return jwt.encode(payload, secret, algorithm=settings.jwt_algorithm)  # always HS256


def decode_access_token(token: str) -> TokenUser:
    """Use: every protected request (through deps.py, M3).

    Checks the signature, the expiry, the algorithm (HS256 only, never "none"),
    that every claim is present and that ``type`` is "access". Valid: returns
    the TokenUser. ANY problem: 401 not_authenticated, with one message for
    every case (the client only needs to know "log in again").
    """
    settings = get_settings()
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret_key.get_secret_value(),
            # Only HS256 (config allows nothing else): a token can't pick "none".
            algorithms=[settings.jwt_algorithm],
            options={"require": ["sub", "org", "type", "iat", "exp"]},
        )
        if payload["type"] != "access":
            raise ValueError("not an access token")
        return TokenUser(user_id=int(payload["sub"]), organization_id=int(payload["org"]))
    except (jwt.InvalidTokenError, ValueError, TypeError) as exc:
        raise NotAuthenticatedError("Invalid or expired access token.") from exc


# ── 3. Refresh and password-reset tokens ──────────────────────────────────────


def hash_token(token: str) -> str:
    """Use: whenever a refresh or reset token is saved or looked up.

    SHA-256 as 64 hex characters; only this hash is stored, never the token.
    SHA-256 (not Argon2) is right here: the token is random and 256 bits long,
    so it can't be guessed, and the hash must be the same every time so the
    UNIQUE index on ``token_hash`` can find the row.
    """
    return hashlib.sha256(token.encode()).hexdigest()


def generate_token() -> tuple[str, str]:
    """Use: login/refresh (a new refresh token) and forgot-password (AUTH-5).

    Returns ``(token, token_hash)``: the token is sent to the user once (in the
    response, or in the email link); the hash is saved in ``refresh_tokens`` or
    ``password_reset_tokens``. 32 random bytes, URL-safe: 43 characters.
    """
    token = secrets.token_urlsafe(32)  # 43 characters
    return token, hash_token(token)

"""Passwords, access tokens and random tokens (app/core/security.py)."""

import time
from datetime import UTC, datetime, timedelta

import anyio
import jwt
import pytest
from argon2 import PasswordHasher

from app.core.config import get_settings
from app.core.exceptions import NotAuthenticatedError
from app.core.security import (
    TokenUser,
    create_access_token,
    decode_access_token,
    generate_token,
    hash_password,
    hash_token,
    password_needs_rehash,
    verify_dummy_password,
    verify_password,
)

SECRET = get_settings().jwt_secret_key.get_secret_value()

# ── 1. Passwords ──────────────────────────────────────────────────────────────


async def test_password_round_trip() -> None:
    password_hash = await hash_password("correct-horse-battery")

    assert password_hash.startswith("$argon2id$v=19$m=65536,t=3,p=4$")
    assert "correct-horse-battery" not in password_hash
    assert await verify_password("correct-horse-battery", password_hash) is True
    assert await verify_password("correct-horse-batterY", password_hash) is False


async def test_same_password_gets_a_different_hash_each_time() -> None:
    # Random salt: equal passwords can't be spotted in the database.
    assert await hash_password("same-password") != await hash_password("same-password")


async def test_non_ascii_password() -> None:
    assert await verify_password("pässwörd-密码-🔑", await hash_password("pässwörd-密码-🔑"))


@pytest.mark.parametrize("stored", ["", "not-a-hash", "$argon2id$v=19$m=65536,t=3,p=4$broken"])
async def test_broken_stored_hash_is_a_failed_check_not_a_crash(stored: str) -> None:
    assert await verify_password("whatever", stored) is False


async def test_only_old_hashes_need_rehash() -> None:
    old_hash = PasswordHasher(time_cost=1, memory_cost=8192, parallelism=1).hash("pw")

    assert password_needs_rehash(old_hash) is True
    assert password_needs_rehash(await hash_password("pw")) is False


async def test_dummy_check_takes_about_as_long_as_a_real_one() -> None:
    real_hash = await hash_password("real-password")

    started = time.perf_counter()
    await verify_password("wrong-password", real_hash)
    real = time.perf_counter() - started
    started = time.perf_counter()
    await verify_dummy_password("wrong-password")
    dummy = time.perf_counter() - started

    # Same Argon2 work, so the same order of magnitude (generous bound for noise).
    assert dummy > real / 5, (real, dummy)


async def test_hashing_does_not_block_other_requests() -> None:
    """While a hash runs in its worker thread, the event loop keeps working."""
    ticks = 0

    async def ticker() -> None:
        nonlocal ticks
        while True:
            await anyio.sleep(0.005)
            ticks += 1

    async with anyio.create_task_group() as group:
        group.start_soon(ticker)
        await hash_password("some-password")  # ~0.1 s
        group.cancel_scope.cancel()

    assert ticks >= 5, ticks  # would be 0 if hashing blocked the event loop


# ── 2. Access tokens ──────────────────────────────────────────────────────────


def test_access_token_round_trip() -> None:
    token = create_access_token(user_id=42, organization_id=7)

    assert decode_access_token(token) == TokenUser(user_id=42, organization_id=7)


def test_access_token_claims() -> None:
    payload = jwt.decode(create_access_token(42, 7), options={"verify_signature": False})

    assert jwt.get_unverified_header(create_access_token(42, 7))["alg"] == "HS256"
    assert set(payload) == {"sub", "org", "type", "iat", "exp"}
    assert (payload["sub"], payload["org"], payload["type"]) == ("42", 7, "access")
    assert payload["exp"] - payload["iat"] == 15 * 60


def _payload(**changes: object) -> dict:
    """A valid payload; pass ``claim=None`` to remove a claim."""
    now = datetime.now(UTC)
    payload = {
        "sub": "42",
        "org": 7,
        "type": "access",
        "iat": now,
        "exp": now + timedelta(minutes=15),
    }
    payload.update(changes)
    return {key: value for key, value in payload.items() if value is not None}


def _sign(payload: dict, key: str = SECRET, algorithm: str = "HS256") -> str:
    return jwt.encode(payload, key, algorithm=algorithm)


@pytest.mark.parametrize(
    "token",
    [
        pytest.param(_sign(_payload(exp=datetime.now(UTC) - timedelta(seconds=1))), id="expired"),
        pytest.param(_sign(_payload(), key="x" * 32), id="wrong-secret"),
        pytest.param(_sign(_payload(), algorithm="HS512"), id="other-algorithm"),
        pytest.param(jwt.encode(_payload(), key=None, algorithm="none"), id="unsigned"),
        pytest.param(_sign(_payload(type="refresh")), id="wrong-type"),
        pytest.param(_sign(_payload(org=None)), id="missing-org"),
        pytest.param(_sign(_payload(exp=None)), id="missing-exp"),
        pytest.param(_sign(_payload(sub="admin")), id="non-numeric-sub"),
        pytest.param("not.a.jwt", id="garbage"),
        pytest.param("", id="empty"),
    ],
)
def test_bad_access_tokens_are_rejected(token: str) -> None:
    with pytest.raises(NotAuthenticatedError) as caught:
        decode_access_token(token)

    assert caught.value.status_code == 401
    assert caught.value.code == "not_authenticated"


def test_changed_payload_is_rejected() -> None:
    header, _payload_part, signature = create_access_token(42, 7).split(".")
    forged = _sign(_payload(sub="1")).split(".")[1]  # try to become user 1

    with pytest.raises(NotAuthenticatedError):
        decode_access_token(f"{header}.{forged}.{signature}")


# ── 3. Refresh and password-reset tokens ──────────────────────────────────────


def test_generated_tokens_are_random_and_url_safe() -> None:
    tokens = [generate_token() for _ in range(100)]

    assert len({token for token, _hash in tokens}) == 100
    for token, token_hash in tokens:
        assert len(token) == 43
        assert set(token) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")
        assert token_hash == hash_token(token)


def test_hash_token_is_sha256_hex() -> None:
    # A known SHA-256 value, so the saved format can't change by accident.
    assert hash_token("abc") == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"

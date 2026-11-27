"""Request validation and response shapes for auth and users
(B24: app/schemas/auth.py, app/schemas/user.py)."""

import re
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.organization import ORGANIZATION_SLUG_PATTERN
from app.schemas import auth, user
from app.schemas.auth import (
    LoginRequest,
    LoginResponse,
    PasswordResetConfirm,
    RefreshRequest,
    RegisterRequest,
    slugify,
)
from app.schemas.common import RoleRef
from app.schemas.user import (
    CreateUserRequest,
    MeResponse,
    OrgPermissions,
    UpdateMeRequest,
    UpdateUserRequest,
    UserOut,
    UserSummary,
)
from tests.factories import make_user

VALID_REGISTER = {
    "organization_name": "Acme Corp",
    "name": "Asha Rao",
    "email": "asha@acme.com",
    "password": "correct-horse-battery",
}


def _error_types(model: type[BaseModel], data: dict[str, Any]) -> list[tuple[str, str]]:
    """``[(field, error type), ...]`` for invalid input."""
    with pytest.raises(ValidationError) as caught:
        model.model_validate(data)
    return [(".".join(map(str, e["loc"])), e["type"]) for e in caught.value.errors()]


# ── AUTH-1 register ───────────────────────────────────────────────────────────


def test_register_cleans_up_the_input() -> None:
    request = RegisterRequest.model_validate(
        VALID_REGISTER
        | {"organization_name": "  Acme Corp ", "name": " Asha Rao ", "email": "Asha@ACME.com"}
    )

    assert request.organization_name == "Acme Corp"
    assert request.name == "Asha Rao"
    assert request.email == "asha@acme.com"  # lowercased (ck_users_email_lowercase)
    assert request.organization_slug == "acme-corp"  # made from the name


def test_password_is_kept_exactly_as_typed() -> None:
    request = RegisterRequest.model_validate(VALID_REGISTER | {"password": "  spaces  "})
    assert request.password == "  spaces  "


def test_an_explicit_slug_is_kept() -> None:
    request = RegisterRequest.model_validate(VALID_REGISTER | {"organization_slug": "acme"})
    assert request.organization_slug == "acme"


@pytest.mark.parametrize("slug", ["Acme", "acme corp", "acme--corp", "-acme", "a" * 61, ""])
def test_invalid_slug_is_rejected(slug: str) -> None:
    errors = _error_types(RegisterRequest, VALID_REGISTER | {"organization_slug": slug})
    assert [field for field, _type in errors] == ["organization_slug"]


@pytest.mark.parametrize(
    ("name", "slug"),
    [
        ("Acme Corp", "acme-corp"),
        ("Café Déjà Vu!", "cafe-deja-vu"),
        ("  R&D -- Team 7 ", "r-d-team-7"),
        ("x" * 70, "x" * 60),
        ("a" * 59 + " b", "a" * 59),  # cut at 60, then the trailing hyphen removed
        ("株式会社", ""),
        ("!!!", ""),
    ],
)
def test_slugify(name: str, slug: str) -> None:
    assert slugify(name) == slug


@pytest.mark.parametrize("name", ["株式会社", "!!!"])
def test_name_without_latin_letters_gets_a_random_valid_slug(name: str) -> None:
    request = RegisterRequest.model_validate(VALID_REGISTER | {"organization_name": name})

    assert re.fullmatch(r"org-[0-9a-f]{8}", request.organization_slug)
    assert re.fullmatch(ORGANIZATION_SLUG_PATTERN, request.organization_slug)  # DB CHECK


@pytest.mark.parametrize(
    ("password", "ok"),
    [("x" * 7, False), ("x" * 8, True), ("x" * 128, True), ("x" * 129, False), ("é" * 8, True)],
)
def test_password_length_rule_a12(password: str, ok: bool) -> None:
    data = VALID_REGISTER | {"password": password}
    if ok:
        RegisterRequest.model_validate(data)
    else:
        assert [f for f, _ in _error_types(RegisterRequest, data)] == ["password"]


@pytest.mark.parametrize(
    ("changes", "field"),
    [
        ({"email": "not-an-email"}, "email"),
        ({"email": "asha@"}, "email"),
        ({"email": "a" * 250 + "@acme.com"}, "email"),
        ({"name": "   "}, "name"),
        ({"name": "x" * 151}, "name"),
        ({"organization_name": ""}, "organization_name"),
        ({"is_admin": True}, "is_admin"),  # unknown fields are refused, not ignored
    ],
)
def test_invalid_register_input(changes: dict[str, Any], field: str) -> None:
    assert [f for f, _ in _error_types(RegisterRequest, VALID_REGISTER | changes)] == [field]


def test_missing_register_fields() -> None:
    errors = _error_types(RegisterRequest, {})
    assert sorted(field for field, error in errors if error == "missing") == [
        "email",
        "name",
        "organization_name",
        "password",
    ]


# ── AUTH-2 to AUTH-6 ──────────────────────────────────────────────────────────


def test_login_accepts_any_stored_password_length_up_to_128() -> None:
    # An old, shorter password must still be able to log in (A-12 applies to new ones).
    assert LoginRequest(email="Asha@Acme.com", password="short").email == "asha@acme.com"
    assert [f for f, _ in _error_types(LoginRequest, {"email": "a@b.co", "password": ""})] == [
        "password"
    ]
    assert [
        f for f, _ in _error_types(LoginRequest, {"email": "a@b.co", "password": "x" * 129})
    ] == ["password"]


@pytest.mark.parametrize("value", ["", "x" * 129])
def test_token_fields_have_sane_limits(value: str) -> None:
    assert _error_types(RefreshRequest, {"refresh_token": value})[0][0] == "refresh_token"
    errors = _error_types(PasswordResetConfirm, {"token": value, "new_password": "long-enough"})
    assert errors[0][0] == "token"


def test_reset_confirm_applies_the_password_rule() -> None:
    errors = _error_types(PasswordResetConfirm, {"token": "t" * 43, "new_password": "short"})
    assert errors == [("new_password", "string_too_short")]


def test_login_response_matches_api_md() -> None:
    response = LoginResponse(
        access_token="eyJ...",
        expires_in=900,
        refresh_token="q8D3tY...",
        refresh_expires_in=1209600,
        user=UserSummary(id=1, name="Asha Rao", email="asha@acme.com"),
    )

    assert response.model_dump() == {
        "access_token": "eyJ...",
        "token_type": "bearer",
        "expires_in": 900,
        "refresh_token": "q8D3tY...",
        "refresh_expires_in": 1209600,
        "user": {"id": 1, "name": "Asha Rao", "email": "asha@acme.com"},
    }


# ── Users ─────────────────────────────────────────────────────────────────────


def test_patch_me_leaving_name_out_changes_nothing() -> None:
    assert UpdateMeRequest().model_fields_set == set()
    assert UpdateMeRequest(name=" Asha R. ").name == "Asha R."


@pytest.mark.parametrize(
    ("model", "data", "field"),
    [
        (UpdateMeRequest, {"name": None}, "name"),
        (UpdateMeRequest, {"name": ""}, "name"),
        (UpdateUserRequest, {"name": None, "expected_updated_at": "2026-11-16T08:00:00Z"}, "name"),
        (
            UpdateUserRequest,
            {"is_active": None, "expected_updated_at": "2026-11-16T08:00:00Z"},
            "is_active",
        ),
    ],
)
def test_patch_fields_cannot_be_set_to_null(
    model: type[BaseModel], data: dict[str, Any], field: str
) -> None:
    assert [f for f, _ in _error_types(model, data)] == [field]


def test_update_user_needs_a_timezone_aware_expected_updated_at() -> None:
    assert _error_types(UpdateUserRequest, {"is_active": False}) == [
        ("expected_updated_at", "missing")
    ]
    assert _error_types(
        UpdateUserRequest, {"is_active": False, "expected_updated_at": "2026-11-16T08:00:00"}
    ) == [("expected_updated_at", "timezone_aware")]


def test_create_user_lowercases_the_email() -> None:
    assert CreateUserRequest(name="Ravi", email="Ravi@Acme.com").email == "ravi@acme.com"


def test_me_without_org_role_has_all_org_permissions_false() -> None:
    me = MeResponse(
        id=2,
        name="Ravi",
        email="ravi@acme.com",
        organization={"name": "Acme Corp", "slug": "acme"},
        org_role=None,
        org_permissions=OrgPermissions(),
    )

    dumped = me.model_dump()
    assert dumped["org_role"] is None
    assert set(dumped["org_permissions"]) == {"organization", "users", "roles", "projects"}
    assert all(not flag for flags in dumped["org_permissions"].values() for flag in flags.values())


async def test_user_out_from_a_database_row(db_session: AsyncSession) -> None:
    """Built straight from the ORM object, without touching the deferred
    password_hash (that would raise), and with timestamps in UTC ending in Z."""
    row = await make_user(db_session, name="Ravi Kumar", email="ravi@acme.com")

    out = UserOut.model_validate(
        {
            "id": row.id,
            "name": row.name,
            "email": row.email,
            "is_active": row.is_active,
            "org_role": RoleRef(id=3, name="Manager"),
            "created_at": row.created_at,
            "updated_at": row.updated_at,
        }
    )
    summary = UserSummary.model_validate(row)  # from_attributes

    body = out.model_dump(mode="json")
    assert body["created_at"].endswith("Z")
    assert body["org_role"] == {"id": 3, "name": "Manager"}
    assert summary.model_dump() == {"id": row.id, "name": "Ravi Kumar", "email": "ravi@acme.com"}


def test_no_schema_can_expose_a_hidden_field() -> None:
    """api.md section 1: password_hash and token_hash are never returned anywhere."""
    hidden = {"password_hash", "token_hash", "next_task_number"}
    models = [
        value
        for module in (auth, user)
        for value in vars(module).values()
        if isinstance(value, type) and issubclass(value, BaseModel)
    ]

    assert len(models) >= 15
    for model in models:
        assert not hidden & set(model.model_fields), model.__name__


def test_timestamps_serialize_with_z() -> None:
    moment = datetime(2026, 11, 16, 8, 0, tzinfo=UTC)
    out = UserOut(
        id=1,
        name="a",
        email="a@b.co",
        is_active=True,
        org_role=None,
        created_at=moment,
        updated_at=moment,
    )
    assert out.model_dump(mode="json")["created_at"] == "2026-11-16T08:00:00Z"

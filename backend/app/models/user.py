"""USERS: people who log in. Each user belongs to exactly one organization (P-8).

Schema: docs/database.md, section 3.2.
Timestamps: created_at, updated_at come from TimestampMixin (app/db/base.py).
"""

from typing import TYPE_CHECKING

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    String,
    UniqueConstraint,
    true,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, BigIntPK, TimestampMixin

if TYPE_CHECKING:
    from app.models.organization import Organization

USER_NAME_MAX_LENGTH = 150
USER_EMAIL_MAX_LENGTH = 254  # the longest address the email standard (RFC 5321) allows
PASSWORD_HASH_MAX_LENGTH = 255


class User(TimestampMixin, Base):
    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("email"),  # uq_users_email (PDF #9): one email, one account
        # Emails are stored lowercase, so "Asha@x.com" and "asha@x.com" can't be two users.
        CheckConstraint("email = lower(email)", name="email_lowercase"),  # P-8
    )

    id: Mapped[BigIntPK]
    organization_id: Mapped[int] = mapped_column(
        BigInteger,  # explicit: don't rely on borrowing the type from organizations.id
        ForeignKey("organizations.id", ondelete="NO ACTION"),
        index=True,  # ix_users_organization_id (PDF #10): the organization's user list
    )
    name: Mapped[str] = mapped_column(String(USER_NAME_MAX_LENGTH))
    email: Mapped[str] = mapped_column(String(USER_EMAIL_MAX_LENGTH))
    # defer hide the co;lumn so select user is run only fetch all field except password
    # defref_raiseload as true
    # raiseinload measn it will give youeror right awaya nd tell
    # toy the cloumn not loaded you nee to ask for it
    # Deferred: never loaded by a normal ``select(User)``. Reading it without asking
    # for it explicitly raises an error instead of quietly running another query.
    # Only login and password changes load it, with ``undefer(User.password_hash)``.
    password_hash: Mapped[str] = mapped_column(
        String(PASSWORD_HASH_MAX_LENGTH),
        deferred=True,
        deferred_raiseload=True,
    )
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=true())

    # Many-to-one: the user's organization. lazy="raise": it must be loaded in the
    # query (joinedload / selectinload) when needed; it is never fetched implicitly.
    organization: Mapped["Organization"] = relationship(lazy="raise")

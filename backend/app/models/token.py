"""REFRESH_TOKENS and PASSWORD_RESET_TOKENS: hashed, single-purpose tokens.

Only the SHA-256 hash of a token is stored (P-10). The raw token exists only in
the user's cookie (refresh) or in the emailed link (reset). Anyone who reads the
database can't use what they find to log in.

Both tokens are looked up by ``token_hash`` and then locked with
``SELECT ... FOR UPDATE`` inside the transaction that uses them, so two
simultaneous requests can never both use the same token (refresh rotation,
docs/api.md section 2.2; AUTH-6).

Schema: docs/database.md, sections 3.12 and 3.13.
Timestamps: created_at only (CreatedAtMixin, app/db/base.py).
"""

from datetime import datetime

from sqlalchemy import BigInteger, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, BigIntPK, CreatedAtMixin

# SHA-256 as 64 hexadecimal characters (hashing lives in app/core/security.py).
TOKEN_HASH_LENGTH = 64


class RefreshToken(CreatedAtMixin, Base):
    """One row per logged-in session (device or browser).

    Valid only while ``revoked_at IS NULL AND expires_at > now()``.
    """

    __tablename__ = "refresh_tokens"
    __table_args__ = (
        # uq_refresh_tokens_token_hash (P-10): every refresh looks the token up by
        # its hash, so this unique index is also the lookup index.
        UniqueConstraint("token_hash"),
    )

    id: Mapped[BigIntPK]
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True,  # ix_refresh_tokens_user_id (PDF #10): revoke all of a user's sessions
    )
    token_hash: Mapped[str] = mapped_column(String(TOKEN_HASH_LENGTH))
    expires_at: Mapped[datetime]
    revoked_at: Mapped[datetime | None]  # set on logout or rotation
    last_used_at: Mapped[datetime | None]


class PasswordResetToken(CreatedAtMixin, Base):
    """One row per reset (or set-password) link.

    Valid only while ``used_at IS NULL AND expires_at > now()``; single use.
    """

    __tablename__ = "password_reset_tokens"
    __table_args__ = (
        UniqueConstraint("token_hash"),  # uq_password_reset_tokens_token_hash (P-10)
    )

    id: Mapped[BigIntPK]
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True,  # ix_password_reset_tokens_user_id (PDF #10): invalidate older tokens
    )
    token_hash: Mapped[str] = mapped_column(String(TOKEN_HASH_LENGTH))
    expires_at: Mapped[datetime]
    used_at: Mapped[datetime | None]  # set once; a used token can't be used again

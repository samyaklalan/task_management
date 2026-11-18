"""ORGANIZATIONS: the tenant. Every user, project and role belongs to one.

Schema: docs/database.md, section 3.1.
"""

from sqlalchemy import CheckConstraint, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, BigIntPK, TimestampMixin

# Limits shared with the Pydantic schemas (app/schemas/organization.py), so the
# API and the database always agree on them.
ORGANIZATION_NAME_MAX_LENGTH = 150
ORGANIZATION_SLUG_MAX_LENGTH = 60
# Lowercase letters and digits in groups separated by single hyphens: "acme", "acme-corp".
ORGANIZATION_SLUG_PATTERN = r"^[a-z0-9]+(-[a-z0-9]+)*$"


class Organization(TimestampMixin, Base):
    __tablename__ = "organizations"
    __table_args__ = (
        UniqueConstraint("slug"),  # uq_organizations_slug (PDF #9)
        CheckConstraint(
            f"slug ~ '{ORGANIZATION_SLUG_PATTERN}'",
            name="slug_format",  # ck_organizations_slug_format (P-5)
        ),
    )

    id: Mapped[BigIntPK]
    name: Mapped[str] = mapped_column(String(ORGANIZATION_NAME_MAX_LENGTH))
    slug: Mapped[str] = mapped_column(String(ORGANIZATION_SLUG_MAX_LENGTH))
    description: Mapped[str | None] = mapped_column(Text)

    # No relationship collections here (organization.users, .projects, .roles).
    # Those lists can hold thousands of rows; they are always fetched through
    # repositories with filters and pagination, never loaded as a whole.

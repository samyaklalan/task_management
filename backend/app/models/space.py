"""SPACES: named work areas inside a project ("Frontend", "QA", "Ravi's tasks").

Every task sits in exactly one space of its own project. Spaces group tasks;
teams group people; the two are independent.

Schema: docs/database.md, section 3.14 (decisions S-1 to S-6, mentor review 17 Nov 2026).
Timestamps: created_at, updated_at come from TimestampMixin (app/db/base.py).
"""

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, BigIntPK, TimestampMixin

SPACE_NAME_MAX_LENGTH = 100
# Created with every project, at position 0 (S-2). It can be renamed like any space.
DEFAULT_SPACE_NAME = "General"


class Space(TimestampMixin, Base):
    __tablename__ = "spaces"
    __table_args__ = (
        # uq_spaces_project_id_name (S-6): no two spaces with the same name in a
        # project. Its leftmost column is project_id, so it also serves "spaces of
        # this project"; no separate project_id or position index (P-7).
        UniqueConstraint("project_id", "name"),
        CheckConstraint("position >= 0", name="position_non_negative"),
    )

    id: Mapped[BigIntPK]
    project_id: Mapped[int] = mapped_column(
        BigInteger,
        # CASCADE only matters if a project is ever hard-deleted; the API only
        # archives projects (P-4).
        ForeignKey("projects.id", ondelete="CASCADE"),
    )
    name: Mapped[str] = mapped_column(String(SPACE_NAME_MAX_LENGTH))
    description: Mapped[str | None] = mapped_column(Text)
    # Sidebar order (0, 1, 2, ...). Not unique: reordering would otherwise hit
    # temporary duplicates. Lists sort by (position, id).
    position: Mapped[int] = mapped_column(Integer)

    # Spaces are never deleted through the API (S-5): there is deliberately no
    # delete service or endpoint. Rename, describe and reorder only.

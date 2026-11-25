"""TASK_COMMENTS: the discussion thread on a task.

``author_id`` is the only "who" column: the old duplicate ``created_by`` and
``edited_by`` fields were removed (PDF #5). Whether a comment was edited is
derived from its timestamps, not stored.

Schema: docs/database.md, section 3.8.
Timestamps: created_at, updated_at come from TimestampMixin (app/db/base.py).
"""

from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, ForeignKey, Index, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, BigIntPK, TimestampMixin

if TYPE_CHECKING:
    from app.models.user import User

# Enforced by the API schema (docs/api.md, CMT-2); the column itself is TEXT.
COMMENT_BODY_MAX_LENGTH = 10_000


class TaskComment(TimestampMixin, Base):
    __tablename__ = "task_comments"
    __table_args__ = (
        # ix_task_comments_task_id_created_at (PDF #10, P-7): finds one task's
        # comments (the task_id foreign key lookup) in time order, for the
        # paginated comment list (CMT-1). Covers the task_id index from PDF #10.
        Index(None, "task_id", "created_at"),
    )

    id: Mapped[BigIntPK]
    task_id: Mapped[int] = mapped_column(
        BigInteger,
        # A comment has no meaning without its task (P-4).
        ForeignKey("tasks.id", ondelete="CASCADE"),
    )
    author_id: Mapped[int] = mapped_column(
        BigInteger,
        # Authorship history must stay intact (P-4). Not indexed: no query filters
        # by author and users are never deleted (P-7). Set from the logged-in user,
        # never from the request body (CMT-2).
        ForeignKey("users.id", ondelete="NO ACTION"),
    )
    body: Mapped[str] = mapped_column(Text)

    # Many-to-one, loaded with joinedload to show author names in CMT-1.
    author: Mapped["User"] = relationship(lazy="raise")

    @property
    def is_edited(self) -> bool:
        """True after the body was changed (PDF #5: replaces the old edited_by column).

        Both timestamps start equal (each defaults to now() on insert); an edit
        moves updated_at forward (TimestampMixin).
        """
        return self.updated_at > self.created_at

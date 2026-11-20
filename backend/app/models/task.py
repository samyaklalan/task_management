"""TASKS: work items. Numbered per project (WEB-1, WEB-2, ...), each in one space
and one status of its own project.

Schema: docs/database.md, section 3.7 (+ space_id from S-1).
Timestamps: created_at, updated_at come from TimestampMixin (app/db/base.py).
``updated_at`` is also the version checked by optimistic concurrency (P-11).
"""

from datetime import date
from enum import StrEnum
from typing import TYPE_CHECKING

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, BigIntPK, TimestampMixin

if TYPE_CHECKING:
    from app.models.space import Space
    from app.models.task_status import TaskStatus
    from app.models.user import User

TASK_TITLE_MAX_LENGTH = 255
TASK_PRIORITY_MAX_LENGTH = 20


class TaskPriority(StrEnum):
    """Allowed values of tasks.priority (P-5), lowest to highest."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    URGENT = "urgent"


# Severity rank, so lists sort urgent > high > medium > low instead of
# alphabetically (docs/api.md, TSK-1).
PRIORITY_RANK: dict[str, int] = {priority.value: rank for rank, priority in enumerate(TaskPriority)}

_PRIORITY_VALUES = ", ".join(f"'{priority.value}'" for priority in TaskPriority)


class Task(TimestampMixin, Base):
    __tablename__ = "tasks"
    __table_args__ = (
        # uq_tasks_project_id_number (PDF #4, #9): a number exists once per project,
        # the final guard behind the atomic next_task_number update. project_id is
        # its leftmost column, so it also serves every "tasks of this project"
        # query; no separate project_id index (P-7).
        UniqueConstraint("project_id", "number"),
        # ix_tasks_title_trgm (P-9): case-insensitive "contains" search
        # (title ILIKE '%login%') across the whole project. A normal B-tree index
        # can't serve a leading wildcard; a trigram GIN index can. Needs the
        # pg_trgm extension, which the first migration enables (B18).
        Index(
            "ix_tasks_title_trgm",
            "title",
            postgresql_using="gin",
            postgresql_ops={"title": "gin_trgm_ops"},
        ),
        CheckConstraint(f"priority IN ({_PRIORITY_VALUES})", name="priority"),
        CheckConstraint("number >= 1", name="number_positive"),
        CheckConstraint(
            "start_date IS NULL OR end_date IS NULL OR end_date >= start_date",
            name="date_range",
        ),
    )

    id: Mapped[BigIntPK]  # internal only; URLs use (project public_id, number)
    project_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("projects.id", ondelete="CASCADE"),
    )
    # The space and status must belong to the same project as the task. A plain
    # foreign key can't express that; the task service checks it inside the
    # transaction (P-12).
    space_id: Mapped[int] = mapped_column(
        BigInteger,
        # Spaces are never deleted (S-5). NO ACTION is checked at the end of the
        # statement, so a project's tasks and spaces can still be removed together.
        ForeignKey("spaces.id", ondelete="NO ACTION"),
        index=True,  # ix_tasks_space_id (S-1): tasks of a space
    )
    status_id: Mapped[int] = mapped_column(
        BigInteger,
        # A status still used by tasks can't be deleted (P-4).
        ForeignKey("task_statuses.id", ondelete="NO ACTION"),
        index=True,  # ix_tasks_status_id (PDF #10): the status-in-use check
    )
    assignee_id: Mapped[int | None] = mapped_column(
        BigInteger,
        # If the user is ever deleted, the task becomes unassigned (P-4).
        ForeignKey("users.id", ondelete="SET NULL"),
        index=True,  # ix_tasks_assignee_id (PDF #10): "my tasks"
    )
    created_by: Mapped[int] = mapped_column(
        BigInteger,
        # Authorship history must stay intact (P-4). Not indexed: no query filters
        # by creator and users are never deleted (P-7).
        ForeignKey("users.id", ondelete="NO ACTION"),
    )
    number: Mapped[int] = mapped_column(Integer)  # shown as <task_prefix>-<number>
    title: Mapped[str] = mapped_column(String(TASK_TITLE_MAX_LENGTH))
    description: Mapped[str | None] = mapped_column(Text)
    priority: Mapped[str] = mapped_column(
        String(TASK_PRIORITY_MAX_LENGTH),
        server_default=TaskPriority.MEDIUM.value,
    )
    start_date: Mapped[date | None]
    end_date: Mapped[date | None]

    # Many-to-one relationships, all lazy="raise": the task list loads them in the
    # same query (joinedload), so 100 tasks never cost 100 extra queries (N+1).
    # Forgetting to load one raises an error instead of running hidden queries.
    space: Mapped["Space"] = relationship(lazy="raise")
    status: Mapped["TaskStatus"] = relationship(lazy="raise")
    # Two foreign keys point at users, so each relationship names its column.
    assignee: Mapped["User | None"] = relationship(foreign_keys=[assignee_id], lazy="raise")
    creator: Mapped["User"] = relationship(foreign_keys=[created_by], lazy="raise")

"""TASK_STATUSES: a project's workflow columns (e.g. To Do, In Review, Done).

Each project has its own statuses (S-4: all spaces in a project share them).
A status can be renamed or recoloured freely; ``category`` says what it *means*
(todo / in_progress / done), whatever its name, so counts and reports work
across projects with different workflows.

Schema: docs/database.md, section 3.6.
Timestamps: created_at, updated_at come from TimestampMixin (app/db/base.py).
"""

from enum import StrEnum

from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, BigIntPK, TimestampMixin

TASK_STATUS_NAME_MAX_LENGTH = 50
TASK_STATUS_CATEGORY_MAX_LENGTH = 20
COLOR_MAX_LENGTH = 7
# "#" followed by six hex digits, e.g. "#3B82F6".
COLOR_PATTERN = r"^#[0-9A-Fa-f]{6}$"


class TaskStatusCategory(StrEnum):
    """Allowed values of task_statuses.category (P-5). Shared with the API schemas."""

    TODO = "todo"
    IN_PROGRESS = "in_progress"
    DONE = "done"


_CATEGORY_VALUES = ", ".join(f"'{category.value}'" for category in TaskStatusCategory)

# Created with every new project, in this order (docs/api.md PRJ-1; behaviour
# proposal A-1, awaiting mentor approval). Used by the project service (B37).
DEFAULT_TASK_STATUSES: tuple[tuple[str, str, TaskStatusCategory], ...] = (
    ("To Do", "#64748B", TaskStatusCategory.TODO),
    ("In Progress", "#3B82F6", TaskStatusCategory.IN_PROGRESS),
    ("Done", "#22C55E", TaskStatusCategory.DONE),
)


class TaskStatus(TimestampMixin, Base):
    __tablename__ = "task_statuses"
    __table_args__ = (
        # ix_task_statuses_project_id_position (PDF #10, P-7): finds a project's
        # statuses quickly (the project_id foreign key lookup). Measured with
        # EXPLAIN on 10,000 statuses: PostgreSQL uses this index to find the
        # handful of rows, then sorts them by position in memory, which costs
        # nothing at that size.
        Index(None, "project_id", "position"),
        CheckConstraint(f"category IN ({_CATEGORY_VALUES})", name="category"),
        CheckConstraint(f"color ~ '{COLOR_PATTERN}'", name="color_format"),
        CheckConstraint("position >= 0", name="position_non_negative"),
    )

    id: Mapped[BigIntPK]
    project_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("projects.id", ondelete="CASCADE"),
    )
    name: Mapped[str] = mapped_column(String(TASK_STATUS_NAME_MAX_LENGTH))
    color: Mapped[str] = mapped_column(String(COLOR_MAX_LENGTH))
    # Board column order (0, 1, 2, ...). Not unique: reordering would otherwise
    # hit temporary duplicates. Lists sort by (position, id).
    position: Mapped[int] = mapped_column(Integer)
    category: Mapped[str] = mapped_column(String(TASK_STATUS_CATEGORY_MAX_LENGTH))

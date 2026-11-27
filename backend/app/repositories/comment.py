"""Database access for task comments. Never commits (see app/repositories/user.py)."""

from datetime import datetime

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import contains_eager

from app.models import TaskComment
from app.schemas.common import Pagination, SortOrder
from app.utils.pagination import PageParams, paginate


async def list_for_task(
    session: AsyncSession, *, task_id: int, order: SortOrder, page: PageParams
) -> tuple[list[TaskComment], Pagination]:
    """CMT-1: one page of a task's comments with their authors (one query plus
    the count). ``ix_task_comments_task_id_created_at`` serves the filter and
    the order; ``id`` breaks ties between comments from the same instant."""
    statement = (
        select(TaskComment)
        .join(TaskComment.author)
        .options(contains_eager(TaskComment.author))
        .where(TaskComment.task_id == task_id)
    )
    if order == "desc":
        statement = statement.order_by(TaskComment.created_at.desc(), TaskComment.id.desc())
    else:
        statement = statement.order_by(TaskComment.created_at.asc(), TaskComment.id.asc())
    return await paginate(session, statement, page)


async def get(session: AsyncSession, task_id: int, comment_id: int) -> TaskComment | None:
    """A comment of THIS task (another task's comment is "not found"), with its author."""
    return await session.scalar(
        select(TaskComment)
        .join(TaskComment.author)
        .options(contains_eager(TaskComment.author))
        .where(TaskComment.id == comment_id, TaskComment.task_id == task_id)
        .execution_options(populate_existing=True)
    )


async def create(session: AsyncSession, *, task_id: int, author_id: int, body: str) -> int:
    """Returns the new id. IntegrityError (fk_task_comments_task_id_tasks): the
    task was deleted a moment ago."""
    comment = TaskComment(task_id=task_id, author_id=author_id, body=body)
    session.add(comment)
    await session.flush()
    return comment.id


async def update_body_if_unchanged(
    session: AsyncSession,
    comment_id: int,
    *,
    author_id: int,
    expected_updated_at: datetime,
    body: str,
) -> bool:
    """CMT-3 in one statement: only the author's comment, only the version they
    loaded. False: someone changed or deleted it in between."""
    changed = await session.scalar(
        update(TaskComment)
        .where(
            TaskComment.id == comment_id,
            TaskComment.author_id == author_id,
            TaskComment.updated_at == expected_updated_at,
        )
        .values(body=body, updated_at=func.statement_timestamp())
        .returning(TaskComment.id)
    )
    return changed is not None


async def delete_comment(session: AsyncSession, comment_id: int) -> bool:
    deleted = await session.scalar(
        delete(TaskComment).where(TaskComment.id == comment_id).returning(TaskComment.id)
    )
    return deleted is not None

"""Task comments (docs/api.md, CMT-1 to CMT-4) and their author rules (A-6)."""

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFoundError, PermissionDeniedError, StaleUpdateError
from app.core.permissions import Action, Feature
from app.db.errors import constraint_name
from app.models import Project, Task, TaskComment
from app.repositories import comment as comment_repository
from app.repositories import task as task_repository
from app.schemas.comment import CommentOut, CreateCommentRequest, UpdateCommentRequest
from app.schemas.common import Page, SortOrder, UserRef
from app.services.authorization import Permissions
from app.utils.pagination import PageParams


async def list_comments(
    session: AsyncSession, project: Project, number: int, *, order: SortOrder, page: PageParams
) -> Page[CommentOut]:
    task = await _task(session, project, number)
    comments, pagination = await comment_repository.list_for_task(
        session, task_id=task.id, order=order, page=page
    )
    return Page[CommentOut](items=[_out(c) for c in comments], pagination=pagination)


async def add_comment(
    session: AsyncSession,
    project: Project,
    number: int,
    author_id: int,
    data: CreateCommentRequest,
) -> CommentOut:
    task = await _task(session, project, number)
    task_id = task.id
    try:
        async with session.begin_nested():
            comment_id = await comment_repository.create(
                session, task_id=task_id, author_id=author_id, body=data.body
            )
    except IntegrityError as exc:
        if constraint_name(exc) == "fk_task_comments_task_id_tasks":
            raise NotFoundError("Task") from exc  # deleted a moment ago
        raise
    await session.commit()
    return await _load(session, task_id, comment_id)


async def edit_comment(
    session: AsyncSession,
    project: Project,
    number: int,
    comment_id: int,
    user_id: int,
    data: UpdateCommentRequest,
) -> CommentOut:
    """Only the author edits (A-6); the route already required comments.edit.
    The same text again writes nothing, so the comment isn't marked "edited"."""
    task = await _task(session, project, number)
    comment = await _comment(session, task, comment_id)
    if comment.author_id != user_id:
        raise PermissionDeniedError("Only the author can edit a comment.")
    if comment.updated_at != data.expected_updated_at:
        raise StaleUpdateError(current=_out(comment).model_dump(mode="json"))
    if comment.body == data.body:
        return _out(comment)

    task_id = task.id
    saved = await comment_repository.update_body_if_unchanged(
        session,
        comment_id,
        author_id=user_id,
        expected_updated_at=data.expected_updated_at,
        body=data.body,
    )
    if not saved:  # edited (or deleted) in another tab since our SELECT
        current = await comment_repository.get(session, task_id, comment_id)
        if current is None:
            raise NotFoundError("Comment")
        raise StaleUpdateError(current=_out(current).model_dump(mode="json"))
    await session.commit()
    return await _load(session, task_id, comment_id)


async def delete_comment(
    session: AsyncSession,
    project: Project,
    permissions: Permissions,
    number: int,
    comment_id: int,
    user_id: int,
) -> None:
    """A-6: the author may delete their own comment (the route required
    comments.edit or comments.delete); comments.delete deletes anyone's
    (moderation)."""
    task = await _task(session, project, number)
    comment = await _comment(session, task, comment_id)
    is_moderator = permissions.allows(Feature.COMMENTS, Action.DELETE)
    is_author = comment.author_id == user_id
    if not is_moderator and not is_author:
        raise PermissionDeniedError("Only the author or a moderator can delete a comment.")
    if not await comment_repository.delete_comment(session, comment.id):
        raise NotFoundError("Comment")  # deleted a moment ago
    await session.commit()


# ── Shared ────────────────────────────────────────────────────────────────────


async def _task(session: AsyncSession, project: Project, number: int) -> Task:
    task = await task_repository.get(session, project.id, number)
    if task is None:
        raise NotFoundError("Task")
    return task


async def _comment(session: AsyncSession, task: Task, comment_id: int) -> TaskComment:
    comment = await comment_repository.get(session, task.id, comment_id)
    if comment is None:
        raise NotFoundError("Comment")
    return comment


async def _load(session: AsyncSession, task_id: int, comment_id: int) -> CommentOut:
    comment = await comment_repository.get(session, task_id, comment_id)
    if comment is None:  # pragma: no cover - we have just written it
        raise NotFoundError("Comment")
    return _out(comment)


def _out(comment: TaskComment) -> CommentOut:
    return CommentOut(
        id=comment.id,
        body=comment.body,
        author=UserRef(id=comment.author.id, name=comment.author.name),
        is_edited=comment.is_edited,
        created_at=comment.created_at,
        updated_at=comment.updated_at,
    )

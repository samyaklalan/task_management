"""Database access for task statuses. Never commits (see app/repositories/user.py).

Grows with the statuses module (M11); for now only what a new project needs.
"""

from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import TaskStatus
from app.models.task_status import DEFAULT_TASK_STATUSES


async def create_defaults(session: AsyncSession, project_id: int) -> None:
    """To Do, In Progress, Done at positions 0, 1, 2 (A-1), in one INSERT."""
    await session.execute(
        insert(TaskStatus),
        [
            {
                "project_id": project_id,
                "name": name,
                "color": color,
                "category": category.value,
                "position": position,
            }
            for position, (name, color, category) in enumerate(DEFAULT_TASK_STATUSES)
        ],
    )

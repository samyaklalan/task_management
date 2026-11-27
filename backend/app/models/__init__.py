"""All SQLAlchemy models, registered in one place.

Importing ``app.models`` (or any module inside it) runs this file first, so
every table is registered on ``Base.metadata`` before any of them is used.
That matters because:

* a foreign key like ``ForeignKey("organizations.id")`` can only be resolved
  once the ``organizations`` model has been imported (found in B9);
* relationships refer to other models by name (``relationship("User")``) and
  are configured only when all of them are known;
* Alembic compares ``Base.metadata`` with the database (B17), so every table
  must be registered to appear in migrations.

Import order doesn't matter: SQLAlchemy resolves foreign keys and
relationships only when they are first used, and by then this file has imported
every model. 14 tables in total (docs/database.md, section 2).
"""

from app.models.organization import Organization
from app.models.project import Project
from app.models.role import Role, RolePermission, UserRole
from app.models.space import Space
from app.models.task import Task
from app.models.task_comment import TaskComment
from app.models.task_status import TaskStatus
from app.models.team import Team, TeamMember
from app.models.token import PasswordResetToken, RefreshToken
from app.models.user import User

__all__ = [
    "Organization",
    "PasswordResetToken",
    "Project",
    "RefreshToken",
    "Role",
    "RolePermission",
    "Space",
    "Task",
    "TaskComment",
    "TaskStatus",
    "Team",
    "TeamMember",
    "User",
    "UserRole",
]

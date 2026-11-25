"""ROLES, ROLE_PERMISSIONS and USER_ROLES: the permission system.

* ``roles``: role names defined per organization (e.g. Admin, Developer).
* ``role_permissions``: what a role may do, one row per feature, with four
  flags (view / create / edit / delete). Feature names are validated in code
  (app/core/permissions.py), not by a CHECK, so adding a feature needs no
  migration (P-5).
* ``user_roles``: which role a user has, either in one project or, with
  ``project_id`` NULL, across the whole organization (P-6).

Schema: docs/database.md, sections 3.9, 3.10 and 3.11.
Timestamps: roles has created_at + updated_at (TimestampMixin); role_permissions
and user_roles have none (PDF #7 added none to them).
"""

from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, Boolean, ForeignKey, String, UniqueConstraint, false
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, BigIntPK, TimestampMixin

if TYPE_CHECKING:
    from app.models.user import User

ROLE_NAME_MAX_LENGTH = 50
FEATURE_MAX_LENGTH = 50
# Created for the first user of every new organization (docs/api.md AUTH-1;
# behaviour proposal A-2, awaiting mentor approval).
ADMIN_ROLE_NAME = "Admin"


class Role(TimestampMixin, Base):
    __tablename__ = "roles"

    id: Mapped[BigIntPK]
    organization_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("organizations.id", ondelete="NO ACTION"),
        index=True,  # ix_roles_organization_id (PDF #10): the organization's roles
    )
    name: Mapped[str] = mapped_column(String(ROLE_NAME_MAX_LENGTH))

    # One-to-many, but small and bounded: at most one row per feature (10 today).
    # That makes it safe as a relationship (relationship policy, B7). Loaded
    # explicitly with selectinload; lazy="raise" otherwise.
    # Used for reading and for creating a role together with its rows (ROLE-2).
    # Changing a role's permissions (ROLE-3) does NOT replace this list: in one
    # flush SQLAlchemy inserts new rows before deleting old ones, so a new row for
    # the same feature collides with uq_role_permissions_role_id_feature. ROLE-3
    # updates each feature's row in place with INSERT ... ON CONFLICT DO UPDATE.
    # passive_deletes: when a role is deleted, PostgreSQL's CASCADE removes its
    # permission rows; SQLAlchemy doesn't load them first just to delete them.
    permissions: Mapped[list["RolePermission"]] = relationship(
        lazy="raise",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="RolePermission.feature",
    )


class RolePermission(Base):
    __tablename__ = "role_permissions"
    __table_args__ = (
        # uq_role_permissions_role_id_feature (PDF #9): one row per feature per
        # role. role_id is its leftmost column, so it also serves the role_id
        # foreign key lookup; no separate index (P-7).
        UniqueConstraint("role_id", "feature"),
    )

    id: Mapped[BigIntPK]
    role_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("roles.id", ondelete="CASCADE"),  # permissions belong to the role (P-4)
    )
    feature: Mapped[str] = mapped_column(String(FEATURE_MAX_LENGTH))
    # Everything is denied unless explicitly granted.
    view_perm: Mapped[bool] = mapped_column(Boolean, server_default=false())
    create_perm: Mapped[bool] = mapped_column(Boolean, server_default=false())
    edit_perm: Mapped[bool] = mapped_column(Boolean, server_default=false())
    delete_perm: Mapped[bool] = mapped_column(Boolean, server_default=false())


class UserRole(Base):
    """Junction table: which role a user has, and where.

    ``project_id`` set:  the role applies in that one project.
    ``project_id`` NULL: the role applies in every project of the organization
                         (org-wide, e.g. the Admin who created the organization).
    """

    __tablename__ = "user_roles"
    __table_args__ = (
        # uq_user_roles_user_id_project_id (P-6): one role per user per project,
        # and at most one org-wide role per user. NULLS NOT DISTINCT (PostgreSQL
        # 15+) makes two NULL project_ids count as equal; a plain UNIQUE would
        # treat every NULL as different and allow duplicates. user_id is its
        # leftmost column, so it also serves the permission lookup (WHERE user_id = ?).
        UniqueConstraint("user_id", "project_id", postgresql_nulls_not_distinct=True),
    )

    id: Mapped[BigIntPK]
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
    )
    role_id: Mapped[int] = mapped_column(
        BigInteger,
        # A role still assigned to someone can't be deleted (P-4).
        ForeignKey("roles.id", ondelete="NO ACTION"),
        index=True,  # ix_user_roles_role_id (PDF #10): "who has this role"
    )
    project_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("projects.id", ondelete="CASCADE"),
        index=True,  # ix_user_roles_project_id (PDF #10): members of a project
    )

    # Many-to-one, loaded with joinedload for the members list (MEM-1).
    user: Mapped["User"] = relationship(lazy="raise")
    role: Mapped["Role"] = relationship(lazy="raise")

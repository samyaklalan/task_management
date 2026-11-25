"""TEAMS and TEAM_MEMBERS: named groups of people inside a project.

Teams group people; spaces group tasks. The two are independent.

Schema: docs/database.md, sections 3.4 (teams) and 3.5 (team_members).
Timestamps: teams has created_at + updated_at (TimestampMixin); team_members has
created_at only (CreatedAtMixin), meaning "when the user joined the team".
"""

from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, BigIntPK, CreatedAtMixin, TimestampMixin

if TYPE_CHECKING:
    from app.models.user import User

TEAM_NAME_MAX_LENGTH = 100


class Team(TimestampMixin, Base):
    __tablename__ = "teams"

    id: Mapped[BigIntPK]
    project_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("projects.id", ondelete="CASCADE"),
        index=True,  # ix_teams_project_id (PDF #10): the project's teams
    )
    name: Mapped[str] = mapped_column(String(TEAM_NAME_MAX_LENGTH))
    description: Mapped[str | None] = mapped_column(Text)

    # No team.members collection: a team's members are listed by a paginated
    # query (TEAM-6), never loaded as a whole (relationship policy, B7).


class TeamMember(CreatedAtMixin, Base):
    """Junction table: which user is in which team.

    The composite primary key (team_id, user_id) makes a duplicate membership
    impossible, even if two "add member" requests arrive at the same moment. The
    add-members service inserts with ON CONFLICT DO NOTHING, so a repeated
    request is harmless (docs/api.md, TEAM-7).
    """

    __tablename__ = "team_members"

    team_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("teams.id", ondelete="CASCADE"),
        primary_key=True,  # pk_team_members (team_id, user_id) also serves "members of a team"
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
        # ix_team_members_user_id (PDF #10): "teams of this user". The primary key
        # can't serve this, because user_id is its second column.
        index=True,
    )

    # Many-to-one, loaded explicitly (joinedload) to show member names in TEAM-6.
    user: Mapped["User"] = relationship(lazy="raise")

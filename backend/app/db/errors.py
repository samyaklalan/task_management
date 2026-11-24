"""Read details out of database errors.

Services insert first and catch the error (never check-then-insert, which two
simultaneous requests could both pass). To give the right message they need to
know *which* constraint failed, e.g. ``uq_users_email`` versus
``uq_organizations_slug``.
"""

from sqlalchemy.exc import DBAPIError


def constraint_name(exc: DBAPIError) -> str | None:
    """The name of the violated constraint, e.g. ``"uq_users_email"``.

    SQLAlchemy wraps the asyncpg error; the original asyncpg exception (which
    has ``constraint_name``) is the wrapped error's ``__cause__``.
    """
    return getattr(exc.orig.__cause__, "constraint_name", None)

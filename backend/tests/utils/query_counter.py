"""Count the SQL statements a block of code sends to PostgreSQL.

Used to prove there's no N+1 problem (Phase 11), for example:

    with count_queries(engine) as queries:
        await task_repository.list_tasks(db_session, ...)
    assert queries.count == 1, str(queries)  # str(): lists every statement on failure

Only real queries are counted. Transaction bookkeeping (SAVEPOINT, RELEASE,
ROLLBACK TO) is ignored: the test session creates savepoints on every
``commit()``, and they are not part of the code being measured. (With asyncpg,
BEGIN/COMMIT don't pass through here at all.)

The listener is attached to the engine, so it counts statements from every
session on that engine while the block runs.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine

_IGNORED_PREFIXES = ("SAVEPOINT", "RELEASE SAVEPOINT", "ROLLBACK TO SAVEPOINT")


@dataclass
class QueryCounter:
    statements: list[str] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.statements)

    def __str__(self) -> str:
        # Shown in assertion failures: every statement, numbered.
        lines = [f"{self.count} statement(s):"]
        lines += [f"  {i}. {sql.strip()}" for i, sql in enumerate(self.statements, 1)]
        return "\n".join(lines)


@contextmanager
def count_queries(engine: AsyncEngine) -> Iterator[QueryCounter]:
    counter = QueryCounter()

    def before_cursor_execute(*args: Any) -> None:
        # Arguments: connection, cursor, statement, parameters, context, executemany
        statement: str = args[2]
        if not statement.lstrip().upper().startswith(_IGNORED_PREFIXES):
            counter.statements.append(statement)

    # Async engines fire their events on the underlying sync engine.
    event.listen(engine.sync_engine, "before_cursor_execute", before_cursor_execute)
    try:
        yield counter
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", before_cursor_execute)

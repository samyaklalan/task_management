"""The ``q`` search parameter of list endpoints (docs/api.md, section 4.3).

    async def list_users(q: SearchQuery, ...): ...      # None, or 3-100 characters
    ... .where(User.name.ilike(contains(q), escape="\\"))

* blank or missing → ``None`` (no search);
* 1-2 characters → ``422 search_too_short``;
* 3-100 characters → searched, case-insensitive, as plain text: ``%`` and ``_``
  are matched literally, not as SQL wildcards.
"""

from typing import Annotated

from fastapi import Depends, Query

from app.core.exceptions import SearchTooShortError

MIN_SEARCH_LENGTH = 3
MAX_SEARCH_LENGTH = 100
LIKE_ESCAPE = "\\"


def _search_query(
    q: Annotated[
        str | None,
        Query(
            max_length=MAX_SEARCH_LENGTH,
            description="Search text, at least 3 characters.",
        ),
    ] = None,
) -> str | None:
    text = (q or "").strip()
    if not text:
        return None
    if len(text) < MIN_SEARCH_LENGTH:
        raise SearchTooShortError()
    return text


SearchQuery = Annotated[str | None, Depends(_search_query)]


def contains(text: str) -> str:
    """ILIKE pattern for "contains ``text``", with LIKE's special characters escaped.

    ``"50%_off"`` → ``"%50\\%\\_off%"``: matches the text "50%_off" literally.
    Use with ``column.ilike(contains(q), escape=LIKE_ESCAPE)``.
    """
    escaped = (
        text.replace(LIKE_ESCAPE, LIKE_ESCAPE * 2)
        .replace("%", LIKE_ESCAPE + "%")
        .replace("_", LIKE_ESCAPE + "_")
    )
    return f"%{escaped}%"


# Summary:
# Used for searching in list endpoints (users, tasks, projects, ...) through the
# ?q=... parameter. Not used by any endpoint yet; ready for the list endpoints.
#
# 1. SearchQuery: reads and checks the search text.
#      - empty or only spaces -> None (no search, everything is listed)
#      - 1-2 characters       -> 422 search_too_short
#      - 3-100 characters     -> searched
#      - over 100 characters  -> 422 (FastAPI's max_length)
#    Spaces at the start and end are trimmed first.
#    Why at least 3: 1-2 letters match almost every row, a slow and useless search.
#
# 2. contains(text): builds a safe "contains" pattern for SQL ILIKE.
#      contains("bug")     -> "%bug%"        (finds "bug" anywhere)
#      contains("50%_off") -> "%50\%\_off%"  (finds the literal text "50%_off")
#    In LIKE, % means "anything" and _ means "any one character"; they are
#    escaped with \ so they are searched as normal characters.
#    Use: .where(Task.title.ilike(contains(q), escape=LIKE_ESCAPE))
#    ilike = case-insensitive ("Bug" also finds "bug").

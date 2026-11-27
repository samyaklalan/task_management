"""Request and response shapes for task comments (docs/api.md, CMT-1 to CMT-4)."""

from datetime import datetime
from typing import Annotated

from pydantic import AwareDatetime, BaseModel, StringConstraints

from app.models.task_comment import COMMENT_BODY_MAX_LENGTH
from app.schemas.common import RequestModel, UserRef

CommentBody = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=COMMENT_BODY_MAX_LENGTH),
]


class CreateCommentRequest(RequestModel):
    """CMT-2. The author comes from the token, never from the body."""

    body: CommentBody


class UpdateCommentRequest(RequestModel):
    """CMT-3: the new body and the version the author is editing."""

    body: CommentBody
    expected_updated_at: AwareDatetime


class CommentOut(BaseModel):
    """CMT-1 item; also the answer of CMT-2 and CMT-3."""

    id: int
    body: str
    author: UserRef
    # Calculated: updated_at > created_at (PDF #5); not a column.
    is_edited: bool
    created_at: datetime
    updated_at: datetime

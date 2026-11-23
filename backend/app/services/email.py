"""Sending emails (A-3).

The rest of the app only knows the ``EmailSender`` interface. Today there is one
implementation, ``ConsoleEmailSender``, which writes the link to the server log
for development. A real provider (SMTP or an email service) is added later as a
second class; nothing else has to change. config.py refuses the console sender
in production.
"""

import logging
from typing import Protocol

from app.core.logging import UNREDACTED_ATTR

logger = logging.getLogger(__name__)


class EmailSender(Protocol):
    async def send_password_reset(self, *, to: str, link: str) -> None: ...


class ConsoleEmailSender:
    """Development only: logs the link instead of emailing it.

    The log line is marked "unredacted" so the token in the link stays readable
    (every other log line hides tokens).
    """

    async def send_password_reset(self, *, to: str, link: str) -> None:
        logger.info(
            "Password reset email to %s: %s", to, link, extra={UNREDACTED_ATTR: True}
        )


def get_email_sender() -> EmailSender:
    """FastAPI dependency; tests replace it with a sender that records messages."""
    return ConsoleEmailSender()

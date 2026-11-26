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

    async def send_invitation(self, *, to: str, link: str) -> None:
        """A new account: "set your password" (USR-4)."""


class ConsoleEmailSender:
    """Development only: logs the link instead of emailing it.

    The log line is marked "unredacted" so the token in the link stays readable
    (every other log line hides tokens).
    """

    async def send_password_reset(self, *, to: str, link: str) -> None:
        logger.info("Password reset email to %s: %s", to, link, extra={UNREDACTED_ATTR: True})

    # Normally the logger (core/logging.py) HIDES secrets like tokens: they become
    # [REDACTED]. extra={UNREDACTED_ATTR: True} marks this one line as "don't hide",
    # so the token in the link stays readable and you can copy the link from the log.
    # Without it you'd see ...?token=[REDACTED], a link you can't open.
    # Development only: config.py refuses the console sender in production.
    async def send_invitation(self, *, to: str, link: str) -> None:
        logger.info("Invitation email to %s: %s", to, link, extra={UNREDACTED_ATTR: True})


def get_email_sender() -> EmailSender:
    """FastAPI dependency; tests replace it with a sender that records messages."""
    return ConsoleEmailSender()


# Summary:
# - EmailSender (Protocol): the interface. It says WHAT a sender must do
#   (send_password_reset, send_invitation), not how. The rest of the app only
#   knows this.
# - send_invitation (USR-4, POST /users): an admin with users.create adds a new
#   user to the organization. Users don't sign up themselves into an existing
#   organization; the admin creates the account. It gets an unusable random
#   password, and this email sends a "set your password" link (the same token
#   flow as password reset, password_reset_tokens). The link expires after 30
#   minutes; the admin can send a new one with AUTH-5. In development it is
#   logged like the reset link.
# - ConsoleEmailSender: development only. Doesn't send a real email; it prints the
#   reset link in the server log. extra={UNREDACTED_ATTR: True} keeps the token
#   readable in this one log line (all other lines hide tokens as [REDACTED]).
# - get_email_sender(): the FastAPI dependency (EmailSenderDep in deps.py).
#   Tests replace it with a fake sender that records messages; later a real sender
#   (SMTP or an email service) is returned here and nothing else changes.
# - config.py refuses the console sender in production.
# - Used by the password-reset flow (AUTH-5) to send the reset link, and by
#   user creation (USR-4) to send the invitation (set-password) link.
# In one line: one interface for sending emails; today it logs the link, later it
# can send real emails without changing the rest of the code.

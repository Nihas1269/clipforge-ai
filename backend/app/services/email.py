"""Outgoing email interface.

Application code builds an `EmailMessage` and hands it to an `EmailSender`.
Rendering templates and delivering through a real provider belong to the
account flows (CF-045); tests and local development use `FakeEmailSender`,
which needs no credentials and never contacts the network.
"""

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Protocol

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EmailMessage:
    to: str
    subject: str
    # Name of the template that renders the body, e.g. "password_reset".
    template: str
    # Template variables. May contain secrets such as single-use tokens, so
    # never log it.
    context: Mapping[str, str] = field(default_factory=dict)


class EmailSender(Protocol):
    def send(self, message: EmailMessage) -> None:
        """Deliver the message or raise an exception if it cannot be sent."""


class FakeEmailSender:
    """Deterministic sender that records messages in `outbox` instead of sending."""

    def __init__(self) -> None:
        self.outbox: list[EmailMessage] = []

    def send(self, message: EmailMessage) -> None:
        self.outbox.append(message)
        # Only the template: the recipient and context are private.
        logger.info("email_recorded", extra={"template": message.template})

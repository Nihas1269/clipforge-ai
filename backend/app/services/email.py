"""Outgoing email interface and providers.

Application code builds an `EmailMessage` and hands it to an `EmailSender`.
`build_email_sender(settings)` returns the configured provider:

- `FakeEmailSender` (EMAIL_PROVIDER=fake, the default) records messages in
  memory. It needs no credentials, never contacts the network, and is used
  by tests and local development. It is refused in production because it
  delivers nothing.
- `SmtpEmailSender` (EMAIL_PROVIDER=smtp) renders a plain-text body and
  delivers it over SMTP with a timeout.

Message contexts can hold single-use tokens and the recipient is personal
data, so neither is ever logged; logs only name the template.
"""

import logging
import smtplib
import ssl
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from email.message import EmailMessage as MimeMessage
from typing import Any, Protocol
from urllib.parse import quote

from pydantic import SecretStr

from app.core.config import Settings

logger = logging.getLogger(__name__)

# Template name -> (frontend path, body). The names match the templates used
# by app/services/account_tokens.py. The token is put in the URL fragment,
# which browsers never send to a server, so it does not end up in web server
# logs or Referer headers.
_TEMPLATES = {
    "email_verification": (
        "verify-email",
        "Confirm the email address of your ClipForge account by opening this "
        "link:\n\n{link}\n\nIf you did not create a ClipForge account, you can "
        "ignore this email.\n",
    ),
    "password_reset": (
        "reset-password",
        "Someone asked to reset the password of your ClipForge account. To "
        "choose a new password, open this link:\n\n{link}\n\nThe link can be "
        "used once and expires soon. If you did not ask for a reset, you can "
        "ignore this email; your password stays unchanged.\n",
    ),
}


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


class EmailNotConfiguredError(Exception):
    """Raised when the configured email provider cannot be used."""


class EmailDeliveryError(Exception):
    """Raised when a provider fails to deliver a message.

    The message is generic; provider errors can contain recipient addresses.
    """

    def __init__(self) -> None:
        super().__init__("The email could not be delivered.")


class FakeEmailSender:
    """Deterministic sender that records messages in `outbox` instead of sending."""

    def __init__(self) -> None:
        self.outbox: list[EmailMessage] = []

    def send(self, message: EmailMessage) -> None:
        self.outbox.append(message)
        # Only the template: the recipient and context are private.
        logger.info("email_recorded", extra={"template": message.template})


class OutboxEmailSender:
    """Collects messages so they can be delivered after the response is sent.

    Used where delivery must not change the response (for example a password
    reset request, which must look the same for unknown addresses).
    """

    def __init__(self) -> None:
        self.messages: list[EmailMessage] = []

    def send(self, message: EmailMessage) -> None:
        self.messages.append(message)


def deliver_all(sender: EmailSender, messages: Iterable[EmailMessage]) -> None:
    """Send each message, logging (never raising) delivery failures."""
    for message in messages:
        try:
            sender.send(message)
        except Exception as exc:  # noqa: BLE001 - a background task must not crash
            logger.error(
                "email_delivery_failed",
                extra={"template": message.template, "error_type": type(exc).__name__},
            )


def render_email_body(message: EmailMessage, frontend_url: str) -> str:
    """Return the plain-text body of the message.

    Raises ValueError for an unknown template or a missing token.
    """
    if message.template not in _TEMPLATES:
        raise ValueError("Unknown email template.")
    token = message.context.get("token")
    if not isinstance(token, str) or not token:
        raise ValueError("The email context has no token.")
    path, body = _TEMPLATES[message.template]
    link = f"{frontend_url.rstrip('/')}/{path}#token={quote(token, safe='')}"
    return body.format(link=link)


class SmtpEmailSender:
    """Deliver messages through an SMTP server."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        security: str,
        from_address: str,
        frontend_url: str,
        timeout_seconds: float,
        username: str | None = None,
        password: SecretStr | None = None,
        smtp_class: Callable[..., Any] = smtplib.SMTP,
        smtp_ssl_class: Callable[..., Any] = smtplib.SMTP_SSL,
    ) -> None:
        if security not in ("starttls", "ssl", "none"):
            raise ValueError("security must be 'starttls', 'ssl' or 'none'.")
        self.host = host
        self.port = port
        self.security = security
        self.from_address = from_address
        self.frontend_url = frontend_url
        self.timeout_seconds = timeout_seconds
        self._username = username
        self._password = password
        self._smtp_class = smtp_class
        self._smtp_ssl_class = smtp_ssl_class

    def _build(self, message: EmailMessage) -> MimeMessage:
        mime = MimeMessage()
        mime["From"] = self.from_address
        mime["To"] = message.to
        mime["Subject"] = message.subject
        mime.set_content(render_email_body(message, self.frontend_url))
        return mime

    def _connect(self) -> Any:
        if self.security == "ssl":
            return self._smtp_ssl_class(
                self.host,
                self.port,
                timeout=self.timeout_seconds,
                context=ssl.create_default_context(),
            )
        return self._smtp_class(self.host, self.port, timeout=self.timeout_seconds)

    def send(self, message: EmailMessage) -> None:
        try:
            mime = self._build(message)
        except ValueError:
            logger.error("email_render_failed", extra={"template": message.template})
            raise EmailDeliveryError() from None
        try:
            with self._connect() as client:
                if self.security == "starttls":
                    client.starttls(context=ssl.create_default_context())
                if self._username and self._password is not None:
                    client.login(self._username, self._password.get_secret_value())
                client.send_message(mime)
        except (smtplib.SMTPException, OSError) as exc:
            logger.warning(
                "email_send_failed",
                extra={"template": message.template, "error_type": type(exc).__name__},
            )
            # Not chained: provider errors can contain recipient addresses.
            raise EmailDeliveryError() from None
        logger.info("email_sent", extra={"template": message.template})


def build_email_sender(settings: Settings) -> EmailSender:
    """Return the email sender selected by EMAIL_PROVIDER.

    Raises EmailNotConfiguredError, naming the missing settings (never their
    values), when the provider cannot be used.
    """
    if settings.EMAIL_PROVIDER == "fake":
        if settings.APP_ENV == "production":
            raise EmailNotConfiguredError(
                "EMAIL_PROVIDER=fake delivers no email and is not allowed in "
                "production. Configure SMTP; see docs/setup/email.md."
            )
        return FakeEmailSender()

    missing = [
        name
        for name in ("SMTP_HOST", "EMAIL_FROM", "FRONTEND_URL")
        if getattr(settings, name) is None
    ]
    if (settings.SMTP_USERNAME is None) != (settings.SMTP_PASSWORD is None):
        missing.append(
            "SMTP_PASSWORD" if settings.SMTP_PASSWORD is None else "SMTP_USERNAME"
        )
    if missing:
        raise EmailNotConfiguredError(
            "Email delivery is not configured; missing: "
            f"{', '.join(missing)}. See docs/setup/email.md."
        )
    if settings.APP_ENV == "production" and settings.SMTP_SECURITY == "none":
        raise EmailNotConfiguredError(
            "SMTP_SECURITY=none sends credentials and tokens unencrypted and is "
            "not allowed in production. Use starttls or ssl."
        )
    return SmtpEmailSender(
        host=settings.SMTP_HOST,  # type: ignore[arg-type]
        port=settings.SMTP_PORT,
        security=settings.SMTP_SECURITY,
        from_address=settings.EMAIL_FROM,  # type: ignore[arg-type]
        frontend_url=settings.FRONTEND_URL,  # type: ignore[arg-type]
        timeout_seconds=settings.SMTP_TIMEOUT_SECONDS,
        username=settings.SMTP_USERNAME,
        password=settings.SMTP_PASSWORD,
    )

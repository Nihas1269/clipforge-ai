"""Email providers: rendering, configuration, and SMTP delivery (mocked)."""

import logging
import smtplib

import pytest
from pydantic import SecretStr, ValidationError

from app.core.config import Settings
from app.services import account_tokens
from app.services.email import (
    EmailDeliveryError,
    EmailMessage,
    EmailNotConfiguredError,
    FakeEmailSender,
    OutboxEmailSender,
    SmtpEmailSender,
    build_email_sender,
    deliver_all,
    render_email_body,
)

TOKEN = "tok_ABC-def_123"
SMTP_SECRET = "test-only-smtp-credential"
STRONG_SECRET = "a-long-random-production-secret-0123456789abcdef"
FRONTEND = "https://app.example.test"

SMTP_SETTINGS = {
    "EMAIL_PROVIDER": "smtp",
    "SMTP_HOST": "smtp.example.test",
    "EMAIL_FROM": "ClipForge <no-reply@example.test>",
    "FRONTEND_URL": FRONTEND,
}


def _message(template="password_reset", token=TOKEN) -> EmailMessage:
    return EmailMessage(
        to="alice@example.com",
        subject="Reset your ClipForge password",
        template=template,
        context={"token": token},
    )


class FakeSMTP:
    """Stands in for smtplib.SMTP / SMTP_SSL and records the calls."""

    instances: list["FakeSMTP"] = []

    def __init__(self, host, port, timeout=None, context=None, fail_with=None):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.context = context
        self.calls: list = []
        self.sent: list = []
        self.closed = False
        self._fail_with = fail_with
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.closed = True
        return False

    def starttls(self, context=None):
        self.calls.append("starttls")

    def login(self, username, password):
        self.calls.append(("login", username, password))

    def send_message(self, message):
        if self._fail_with is not None:
            raise self._fail_with
        self.calls.append("send_message")
        self.sent.append(message)


@pytest.fixture(autouse=True)
def reset_fake_smtp():
    FakeSMTP.instances = []


@pytest.fixture(autouse=True)
def clean_email_environment(monkeypatch):
    for name in (
        "APP_ENV",
        "EMAIL_PROVIDER",
        "EMAIL_FROM",
        "FRONTEND_URL",
        "SMTP_HOST",
        "SMTP_PORT",
        "SMTP_SECURITY",
        "SMTP_USERNAME",
        "SMTP_PASSWORD",
        "SMTP_TIMEOUT_SECONDS",
    ):
        monkeypatch.delenv(name, raising=False)


def _smtp_sender(security="starttls", fail_with=None, **kwargs) -> SmtpEmailSender:
    def factory(*args, **kw):
        return FakeSMTP(*args, fail_with=fail_with, **kw)

    def failing_ssl_factory(*args, **kw):
        raise AssertionError("SMTP_SSL must not be used")

    options = {
        "host": "smtp.example.test",
        "port": 587,
        "security": security,
        "from_address": "no-reply@example.test",
        "frontend_url": FRONTEND,
        "timeout_seconds": 7.5,
        "smtp_class": factory,
        "smtp_ssl_class": factory if security == "ssl" else failing_ssl_factory,
    }
    options.update(kwargs)
    return SmtpEmailSender(**options)


# Rendering


def test_every_account_template_renders_a_link_with_the_token_in_the_fragment():
    verification = render_email_body(
        _message(account_tokens.EMAIL_VERIFICATION_TEMPLATE), FRONTEND + "/"
    )
    reset = render_email_body(_message(account_tokens.PASSWORD_RESET_TEMPLATE), FRONTEND)

    assert f"{FRONTEND}/verify-email#token={TOKEN}" in verification
    assert f"{FRONTEND}/reset-password#token={TOKEN}" in reset


def test_rendering_rejects_unknown_template_and_missing_token():
    with pytest.raises(ValueError):
        render_email_body(_message("unknown"), FRONTEND)
    with pytest.raises(ValueError):
        render_email_body(_message(token=""), FRONTEND)


# Provider selection


def test_fake_provider_is_the_default_outside_production():
    assert isinstance(build_email_sender(Settings(EMAIL_PROVIDER="fake")), FakeEmailSender)


def test_fake_provider_is_refused_in_production():
    settings = Settings(APP_ENV="production", SECRET_KEY=STRONG_SECRET, EMAIL_PROVIDER="fake")

    with pytest.raises(EmailNotConfiguredError, match="production"):
        build_email_sender(settings)


def test_smtp_provider_reports_missing_settings_by_name():
    with pytest.raises(EmailNotConfiguredError) as exc_info:
        build_email_sender(Settings(EMAIL_PROVIDER="smtp", SMTP_HOST="  "))

    message = str(exc_info.value)
    assert "SMTP_HOST" in message
    assert "EMAIL_FROM" in message
    assert "FRONTEND_URL" in message


def test_smtp_username_without_password_is_rejected_without_echoing_values():
    settings = Settings(**SMTP_SETTINGS, SMTP_USERNAME="mailer-user")

    with pytest.raises(EmailNotConfiguredError, match="SMTP_PASSWORD") as exc_info:
        build_email_sender(settings)
    assert "mailer-user" not in str(exc_info.value)


def test_unencrypted_smtp_is_refused_in_production():
    settings = Settings(
        **SMTP_SETTINGS,
        APP_ENV="production",
        SECRET_KEY=STRONG_SECRET,
        SMTP_SECURITY="none",
    )

    with pytest.raises(EmailNotConfiguredError, match="SMTP_SECURITY"):
        build_email_sender(settings)


def test_complete_smtp_configuration_builds_smtp_sender():
    settings = Settings(
        **SMTP_SETTINGS,
        SMTP_PORT=465,
        SMTP_SECURITY="SSL",
        SMTP_USERNAME="mailer-user",
        SMTP_PASSWORD=SMTP_SECRET,
        SMTP_TIMEOUT_SECONDS=5,
    )

    sender = build_email_sender(settings)

    assert isinstance(sender, SmtpEmailSender)
    assert sender.port == 465
    assert sender.security == "ssl"
    assert sender.timeout_seconds == 5
    assert SMTP_SECRET not in repr(settings)


@pytest.mark.parametrize(
    "url", ["ftp://app.example.test", "app.example.test", "https://", "https://a.test/?x=1"]
)
def test_invalid_frontend_url_is_rejected(url):
    with pytest.raises(ValidationError):
        Settings(FRONTEND_URL=url)


def test_frontend_url_trailing_slash_is_removed():
    assert Settings(FRONTEND_URL="https://app.example.test/").FRONTEND_URL == FRONTEND


@pytest.mark.parametrize("name", ["SMTP_PORT", "SMTP_TIMEOUT_SECONDS"])
def test_non_positive_smtp_numbers_are_rejected(name):
    with pytest.raises(ValidationError):
        Settings(**{name: 0})


# SMTP delivery


def test_starttls_delivery_logs_in_and_sends_rendered_message():
    sender = _smtp_sender(username="mailer-user", password=SecretStr(SMTP_SECRET))

    sender.send(_message())

    (client,) = FakeSMTP.instances
    assert (client.host, client.port, client.timeout) == ("smtp.example.test", 587, 7.5)
    assert client.calls == ["starttls", ("login", "mailer-user", SMTP_SECRET), "send_message"]
    assert client.closed
    (mime,) = client.sent
    assert mime["To"] == "alice@example.com"
    assert mime["From"] == "no-reply@example.test"
    assert mime["Subject"] == "Reset your ClipForge password"
    assert f"{FRONTEND}/reset-password#token={TOKEN}" in mime.get_content()


def test_ssl_delivery_uses_smtp_ssl_with_tls_context():
    sender = _smtp_sender(security="ssl")

    sender.send(_message())

    (client,) = FakeSMTP.instances
    assert client.context is not None
    assert client.calls == ["send_message"]


def test_unauthenticated_plain_delivery_skips_starttls_and_login():
    sender = _smtp_sender(security="none")

    sender.send(_message())

    assert FakeSMTP.instances[0].calls == ["send_message"]


@pytest.mark.parametrize(
    "error",
    [
        smtplib.SMTPRecipientsRefused({"alice@example.com": (550, b"no such user")}),
        smtplib.SMTPServerDisconnected("gone"),
        TimeoutError("timed out"),
    ],
)
def test_delivery_errors_raise_generic_error_without_private_data(error, caplog):
    sender = _smtp_sender(fail_with=error)

    with caplog.at_level(logging.DEBUG), pytest.raises(EmailDeliveryError) as exc_info:
        sender.send(_message())

    assert exc_info.value.__cause__ is None
    assert "alice@example.com" not in str(exc_info.value)
    assert "alice@example.com" not in caplog.text
    assert TOKEN not in caplog.text


def test_connection_failure_raises_delivery_error():
    def refuse(*args, **kwargs):
        raise ConnectionRefusedError("refused")

    sender = _smtp_sender(smtp_class=refuse)

    with pytest.raises(EmailDeliveryError):
        sender.send(_message())


def test_render_failure_raises_delivery_error_without_connecting(caplog):
    sender = _smtp_sender()

    with caplog.at_level(logging.DEBUG), pytest.raises(EmailDeliveryError) as exc_info:
        sender.send(_message("unknown"))

    assert exc_info.value.__cause__ is None
    assert FakeSMTP.instances == []
    assert "email_render_failed" in caplog.text
    assert TOKEN not in caplog.text
    assert "alice@example.com" not in caplog.text


def test_successful_delivery_logs_only_the_template(caplog):
    with caplog.at_level(logging.DEBUG):
        _smtp_sender(username="mailer-user", password=SecretStr(SMTP_SECRET)).send(
            _message()
        )

    assert "email_sent" in caplog.text
    assert TOKEN not in caplog.text
    assert SMTP_SECRET not in caplog.text
    assert "alice@example.com" not in caplog.text


# Deferred delivery


def test_deliver_all_continues_after_a_failure_and_logs_it(caplog):
    class FlakySender:
        def __init__(self):
            self.delivered = []

        def send(self, message):
            if message.template == "email_verification":
                raise EmailDeliveryError()
            self.delivered.append(message)

    outbox = OutboxEmailSender()
    outbox.send(_message("email_verification"))
    outbox.send(_message("password_reset"))
    sender = FlakySender()

    with caplog.at_level(logging.INFO):
        deliver_all(sender, outbox.messages)

    assert [m.template for m in sender.delivered] == ["password_reset"]
    assert "email_delivery_failed" in caplog.text
    assert TOKEN not in caplog.text

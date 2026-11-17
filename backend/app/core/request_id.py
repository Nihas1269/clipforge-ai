"""Request IDs for correlating API requests with background jobs and logs.

Every HTTP request gets an ID: the client's `X-Request-ID` header when it is
a short, safe token, otherwise a new random one. The ID is returned in the
`X-Request-ID` response header and is available to code handling the request
through get_request_id(); enqueue_job() stores it on the job.
"""

import re
import uuid
from contextvars import ContextVar
from typing import TypeGuard

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

REQUEST_ID_HEADER = "X-Request-ID"
REQUEST_ID_MAX_LENGTH = 64

# Only characters that are safe in logs and headers; anything else is
# replaced with a generated ID.
_VALID_REQUEST_ID = re.compile(rf"^[A-Za-z0-9._-]{{1,{REQUEST_ID_MAX_LENGTH}}}$")

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)


def new_request_id() -> str:
    return uuid.uuid4().hex


def is_valid_request_id(value: str | None) -> TypeGuard[str]:
    return value is not None and _VALID_REQUEST_ID.fullmatch(value) is not None


def get_request_id() -> str | None:
    """Return the ID of the request being handled, or None outside a request."""
    return request_id_var.get()


class RequestIDMiddleware:
    """Assign a request ID to every HTTP request and echo it in the response."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        incoming = Headers(scope=scope).get(REQUEST_ID_HEADER)
        request_id = incoming if is_valid_request_id(incoming) else new_request_id()

        async def send_with_request_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message)[REQUEST_ID_HEADER] = request_id
            await send(message)

        token = request_id_var.set(request_id)
        try:
            await self.app(scope, receive, send_with_request_id)
        finally:
            request_id_var.reset(token)

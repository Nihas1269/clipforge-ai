"""CSRF protection for cookie-authenticated requests.

Browsers attach the `access_token` cookie to cross-site requests (SameSite=Lax
still allows top-level POST navigations from some older browsers and every
same-site subdomain), so state-changing requests must also prove they come
from a trusted origin. The strategy is an Origin check:

- Safe methods (GET, HEAD, OPTIONS, TRACE) are never checked; they must not
  change state.
- Otherwise the `Origin` header (or, if absent, the origin of `Referer`) must
  be the API's own origin or one of `CORS_ORIGINS`; anything else, including
  `Origin: null`, is rejected with 403.
- A request with neither header is accepted only when it does not carry the
  auth cookie. Browsers always send `Origin` on cross-origin POSTs, so such a
  request comes from a non-browser client that has no ambient credentials to
  abuse.
"""

import enum
import logging
from urllib.parse import urlsplit

from starlette.requests import Request

from app.core.config import Settings
from app.core.errors import AppError

logger = logging.getLogger(__name__)

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})
_DEFAULT_PORTS = {"http": 80, "https": 443}


class OriginCheck(enum.Enum):
    TRUSTED = "trusted"
    UNTRUSTED = "untrusted"
    ABSENT = "absent"


def normalize_origin(value: str) -> str | None:
    """Return `scheme://host[:port]` in lowercase, or None if not a valid origin.

    Default ports are dropped so `https://a.example:443` equals
    `https://a.example`. Paths (as in a Referer) are ignored.
    """
    try:
        parts = urlsplit(value.strip())
        port = parts.port
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    host = parts.hostname
    if scheme not in _DEFAULT_PORTS or not host:
        return None
    origin = f"{scheme}://{host.lower()}"
    if port is not None and port != _DEFAULT_PORTS[scheme]:
        origin += f":{port}"
    return origin


def trusted_origins(request: Request, settings: Settings) -> set[str]:
    """Return the API's own origin plus the configured CORS origins."""
    origins = {normalize_origin(origin) for origin in settings.CORS_ORIGINS}
    origins.add(normalize_origin(f"{request.url.scheme}://{request.url.netloc}"))
    origins.discard(None)
    return origins  # type: ignore[return-value]


def check_origin(request: Request, settings: Settings) -> OriginCheck:
    """Classify the request's Origin (or Referer) against the trusted origins."""
    value = request.headers.get("origin")
    if value is None:
        value = request.headers.get("referer")
    if value is None:
        return OriginCheck.ABSENT
    origin = normalize_origin(value)
    if origin is not None and origin in trusted_origins(request, settings):
        return OriginCheck.TRUSTED
    return OriginCheck.UNTRUSTED


def enforce_csrf(
    request: Request, settings: Settings, *, has_auth_cookie: bool
) -> None:
    """Raise a 403 AppError if a state-changing request fails the origin check."""
    if request.method.upper() in SAFE_METHODS:
        return
    result = check_origin(request, settings)
    if result is OriginCheck.TRUSTED:
        return
    if result is OriginCheck.ABSENT and not has_auth_cookie:
        return
    logger.warning(
        "csrf_rejected",
        extra={
            "method": request.method,
            "path": request.url.path,
            "reason": result.value,
        },
    )
    raise AppError(403, "csrf_failed", "Cross-site request rejected.")

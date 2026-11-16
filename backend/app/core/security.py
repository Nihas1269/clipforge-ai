"""Password hashing, JWT access tokens, and single-use token helpers.

Passwords are hashed with Argon2id (the variant recommended by RFC 9106 and
OWASP) through argon2-cffi, using the library's default parameters. The
encoded hash stores the algorithm, parameters, and salt, so stronger
parameters can be adopted later without invalidating existing hashes.

Access tokens are stateless HS256 JWTs signed with SECRET_KEY. They carry the
user id (`sub`), the issue and expiry times (`iat`, `exp`), and the user's
token version (`ver`); bumping the version (for example on a password reset)
invalidates every token issued before.

Never log or return the password hashes, raw single-use tokens, or access
tokens produced here.
"""

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import jwt
from argon2 import PasswordHasher, Type
from argon2.exceptions import InvalidHashError, VerificationError

from app.core.config import Settings, get_settings

_password_hasher = PasswordHasher(type=Type.ID)

JWT_ALGORITHM = "HS256"

# Password length limits for new passwords. The upper bound keeps hashing
# cost predictable; it is far above any realistic passphrase.
PASSWORD_MIN_LENGTH = 8
PASSWORD_MAX_LENGTH = 256

# Bytes of randomness in single-use tokens (256 bits).
ONE_TIME_TOKEN_BYTES = 32
# Upper bound on the length of a raw single-use token accepted for lookup;
# token_urlsafe(32) produces 43 characters.
ONE_TIME_TOKEN_MAX_LENGTH = 128


def hash_password(password: str) -> str:
    """Return an Argon2id hash of the given plaintext password."""
    if not isinstance(password, str) or not password:
        raise ValueError("Password must be a non-empty string.")
    return _password_hasher.hash(password)


def verify_password(password: str, hashed_password: str) -> bool:
    """Return True if the plaintext password matches the stored hash.

    Returns False for a wrong password and for a missing or malformed hash,
    so callers never have to handle hashing exceptions.
    """
    if not isinstance(password, str) or not isinstance(hashed_password, str):
        return False
    if not password or not hashed_password:
        return False
    try:
        return _password_hasher.verify(hashed_password, password)
    except (VerificationError, InvalidHashError):
        return False


_dummy_password_hash: str | None = None


def verify_password_dummy(password: str) -> None:
    """Spend about the same time as verify_password without a real hash.

    Used when a login names an unknown email so the response time does not
    reveal whether the account exists.
    """
    global _dummy_password_hash
    if _dummy_password_hash is None:
        _dummy_password_hash = hash_password(secrets.token_urlsafe(16))
    verify_password(password or "-", _dummy_password_hash)


class InvalidAccessTokenError(Exception):
    """Raised when an access token is malformed, badly signed, or expired."""


@dataclass(frozen=True)
class AccessTokenPayload:
    user_id: uuid.UUID
    token_version: int
    issued_at: datetime
    expires_at: datetime


def _secret_key(settings: Settings) -> str:
    return settings.SECRET_KEY.get_secret_value()


def create_access_token(
    user_id: uuid.UUID,
    *,
    token_version: int = 0,
    settings: Settings | None = None,
    now: datetime | None = None,
) -> str:
    """Return a signed JWT for the user that expires after ACCESS_TOKEN_EXPIRE_MINUTES."""
    settings = settings or get_settings()
    issued_at = now or datetime.now(timezone.utc)
    expires_at = issued_at + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    payload = {
        "sub": str(user_id),
        "iat": issued_at,
        "exp": expires_at,
        "ver": token_version,
    }
    return jwt.encode(payload, _secret_key(settings), algorithm=JWT_ALGORITHM)


def decode_access_token(
    token: str, *, settings: Settings | None = None
) -> AccessTokenPayload:
    """Verify the token's signature and expiry and return its claims.

    Raises InvalidAccessTokenError for any invalid token; the reason is not
    exposed so callers return the same generic error in every case.
    """
    settings = settings or get_settings()
    if not isinstance(token, str) or not token:
        raise InvalidAccessTokenError()
    try:
        claims = jwt.decode(
            token,
            _secret_key(settings),
            # Pin the algorithm so a token cannot choose a weaker one ("none").
            algorithms=[JWT_ALGORITHM],
            options={"require": ["sub", "exp", "iat"]},
        )
        user_id = uuid.UUID(claims["sub"])
    except (jwt.PyJWTError, ValueError, TypeError, AttributeError) as exc:
        raise InvalidAccessTokenError() from exc

    token_version = claims.get("ver")
    if not isinstance(token_version, int) or isinstance(token_version, bool):
        raise InvalidAccessTokenError()
    return AccessTokenPayload(
        user_id=user_id,
        token_version=token_version,
        issued_at=datetime.fromtimestamp(claims["iat"], timezone.utc),
        expires_at=datetime.fromtimestamp(claims["exp"], timezone.utc),
    )


def generate_one_time_token() -> str:
    """Return a cryptographically random, URL-safe single-use token."""
    return secrets.token_urlsafe(ONE_TIME_TOKEN_BYTES)


def hash_one_time_token(token: str) -> str:
    """Return the SHA-256 hex digest under which a single-use token is stored.

    The tokens carry 256 random bits, so a fast unsalted hash is enough to
    make a leaked database row useless while still allowing a direct lookup.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()

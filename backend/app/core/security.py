"""Password hashing.

Passwords are hashed with Argon2id (the variant recommended by RFC 9106 and
OWASP) through argon2-cffi, using the library's default parameters. The
encoded hash stores the algorithm, parameters, and salt, so stronger
parameters can be adopted later without invalidating existing hashes.

Never log or return the hashes produced here.
"""

from argon2 import PasswordHasher, Type
from argon2.exceptions import InvalidHashError, VerificationError

_password_hasher = PasswordHasher(type=Type.ID)


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

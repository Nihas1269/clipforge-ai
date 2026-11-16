import hashlib
import uuid
from datetime import datetime, timedelta, timezone

import jwt
import pytest

from app.core.config import Settings, get_settings
from app.core.security import (
    InvalidAccessTokenError,
    create_access_token,
    decode_access_token,
    generate_one_time_token,
    hash_one_time_token,
    hash_password,
    verify_password,
)

PASSWORD = "correct horse battery staple"
SECRET = "test-only-jwt-signing-secret-0123456789abcdef"


@pytest.fixture
def settings():
    return Settings(SECRET_KEY=SECRET, ACCESS_TOKEN_EXPIRE_MINUTES=15)


def _encode(claims, secret=SECRET, algorithm="HS256"):
    return jwt.encode(claims, secret, algorithm=algorithm)


def _valid_claims(user_id):
    now = datetime.now(timezone.utc)
    return {
        "sub": str(user_id),
        "iat": now,
        "exp": now + timedelta(minutes=5),
        "ver": 0,
    }


def test_hash_password_differs_from_plaintext():
    hashed = hash_password(PASSWORD)

    assert hashed != PASSWORD
    assert PASSWORD not in hashed


def test_hash_password_uses_argon2id():
    assert hash_password(PASSWORD).startswith("$argon2id$")


def test_hash_password_is_salted():
    assert hash_password(PASSWORD) != hash_password(PASSWORD)


def test_verify_password_accepts_correct_password():
    assert verify_password(PASSWORD, hash_password(PASSWORD)) is True


def test_verify_password_rejects_wrong_password():
    hashed = hash_password(PASSWORD)

    assert verify_password("wrong password", hashed) is False
    assert verify_password(PASSWORD.upper(), hashed) is False


@pytest.mark.parametrize("hashed", ["", "not-a-hash", "$argon2id$v=19$broken"])
def test_verify_password_rejects_malformed_hash(hashed):
    assert verify_password(PASSWORD, hashed) is False


def test_verify_password_rejects_empty_password():
    assert verify_password("", hash_password(PASSWORD)) is False


@pytest.mark.parametrize("password", ["", None, 123])
def test_hash_password_rejects_invalid_password(password):
    with pytest.raises(ValueError):
        hash_password(password)


def test_access_token_has_sub_and_exp_from_settings(settings):
    user_id = uuid.uuid4()
    issued = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)

    token = create_access_token(user_id, settings=settings, now=issued)

    assert jwt.get_unverified_header(token)["alg"] == "HS256"
    claims = jwt.decode(
        token, SECRET, algorithms=["HS256"], options={"verify_exp": False}
    )
    assert claims["sub"] == str(user_id)
    assert claims["exp"] == int((issued + timedelta(minutes=15)).timestamp())
    assert claims["iat"] == int(issued.timestamp())
    assert claims["ver"] == 0


def test_decode_access_token_returns_claims(settings):
    user_id = uuid.uuid4()

    payload = decode_access_token(
        create_access_token(user_id, token_version=3, settings=settings),
        settings=settings,
    )

    assert payload.user_id == user_id
    assert payload.token_version == 3
    assert payload.expires_at - payload.issued_at == timedelta(minutes=15)
    assert payload.expires_at > datetime.now(timezone.utc)


def test_decode_access_token_uses_settings_by_default(monkeypatch):
    monkeypatch.setenv("SECRET_KEY", SECRET)
    get_settings.cache_clear()
    user_id = uuid.uuid4()

    token = create_access_token(user_id)

    # Signed with the SECRET_KEY from the environment, not a cached value.
    assert jwt.decode(token, SECRET, algorithms=["HS256"])["sub"] == str(user_id)
    assert decode_access_token(token).user_id == user_id


def test_decode_access_token_rejects_expired_token(settings):
    issued = datetime.now(timezone.utc) - timedelta(minutes=16)
    token = create_access_token(uuid.uuid4(), settings=settings, now=issued)

    with pytest.raises(InvalidAccessTokenError):
        decode_access_token(token, settings=settings)


def test_decode_access_token_rejects_wrong_secret(settings):
    token = _encode(
        _valid_claims(uuid.uuid4()), secret="another-secret-0123456789abcdef!"
    )

    with pytest.raises(InvalidAccessTokenError):
        decode_access_token(token, settings=settings)


def test_decode_access_token_rejects_tampered_token(settings):
    token = create_access_token(uuid.uuid4(), settings=settings)
    header, payload, signature = token.split(".")
    forged = _encode(_valid_claims(uuid.uuid4())).split(".")[1]

    with pytest.raises(InvalidAccessTokenError):
        decode_access_token(f"{header}.{forged}.{signature}", settings=settings)


def test_decode_access_token_rejects_unsigned_token(settings):
    token = jwt.encode(_valid_claims(uuid.uuid4()), None, algorithm="none")

    with pytest.raises(InvalidAccessTokenError):
        decode_access_token(token, settings=settings)


@pytest.mark.parametrize("missing", ["sub", "exp", "iat", "ver"])
def test_decode_access_token_rejects_missing_claims(settings, missing):
    claims = _valid_claims(uuid.uuid4())
    del claims[missing]

    with pytest.raises(InvalidAccessTokenError):
        decode_access_token(_encode(claims), settings=settings)


@pytest.mark.parametrize(
    ("claim", "value"), [("sub", "not-a-uuid"), ("ver", "0"), ("ver", True)]
)
def test_decode_access_token_rejects_invalid_claims(settings, claim, value):
    claims = _valid_claims(uuid.uuid4())
    claims[claim] = value

    with pytest.raises(InvalidAccessTokenError):
        decode_access_token(_encode(claims), settings=settings)


@pytest.mark.parametrize("token", ["", "not-a-jwt", "a.b.c", None])
def test_decode_access_token_rejects_malformed_token(settings, token):
    with pytest.raises(InvalidAccessTokenError):
        decode_access_token(token, settings=settings)


def test_one_time_tokens_are_random_and_hashed_with_sha256():
    first = generate_one_time_token()
    second = generate_one_time_token()

    assert first != second
    assert len(first) >= 43
    assert hash_one_time_token(first) == hashlib.sha256(first.encode()).hexdigest()
    assert hash_one_time_token(first) != first

import pytest

from app.core.security import hash_password, verify_password

PASSWORD = "correct horse battery staple"


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

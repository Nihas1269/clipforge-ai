from typing import Annotated, Literal

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.testclient import TestClient
from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic_core import PydanticCustomError

from app.config import Settings, get_settings, parse_cors_origins, sanitize_cors_origins
from app.core.errors import AppError
from app.main import create_app

ALLOWED_ORIGIN = "https://app.example.com"
DISALLOWED_ORIGIN = "https://evil.example.com"
SENSITIVE_VALUE = "hunter2-sensitive-value"
INTERNAL_DETAIL = "internal-db-host=10.0.0.5"


class SecretPayload(BaseModel):
    token: str

    @field_validator("token")
    @classmethod
    def reject_token(cls, value: str) -> str:
        # Simulates a validator whose exception text includes both the
        # submitted value and internal details.
        raise ValueError(f"token {value!r} rejected by {INTERNAL_DETAIL}")


class CustomTypePayload(BaseModel):
    code: str

    @field_validator("code")
    @classmethod
    def reject_code(cls, value: str) -> str:
        raise PydanticCustomError(
            f"Leaked {value} type",
            "custom failure for {value} at {where}",
            {"value": value, "where": INTERNAL_DETAIL},
        )


IDENTIFIER_SHAPED_TYPE = "internal_password_hunter2"


class IdentifierTypePayload(BaseModel):
    code: str

    @field_validator("code")
    @classmethod
    def reject_code(cls, value: str) -> str:
        # A syntactically valid identifier that still carries sensitive data.
        raise PydanticCustomError(IDENTIFIER_SHAPED_TYPE, "custom failure")


class Cat(BaseModel):
    kind: Literal["cat"]


class Dog(BaseModel):
    kind: Literal["dog"]


class PetPayload(BaseModel):
    pet: Annotated[Cat | Dog, Field(discriminator="kind")]


class StrictPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str


def build_app(origins: list[str] | None = None) -> FastAPI:
    app = create_app(Settings(CORS_ORIGINS=origins or [ALLOWED_ORIGIN]))

    @app.get("/test/items")
    def items(count: int) -> dict[str, int]:
        return {"count": count}

    @app.get("/test/app-error")
    def app_error() -> None:
        raise AppError(409, "clip_conflict", "Clip already exists.")

    @app.get("/test/unauthorized")
    def unauthorized() -> None:
        raise HTTPException(
            status_code=401,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )

    @app.get("/test/server-error")
    def server_error() -> None:
        raise HTTPException(status_code=500, detail="internal secret detail")

    @app.get("/test/reset-content")
    def reset_content() -> None:
        raise HTTPException(
            status_code=205,
            detail="should not be sent",
            headers={"X-Reset": "1"},
        )

    @app.get("/test/stale-body-headers")
    def stale_body_headers() -> None:
        raise HTTPException(
            status_code=429,
            detail="Too many requests",
            headers={
                "Content-Length": "999",
                "Content-Type": "text/plain",
                "Content-Encoding": "gzip",
                "ETag": '"stale"',
                "Content-Range": "bytes 0-9/100",
                "Retry-After": "30",
            },
        )

    @app.get("/test/range-not-satisfiable")
    def range_not_satisfiable() -> None:
        raise HTTPException(
            status_code=416,
            detail="Range not satisfiable",
            headers={"Content-Range": "bytes */1234", "Content-Type": "text/plain"},
        )

    @app.get("/test/precondition-failed")
    def precondition_failed() -> None:
        raise HTTPException(
            status_code=412,
            detail="Precondition failed",
            headers={
                "ETag": '"current-v2"',
                "Content-Type": "text/plain",
                "Content-Range": "bytes 0-9/100",
            },
        )

    @app.get("/test/reset-content-stale-length")
    def reset_content_stale_length() -> None:
        raise HTTPException(
            status_code=205,
            headers={"Content-Length": "42", "X-Reset": "1"},
        )

    @app.post("/test/secret")
    def secret(payload: SecretPayload) -> dict[str, str]:
        return {"token": payload.token}

    @app.post("/test/custom-type")
    def custom_type(payload: CustomTypePayload) -> dict[str, str]:
        return {"code": payload.code}

    @app.post("/test/identifier-type")
    def identifier_type(payload: IdentifierTypePayload) -> dict[str, str]:
        return {"code": payload.code}

    @app.post("/test/pet")
    def pet(payload: PetPayload) -> dict[str, str]:
        return {"kind": payload.pet.kind}

    @app.post("/test/int-mapping")
    def int_mapping(payload: dict[int, int]) -> dict[int, int]:
        return payload

    @app.post("/test/strict")
    def strict(payload: StrictPayload) -> dict[str, str]:
        return {"name": payload.name}

    return app


@pytest.fixture
def client() -> TestClient:
    return TestClient(build_app())


def assert_error_shape(body: dict) -> None:
    assert set(body) == {"error"}
    assert set(body["error"]) == {"code", "message", "details"}
    assert isinstance(body["error"]["code"], str)
    assert isinstance(body["error"]["message"], str)


def test_ping_returns_ok(client):
    response = client.get("/api/v1/ping")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_health_remains_at_root(client):
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_unknown_route_returns_404_in_standard_shape(client):
    response = client.get("/api/v1/does-not-exist")

    assert response.status_code == 404
    body = response.json()
    assert_error_shape(body)
    assert body["error"] == {
        "code": "not_found",
        "message": "Not Found",
        "details": None,
    }


def test_method_not_allowed_preserves_status_and_allow_header(client):
    response = client.post("/api/v1/ping")

    assert response.status_code == 405
    assert "GET" in response.headers["allow"]
    body = response.json()
    assert_error_shape(body)
    assert body["error"]["code"] == "method_not_allowed"


def test_http_exception_preserves_status_and_headers(client):
    response = client.get("/test/unauthorized")

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.json() == {
        "error": {
            "code": "unauthorized",
            "message": "Not authenticated",
            "details": None,
        }
    }


def test_server_http_exception_does_not_leak_detail(client):
    response = client.get("/test/server-error")

    assert response.status_code == 500
    assert "internal secret detail" not in response.text
    assert response.json() == {
        "error": {
            "code": "internal_server_error",
            "message": "Internal Server Error",
            "details": None,
        }
    }


def test_reset_content_http_exception_has_empty_body(client):
    response = client.get("/test/reset-content")

    assert response.status_code == 205
    assert response.content == b""
    assert response.headers["x-reset"] == "1"
    assert "should not be sent" not in response.text


def test_http_exception_recomputes_stale_body_headers(client):
    response = client.get("/test/stale-body-headers")

    assert response.status_code == 429
    assert response.headers["retry-after"] == "30"
    assert response.headers["content-type"] == "application/json"
    assert response.headers["content-length"] == str(len(response.content))
    assert "content-encoding" not in response.headers
    assert "etag" not in response.headers
    assert "content-range" not in response.headers
    assert response.json() == {
        "error": {
            "code": "too_many_requests",
            "message": "Too many requests",
            "details": None,
        }
    }


def test_range_not_satisfiable_preserves_content_range(client):
    response = client.get("/test/range-not-satisfiable")

    assert response.status_code == 416
    assert response.headers["content-range"] == "bytes */1234"
    assert response.headers["content-type"] == "application/json"
    body = response.json()
    assert_error_shape(body)
    # The status phrase differs between Python versions ("Requested Range Not
    # Satisfiable" before 3.13, "Range Not Satisfiable" after).
    assert body["error"]["code"].endswith("range_not_satisfiable")
    assert body["error"]["message"] == "Range not satisfiable"


def test_precondition_failed_preserves_etag(client):
    response = client.get("/test/precondition-failed")

    assert response.status_code == 412
    assert response.headers["etag"] == '"current-v2"'
    assert response.headers["content-type"] == "application/json"
    assert "content-range" not in response.headers
    assert response.json() == {
        "error": {
            "code": "precondition_failed",
            "message": "Precondition failed",
            "details": None,
        }
    }


def test_bodyless_http_exception_drops_stale_content_length(client):
    response = client.get("/test/reset-content-stale-length")

    assert response.status_code == 205
    assert response.content == b""
    assert response.headers["x-reset"] == "1"
    assert response.headers.get("content-length", "0") == "0"


def test_validation_error_returns_422_in_standard_shape(client):
    response = client.get("/test/items", params={"count": "secret-value"})

    assert response.status_code == 422
    body = response.json()
    assert_error_shape(body)
    assert body["error"]["code"] == "validation_error"
    assert body["error"]["message"] == "Request validation failed."

    details = body["error"]["details"]
    assert isinstance(details, list)
    assert len(details) == 1
    assert set(details[0]) == {"loc", "msg", "type"}
    assert details[0]["loc"] == ["query", "count"]
    # The submitted input is not echoed back to the client.
    assert "secret-value" not in response.text


def test_validation_error_uses_fixed_public_message(client):
    response = client.get("/test/items", params={"count": "secret-value"})

    assert response.json()["error"]["details"] == [
        {
            "loc": ["query", "count"],
            "msg": "Value must be an integer.",
            "type": "int_parsing",
        }
    ]


def test_validation_error_does_not_expose_custom_validator_text(client):
    response = client.post("/test/secret", json={"token": SENSITIVE_VALUE})

    assert response.status_code == 422
    body = response.json()
    assert_error_shape(body)
    assert body["error"]["code"] == "validation_error"
    assert body["error"]["details"] == [
        {"loc": ["body", "token"], "msg": "Invalid value.", "type": "value_error"}
    ]
    assert SENSITIVE_VALUE not in response.text
    assert INTERNAL_DETAIL not in response.text
    assert "rejected" not in response.text


def test_validation_error_does_not_expose_custom_error_type_or_message(client):
    response = client.post("/test/custom-type", json={"code": SENSITIVE_VALUE})

    assert response.status_code == 422
    body = response.json()
    assert_error_shape(body)
    assert body["error"]["details"] == [
        {"loc": ["body", "code"], "msg": "Invalid value.", "type": "value_error"}
    ]
    assert SENSITIVE_VALUE not in response.text
    assert INTERNAL_DETAIL not in response.text
    assert "custom failure" not in response.text


def test_validation_error_does_not_expose_identifier_shaped_custom_type(client):
    response = client.post("/test/identifier-type", json={"code": "abc"})

    assert response.status_code == 422
    body = response.json()
    assert_error_shape(body)
    assert body["error"]["details"] == [
        {"loc": ["body", "code"], "msg": "Invalid value.", "type": "value_error"}
    ]
    assert IDENTIFIER_SHAPED_TYPE not in response.text
    assert "hunter2" not in response.text


def test_discriminator_validation_error_does_not_echo_tag(client):
    response = client.post("/test/pet", json={"pet": {"kind": SENSITIVE_VALUE}})

    assert response.status_code == 422
    body = response.json()
    assert_error_shape(body)
    assert body["error"]["details"] == [
        {
            "loc": ["body", "pet"],
            "msg": "Value has an invalid discriminator.",
            "type": "union_tag_invalid",
        }
    ]
    assert SENSITIVE_VALUE not in response.text


def test_validation_error_does_not_echo_submitted_dict_key(client):
    response = client.post("/test/int-mapping", json={SENSITIVE_VALUE: 1})

    assert response.status_code == 422
    body = response.json()
    assert_error_shape(body)
    assert body["error"]["details"] == [
        {
            "loc": ["body", "*", "[key]"],
            "msg": "Value must be an integer.",
            "type": "int_parsing",
        }
    ]
    assert SENSITIVE_VALUE not in response.text


def test_validation_error_does_not_echo_forbidden_extra_key(client):
    response = client.post("/test/strict", json={"name": "a", SENSITIVE_VALUE: 1})

    assert response.status_code == 422
    body = response.json()
    assert_error_shape(body)
    assert body["error"]["details"] == [
        {
            "loc": ["body", "*"],
            "msg": "Extra fields are not permitted.",
            "type": "extra_forbidden",
        }
    ]
    assert SENSITIVE_VALUE not in response.text


def test_invalid_json_body_does_not_echo_input(client):
    response = client.post(
        "/test/secret",
        content='{"token": "' + SENSITIVE_VALUE + '"',
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 422
    body = response.json()
    assert_error_shape(body)
    assert body["error"]["details"][0]["type"] == "json_invalid"
    assert body["error"]["details"][0]["msg"] == "Invalid JSON."
    assert SENSITIVE_VALUE not in response.text


def test_missing_parameter_returns_422_in_standard_shape(client):
    response = client.get("/test/items")

    assert response.status_code == 422
    body = response.json()
    assert_error_shape(body)
    assert body["error"]["details"][0]["loc"] == ["query", "count"]


def test_app_error_returns_its_status_and_code(client):
    response = client.get("/test/app-error")

    assert response.status_code == 409
    assert response.json() == {
        "error": {
            "code": "clip_conflict",
            "message": "Clip already exists.",
            "details": None,
        }
    }


def test_app_error_fields():
    error = AppError(400, "bad_input", "Bad input.", details={"field": "name"})

    assert error.status_code == 400
    assert error.code == "bad_input"
    assert error.message == "Bad input."
    assert error.details == {"field": "name"}


def test_cors_preflight_from_allowed_origin_succeeds(client):
    response = client.options(
        "/api/v1/ping",
        headers={
            "Origin": ALLOWED_ORIGIN,
            "Access-Control-Request-Method": "GET",
        },
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == ALLOWED_ORIGIN
    assert response.headers["access-control-allow-credentials"] == "true"


def test_cors_preflight_from_disallowed_origin_is_rejected(client):
    response = client.options(
        "/api/v1/ping",
        headers={
            "Origin": DISALLOWED_ORIGIN,
            "Access-Control-Request-Method": "GET",
        },
    )

    assert response.status_code != 200
    assert "access-control-allow-origin" not in response.headers


def test_cors_credentialed_request_from_disallowed_origin_is_not_allowed(client):
    response = client.get(
        "/api/v1/ping",
        headers={"Origin": DISALLOWED_ORIGIN, "Cookie": "session=abc"},
    )

    assert "access-control-allow-origin" not in response.headers


def test_cors_headers_present_on_error_responses_for_allowed_origin(client):
    response = client.get("/api/v1/does-not-exist", headers={"Origin": ALLOWED_ORIGIN})

    assert response.status_code == 404
    assert response.headers["access-control-allow-origin"] == ALLOWED_ORIGIN


def test_cors_wildcard_origin_is_ignored_with_credentials():
    origins = parse_cors_origins(f"*, {ALLOWED_ORIGIN}")
    client = TestClient(build_app(origins))

    assert origins == [ALLOWED_ORIGIN]

    preflight = client.options(
        "/api/v1/ping",
        headers={
            "Origin": DISALLOWED_ORIGIN,
            "Access-Control-Request-Method": "GET",
        },
    )
    assert "access-control-allow-origin" not in preflight.headers

    credentialed = client.get(
        "/api/v1/ping",
        headers={"Origin": DISALLOWED_ORIGIN, "Cookie": "session=abc"},
    )
    assert "access-control-allow-origin" not in credentialed.headers


def cors_middleware_options(app: FastAPI) -> dict:
    for middleware in app.user_middleware:
        if middleware.cls is CORSMiddleware:
            return middleware.kwargs
    raise AssertionError("CORSMiddleware is not configured")


def test_settings_constructed_directly_drops_wildcard_origin():
    settings = Settings(CORS_ORIGINS=["*", f" {ALLOWED_ORIGIN} ", ""])

    assert settings.CORS_ORIGINS == [ALLOWED_ORIGIN]


def test_settings_constructed_directly_with_only_wildcard_allows_no_origins():
    assert Settings(CORS_ORIGINS=["*"]).CORS_ORIGINS == []


def test_sanitize_cors_origins_drops_wildcard_and_blanks():
    assert sanitize_cors_origins(["*", " ", ALLOWED_ORIGIN, " * "]) == [ALLOWED_ORIGIN]


def test_create_app_never_combines_credentials_with_wildcard():
    app = create_app(Settings(CORS_ORIGINS=["*"]))
    options = cors_middleware_options(app)

    assert options["allow_credentials"] is True
    assert "*" not in options["allow_origins"]
    assert options["allow_origins"] == []


def test_create_app_with_direct_wildcard_settings_rejects_any_origin():
    client = TestClient(create_app(Settings(CORS_ORIGINS=["*", ALLOWED_ORIGIN])))

    preflight = client.options(
        "/api/v1/ping",
        headers={
            "Origin": DISALLOWED_ORIGIN,
            "Access-Control-Request-Method": "GET",
        },
    )
    assert "access-control-allow-origin" not in preflight.headers

    credentialed = client.get(
        "/api/v1/ping",
        headers={"Origin": DISALLOWED_ORIGIN, "Cookie": "session=abc"},
    )
    assert "access-control-allow-origin" not in credentialed.headers

    allowed = client.get("/api/v1/ping", headers={"Origin": ALLOWED_ORIGIN})
    assert allowed.headers["access-control-allow-origin"] == ALLOWED_ORIGIN
    assert allowed.headers["access-control-allow-credentials"] == "true"


def test_create_app_sanitizes_origins_mutated_after_settings_construction():
    settings = Settings(CORS_ORIGINS=[ALLOWED_ORIGIN])
    settings.CORS_ORIGINS.append("*")

    options = cors_middleware_options(create_app(settings))

    assert options["allow_origins"] == [ALLOWED_ORIGIN]


def test_parse_cors_origins_ignores_blank_entries():
    assert parse_cors_origins("") == []
    assert parse_cors_origins(" https://a.example.com , ,https://b.example.com ") == [
        "https://a.example.com",
        "https://b.example.com",
    ]


def test_get_settings_reads_cors_origins_from_environment(monkeypatch):
    monkeypatch.setenv("CORS_ORIGINS", f"{ALLOWED_ORIGIN},http://localhost:3000")

    assert get_settings().CORS_ORIGINS == [ALLOWED_ORIGIN, "http://localhost:3000"]


def test_get_settings_defaults_to_no_cors_origins(monkeypatch):
    monkeypatch.delenv("CORS_ORIGINS", raising=False)

    assert get_settings().CORS_ORIGINS == []

import typing
from http import HTTPStatus
from typing import Any

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

# Statuses that must not include a response body (204 No Content,
# 205 Reset Content, 304 Not Modified).
_BODYLESS_STATUS_CODES = {204, 205, 304}

# Headers that control message framing. They are recomputed for the response
# that is actually sent, so values supplied on an exception are dropped.
_FRAMING_HEADERS = {"content-length", "transfer-encoding"}

# Headers that describe the body representation. They are dropped when the
# body is replaced with the standard JSON error so they never describe the
# original representation. Other headers (such as `Allow`, `WWW-Authenticate`
# and `Retry-After`) are preserved.
_REPRESENTATION_HEADERS = _FRAMING_HEADERS | {
    "content-type",
    "content-encoding",
    "content-range",
    "content-md5",
    "content-digest",
    "repr-digest",
    "digest",
    "etag",
}

# Headers that describe the target resource rather than the body for specific
# statuses, so they are kept: `Content-Range` on 416 gives the resource size,
# and `ETag` on 412 gives the current validator for conditional-update recovery.
_RESOURCE_HEADERS_BY_STATUS = {
    412: {"etag"},
    416: {"content-range"},
}

# Location segments that are safe to return in validation error `loc` lists.
# Any other string segment that is not a field name or alias declared by the
# matched route is replaced, because it can be a key taken from the request
# (for example a dictionary key or a forbidden extra field).
_LOCATION_SOURCES = {"body", "query", "path", "header", "cookie"}
_FIXED_LOCATION_SEGMENTS = {"[key]"}
_REDACTED_LOCATION_SEGMENT = "*"

# Public messages for validation errors, keyed by error type. Raw validation
# messages are never returned because they can embed submitted values or text
# from exceptions raised inside custom validators.
_VALIDATION_MESSAGES = {
    "missing": "Field required.",
    "extra_forbidden": "Extra fields are not permitted.",
    "json_invalid": "Invalid JSON.",
    "json_type": "Invalid JSON input type.",
    "int_type": "Value must be an integer.",
    "int_parsing": "Value must be an integer.",
    "int_from_float": "Value must be an integer.",
    "float_type": "Value must be a number.",
    "float_parsing": "Value must be a number.",
    "decimal_type": "Value must be a decimal number.",
    "decimal_parsing": "Value must be a decimal number.",
    "bool_type": "Value must be a boolean.",
    "bool_parsing": "Value must be a boolean.",
    "string_type": "Value must be a string.",
    "string_too_short": "Value is too short.",
    "string_too_long": "Value is too long.",
    "string_pattern_mismatch": "Value does not match the required pattern.",
    "too_short": "Value has too few items.",
    "too_long": "Value has too many items.",
    "greater_than": "Value is too small.",
    "greater_than_equal": "Value is too small.",
    "less_than": "Value is too large.",
    "less_than_equal": "Value is too large.",
    "multiple_of": "Value is not a valid multiple.",
    "list_type": "Value must be a list.",
    "dict_type": "Value must be an object.",
    "model_type": "Value must be an object.",
    "model_attributes_type": "Value must be an object.",
    "literal_error": "Value is not one of the permitted values.",
    "enum": "Value is not one of the permitted values.",
    "union_tag_invalid": "Value has an invalid discriminator.",
    "union_tag_not_found": "Discriminator field is missing.",
    "uuid_type": "Value must be a valid UUID.",
    "uuid_parsing": "Value must be a valid UUID.",
    "date_type": "Value must be a valid date.",
    "date_parsing": "Value must be a valid date.",
    "date_from_datetime_parsing": "Value must be a valid date.",
    "datetime_type": "Value must be a valid datetime.",
    "datetime_parsing": "Value must be a valid datetime.",
    "datetime_from_date_parsing": "Value must be a valid datetime.",
    "time_type": "Value must be a valid time.",
    "time_parsing": "Value must be a valid time.",
    "url_type": "Value must be a valid URL.",
    "url_parsing": "Value must be a valid URL.",
    "url_scheme": "URL scheme is not permitted.",
    "value_error": "Invalid value.",
    "assertion_error": "Invalid value.",
}
_DEFAULT_VALIDATION_MESSAGE = "Invalid value."
_DEFAULT_VALIDATION_TYPE = "value_error"


class AppError(Exception):
    """Application error that is returned to the client in the standard error shape."""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        details: Any = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details


def error_response(
    status_code: int,
    code: str,
    message: str,
    details: Any = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    """Build a response in the standard `{"error": {...}}` shape."""
    content = {
        "error": {
            "code": code,
            "message": message,
            "details": jsonable_encoder(details),
        }
    }
    return JSONResponse(status_code=status_code, content=content, headers=headers)


def _status_phrase(status_code: int) -> str:
    try:
        return HTTPStatus(status_code).phrase
    except ValueError:
        return "HTTP error"


def _status_code_name(status_code: int) -> str:
    """Return a machine-readable code such as "not_found" for a status code."""
    try:
        phrase = HTTPStatus(status_code).phrase
    except ValueError:
        return "http_error"
    return phrase.lower().replace(" ", "_").replace("-", "_")


def _without_headers(
    headers: dict[str, str] | None, excluded: set[str]
) -> dict[str, str] | None:
    if not headers:
        return None
    return {
        name: value for name, value in headers.items() if name.lower() not in excluded
    }


async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    return error_response(exc.status_code, exc.code, exc.message, exc.details)


async def http_exception_handler(
    request: Request, exc: StarletteHTTPException
) -> Response:
    headers = getattr(exc, "headers", None)
    if exc.status_code in _BODYLESS_STATUS_CODES:
        return Response(
            status_code=exc.status_code,
            headers=_without_headers(headers, _FRAMING_HEADERS),
        )

    message = _status_phrase(exc.status_code)
    details = None
    # Server errors always use the generic status phrase so internal details
    # are never exposed to the client.
    if exc.status_code < 500:
        if isinstance(exc.detail, str):
            message = exc.detail
        elif exc.detail is not None:
            details = exc.detail

    excluded_headers = _REPRESENTATION_HEADERS - _RESOURCE_HEADERS_BY_STATUS.get(
        exc.status_code, set()
    )
    return error_response(
        exc.status_code,
        _status_code_name(exc.status_code),
        message,
        details,
        headers=_without_headers(headers, excluded_headers),
    )


def _collect_annotation_names(annotation: Any, names: set[str], seen: set[int]) -> None:
    """Add field names and aliases of models reachable from `annotation`."""
    if id(annotation) in seen:
        return
    seen.add(id(annotation))
    if (
        typing.get_origin(annotation) is None
        and isinstance(annotation, type)
        and issubclass(annotation, BaseModel)
    ):
        for name, field in annotation.model_fields.items():
            names.add(name)
            for alias in (field.alias, field.validation_alias):
                if isinstance(alias, str):
                    names.add(alias)
            _collect_annotation_names(field.annotation, names, seen)
        return
    for arg in typing.get_args(annotation):
        _collect_annotation_names(arg, names, seen)


def _collect_dependant_names(dependant: Any, names: set[str], seen: set[int]) -> None:
    """Add parameter names, aliases, and nested model fields of a dependant."""
    for params_attr in (
        "path_params",
        "query_params",
        "header_params",
        "cookie_params",
        "body_params",
    ):
        for field in getattr(dependant, params_attr, ()):
            for name in (getattr(field, "name", None), getattr(field, "alias", None)):
                if isinstance(name, str):
                    names.add(name)
            field_info = getattr(field, "field_info", None)
            _collect_annotation_names(
                getattr(field_info, "annotation", None), names, seen
            )
    for sub_dependant in getattr(dependant, "dependencies", ()):
        _collect_dependant_names(sub_dependant, names, seen)


def _declared_names(request: Request) -> set[str]:
    """Return the parameter and field names declared by the matched route."""
    names: set[str] = set()
    dependant = getattr(request.scope.get("route"), "dependant", None)
    if dependant is not None:
        _collect_dependant_names(dependant, names, set())
    return names


def _public_location(loc: Any, declared_names: set[str]) -> list[Any]:
    """Return `loc` with segments that may come from request data redacted."""
    public: list[Any] = []
    for index, segment in enumerate(loc):
        if isinstance(segment, int) and not isinstance(segment, bool):
            public.append(segment)
        elif isinstance(segment, str) and (
            (index == 0 and segment in _LOCATION_SOURCES)
            or segment in declared_names
            or segment in _FIXED_LOCATION_SEGMENTS
        ):
            public.append(segment)
        else:
            public.append(_REDACTED_LOCATION_SEGMENT)
    return public


def _public_validation_error(
    error: dict[str, Any], declared_names: set[str]
) -> dict[str, Any]:
    """Convert a raw validation error into a safe public representation.

    The raw `input`, `ctx`, and `msg` are dropped: `msg` can contain submitted
    values or internal exception text. The message is looked up from a fixed
    table by error type instead. Only types in that table are returned; any
    other type (for example one set by a custom error) is replaced with the
    default so arbitrary identifiers are never published. String `loc`
    segments that are not declared by the route are redacted so submitted
    keys are never echoed back.
    """
    error_type = error.get("type")
    if not isinstance(error_type, str) or error_type not in _VALIDATION_MESSAGES:
        error_type = _DEFAULT_VALIDATION_TYPE
    return {
        "loc": _public_location(error.get("loc", ()), declared_names),
        "msg": _VALIDATION_MESSAGES.get(error_type, _DEFAULT_VALIDATION_MESSAGE),
        "type": error_type,
    }


async def validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    declared_names = _declared_names(request)
    details = [
        _public_validation_error(error, declared_names) for error in exc.errors()
    ]
    return error_response(
        422,
        "validation_error",
        "Request validation failed.",
        details,
    )


def register_exception_handlers(app: FastAPI) -> None:
    """Register handlers that return errors in the standard shape."""
    app.add_exception_handler(AppError, app_error_handler)
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.add_exception_handler(RequestValidationError, validation_exception_handler)

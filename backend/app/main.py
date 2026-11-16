from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1 import API_V1_PREFIX, api_router
from app.core.config import Settings, get_settings, sanitize_cors_origins
from app.core.errors import register_exception_handlers
from app.core.request_id import RequestIDMiddleware


def create_app(settings: Settings | None = None) -> FastAPI:
    """Create the FastAPI application."""
    if settings is None:
        settings = get_settings()

    app = FastAPI(title=settings.APP_NAME)

    # Sanitize again here so a wildcard can never be combined with
    # allow_credentials=True, even if the settings list was mutated after
    # construction.
    allowed_origins = sanitize_cors_origins(settings.CORS_ORIGINS)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    # Added last so it is the outermost middleware and every response,
    # including CORS preflight responses, carries the request ID.
    app.add_middleware(RequestIDMiddleware)
    register_exception_handlers(app)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    app.include_router(api_router, prefix=API_V1_PREFIX)

    return app


app = create_app()

from fastapi import APIRouter

API_V1_PREFIX = "/api/v1"

api_router = APIRouter()


@api_router.get("/ping")
def ping() -> dict[str, str]:
    return {"status": "ok"}

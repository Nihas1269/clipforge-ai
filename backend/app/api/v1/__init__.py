from fastapi import APIRouter

from app.api.v1 import auth, clips, jobs, transcripts, users, videos

API_V1_PREFIX = "/api/v1"

api_router = APIRouter()
api_router.include_router(auth.router)
api_router.include_router(users.router)
api_router.include_router(videos.router)
api_router.include_router(transcripts.router)
api_router.include_router(clips.router)
api_router.include_router(jobs.router)


@api_router.get("/ping")
def ping() -> dict[str, str]:
    return {"status": "ok"}

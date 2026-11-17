"""The dashboard summary of the current user's videos, projects and clips.

Every number and list is scoped to the authenticated user.
"""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.api.v1.projects import project_reads
from app.database import get_db
from app.models.user import User
from app.repositories.projects import count_projects_for_user, list_projects_for_user
from app.repositories.videos import (
    count_clips_for_user,
    list_videos_for_user,
    video_totals_for_user,
)
from app.schemas.dashboard import (
    DashboardSummary,
    ProjectTotals,
    UsageSummary,
    VideoTotals,
)
from app.schemas.project import StatusCounts
from app.schemas.video import VideoRead

RECENT_LIMIT = 5

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


@router.get("", response_model=DashboardSummary)
def read_dashboard(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> DashboardSummary:
    totals = video_totals_for_user(db, user.id)
    active, archived = count_projects_for_user(db, user.id)
    projects, _ = list_projects_for_user(db, user.id, limit=RECENT_LIMIT, offset=0)
    videos, _ = list_videos_for_user(db, user.id, limit=RECENT_LIMIT, offset=0)
    return DashboardSummary(
        videos=VideoTotals(
            total=totals["total"], status_counts=StatusCounts(**totals["by_status"])
        ),
        clips=count_clips_for_user(db, user.id),
        projects=ProjectTotals(active=active, archived=archived),
        usage=UsageSummary(
            storage_bytes=totals["size_bytes"],
            video_seconds=totals["duration_seconds"],
        ),
        recent_projects=project_reads(db, user.id, projects),
        recent_videos=[VideoRead.model_validate(v) for v in videos],
    )

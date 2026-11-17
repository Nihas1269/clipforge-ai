from pydantic import BaseModel, Field

from app.schemas.project import ProjectRead, StatusCounts
from app.schemas.video import VideoRead


class VideoTotals(BaseModel):
    total: int = Field(ge=0)
    status_counts: StatusCounts


class UsageSummary(BaseModel):
    """What the user's stored videos add up to.

    Credits are not tracked yet (there is no billing), so only usage is
    reported.
    """

    storage_bytes: int = Field(ge=0)
    video_seconds: float = Field(ge=0)


class ProjectTotals(BaseModel):
    active: int = Field(ge=0)
    archived: int = Field(ge=0)


class DashboardSummary(BaseModel):
    """Everything the dashboard shows above the video list, in one request."""

    videos: VideoTotals
    clips: int = Field(ge=0)
    projects: ProjectTotals
    usage: UsageSummary
    recent_projects: list[ProjectRead]
    recent_videos: list[VideoRead]

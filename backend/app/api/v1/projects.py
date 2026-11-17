"""The current user's projects.

Every endpoint requires authentication and only ever sees the current user's
projects; another user's project gets the same 404 as a missing one. Projects
are archived rather than deleted. A project's videos are listed with
`GET /videos?project_id=...`, and moved with `PATCH /videos/{id}`.
"""

import uuid

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.core.errors import AppError
from app.database import get_db
from app.models.project import Project
from app.models.user import User
from app.repositories.projects import (
    create_project,
    get_project_for_user,
    list_projects_for_user,
    status_counts_by_project,
    update_project,
)
from app.schemas.project import (
    ProjectCreate,
    ProjectList,
    ProjectRead,
    ProjectUpdate,
    StatusCounts,
)

DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100
MAX_OFFSET = 1_000_000

router = APIRouter(prefix="/projects", tags=["projects"])


def project_reads(
    db: Session, user_id: uuid.UUID, projects: list[Project]
) -> list[ProjectRead]:
    """Build the API representation of `projects`, with their video counts."""
    counts = status_counts_by_project(db, user_id, [p.id for p in projects])
    reads: list[ProjectRead] = []
    for project in projects:
        status_counts = StatusCounts(**counts.get(project.id, {}))
        reads.append(
            ProjectRead(
                id=project.id,
                name=project.name,
                archived=project.archived,
                archived_at=project.archived_at,
                created_at=project.created_at,
                updated_at=project.updated_at,
                video_count=sum(status_counts.model_dump().values()),
                status_counts=status_counts,
            )
        )
    return reads


def get_owned_project(
    project_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Project:
    """Return the `{project_id}` project if it belongs to the current user."""
    project = get_project_for_user(db, project_id, user.id)
    if project is None:
        raise AppError(status.HTTP_404_NOT_FOUND, "not_found", "Project not found.")
    return project


@router.get("", response_model=ProjectList)
def list_projects(
    limit: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    offset: int = Query(0, ge=0, le=MAX_OFFSET),
    archived: bool = Query(False),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ProjectList:
    """List the user's active (or, with `archived=true`, archived) projects,
    most recently updated first."""
    projects, total = list_projects_for_user(
        db, user.id, limit=limit, offset=offset, archived=archived
    )
    return ProjectList(items=project_reads(db, user.id, projects), total=total)


@router.post("", status_code=status.HTTP_201_CREATED, response_model=ProjectRead)
def add_project(
    payload: ProjectCreate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ProjectRead:
    project = create_project(db, user.id, payload.name)
    return project_reads(db, user.id, [project])[0]


@router.get("/{project_id}", response_model=ProjectRead)
def read_project(
    project: Project = Depends(get_owned_project),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ProjectRead:
    return project_reads(db, user.id, [project])[0]


@router.patch("/{project_id}", response_model=ProjectRead)
def change_project(
    payload: ProjectUpdate,
    project: Project = Depends(get_owned_project),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ProjectRead:
    """Rename the project and/or archive (`archived: true`) or restore it.

    Archiving keeps the project's videos where they are; an archived project
    accepts no further videos.
    """
    update_project(db, project, name=payload.name, archived=payload.archived)
    return project_reads(db, user.id, [project])[0]

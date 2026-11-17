"""Projects, moving videos between them, project filters and the dashboard."""

import logging
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401  (registers every model on Base.metadata)
from app.core.config import get_settings
from app.database import Base, create_db_engine, create_session_factory, get_db
from app.main import create_app
from app.models.clip import Clip, ClipSource
from app.models.project import Project, ProjectVideo
from app.models.video import Video, VideoStatus

SECRET = "test-only-jwt-signing-secret-0123456789abcdef"
PASSWORD = "s3cure-Passw0rd"
FRONTEND_ORIGIN = "http://localhost:3000"
EVIL_ORIGIN = "http://evil.example"

REGISTER_URL = "/api/v1/auth/register"
LOGIN_URL = "/api/v1/auth/login"
PROJECTS_URL = "/api/v1/projects"
VIDEOS_URL = "/api/v1/videos"
DASHBOARD_URL = "/api/v1/dashboard"


@pytest.fixture
def session_factory():
    engine = create_db_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    yield create_session_factory(engine)
    engine.dispose()


@pytest.fixture
def db(session_factory):
    session = session_factory()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def client(session_factory, tmp_path, monkeypatch):
    monkeypatch.setenv("SECRET_KEY", SECRET)
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("CORS_ORIGINS", FRONTEND_ORIGIN)
    monkeypatch.setenv("STORAGE_BACKEND", "local")
    monkeypatch.setenv("LOCAL_STORAGE_DIR", str(tmp_path / "storage"))
    get_settings.cache_clear()
    application = create_app()

    def override_get_db():
        session = session_factory()
        try:
            yield session
        finally:
            session.close()

    application.dependency_overrides[get_db] = override_get_db
    return TestClient(application)


def _register(client, email) -> uuid.UUID:
    response = client.post(REGISTER_URL, json={"email": email, "password": PASSWORD})
    assert response.status_code == 201
    return uuid.UUID(response.json()["id"])


def _auth(client, email) -> dict[str, str]:
    response = client.post(LOGIN_URL, json={"email": email, "password": PASSWORD})
    assert response.status_code == 200
    client.cookies.clear()
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


@pytest.fixture
def alice(client):
    user_id = _register(client, "alice@example.com")
    return user_id, _auth(client, "alice@example.com")


@pytest.fixture
def bob(client):
    user_id = _register(client, "bob@example.com")
    return user_id, _auth(client, "bob@example.com")


_clock = [datetime(2026, 1, 1, tzinfo=timezone.utc)]


def _add_video(db, user_id, **overrides) -> Video:
    video_id = uuid.uuid4()
    # Strictly increasing creation times keep the newest-first order stable.
    _clock[0] += timedelta(seconds=1)
    values = {
        "id": video_id,
        "user_id": user_id,
        "original_filename": "talk.mp4",
        "content_type": "video/mp4",
        "size_bytes": 1000,
        "storage_key": f"users/{user_id}/videos/{video_id}/source.mp4",
        "status": VideoStatus.READY,
        "duration_seconds": 60.0,
        "created_at": _clock[0],
    }
    values.update(overrides)
    video = Video(**values)
    db.add(video)
    db.commit()
    return video


def _create_project(client, headers, name="Podcast") -> dict:
    response = client.post(PROJECTS_URL, json={"name": name}, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


def _move(client, headers, video_id, project_id):
    return client.patch(
        f"{VIDEOS_URL}/{video_id}",
        json={"project_id": str(project_id) if project_id else None},
        headers=headers,
    )


def _listed_ids(response) -> list[str]:
    assert response.status_code == 200, response.text
    return [item["id"] for item in response.json()["items"]]


# Authentication


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", PROJECTS_URL),
        ("post", PROJECTS_URL),
        ("get", f"{PROJECTS_URL}/{uuid.uuid4()}"),
        ("patch", f"{PROJECTS_URL}/{uuid.uuid4()}"),
        ("patch", f"{VIDEOS_URL}/{uuid.uuid4()}"),
        ("get", DASHBOARD_URL),
    ],
)
def test_endpoints_require_authentication(client, method, path):
    kwargs = {"json": {"name": "x"}} if method in ("post", "patch") else {}

    response = getattr(client, method)(path, **kwargs)

    assert response.status_code == 401


def test_cookie_authenticated_project_change_requires_trusted_origin(client, alice):
    project = _create_project(client, alice[1])
    login = client.post(
        LOGIN_URL, json={"email": "alice@example.com", "password": PASSWORD}
    )
    assert login.status_code == 200

    evil = client.patch(
        f"{PROJECTS_URL}/{project['id']}",
        json={"name": "Hijacked"},
        headers={"Origin": EVIL_ORIGIN},
    )
    trusted = client.patch(
        f"{PROJECTS_URL}/{project['id']}",
        json={"name": "Renamed"},
        headers={"Origin": FRONTEND_ORIGIN},
    )

    assert evil.status_code == 403
    assert trusted.status_code == 200
    assert trusted.json()["name"] == "Renamed"


def test_cookie_authenticated_video_move_requires_trusted_origin(client, db, alice):
    project = _create_project(client, alice[1])
    video = _add_video(db, alice[0])
    login = client.post(
        LOGIN_URL, json={"email": "alice@example.com", "password": PASSWORD}
    )
    assert login.status_code == 200

    evil = client.patch(
        f"{VIDEOS_URL}/{video.id}",
        json={"project_id": project["id"]},
        headers={"Origin": EVIL_ORIGIN},
    )
    assert evil.status_code == 403
    assert db.scalar(select(func.count()).select_from(ProjectVideo)) == 0

    trusted = client.patch(
        f"{VIDEOS_URL}/{video.id}",
        json={"project_id": project["id"]},
        headers={"Origin": FRONTEND_ORIGIN},
    )
    assert trusted.status_code == 200
    assert trusted.json()["project"]["id"] == project["id"]


# Create, rename, archive


def test_create_project(client, db, alice):
    body = _create_project(client, alice[1], "  Weekly podcast  ")

    assert body["name"] == "Weekly podcast"
    assert body["archived"] is False
    assert body["archived_at"] is None
    assert body["video_count"] == 0
    assert body["status_counts"] == {
        "uploaded": 0,
        "processing": 0,
        "ready": 0,
        "failed": 0,
    }
    project = db.scalars(select(Project)).one()
    assert project.user_id == alice[0]


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"name": ""},
        {"name": "   "},
        {"name": None},
        {"name": "x" * 101},
        {"name": "bad\x00name"},
        {"name": "line\nbreak"},
        {"name": "ok", "user_id": str(uuid.uuid4())},
    ],
)
def test_invalid_project_is_rejected(client, db, alice, payload):
    response = client.post(PROJECTS_URL, json=payload, headers=alice[1])

    assert response.status_code == 422
    assert db.scalar(select(func.count()).select_from(Project)) == 0


def test_rename_archive_and_restore_project(client, alice):
    project = _create_project(client, alice[1])
    url = f"{PROJECTS_URL}/{project['id']}"

    renamed = client.patch(url, json={"name": "Interviews"}, headers=alice[1])
    archived = client.patch(url, json={"archived": True}, headers=alice[1])
    active_list = client.get(PROJECTS_URL, headers=alice[1])
    archived_list = client.get(f"{PROJECTS_URL}?archived=true", headers=alice[1])
    restored = client.patch(url, json={"archived": False}, headers=alice[1])

    assert renamed.json()["name"] == "Interviews"
    assert archived.json()["archived"] is True
    assert archived.json()["archived_at"] is not None
    assert _listed_ids(active_list) == []
    assert _listed_ids(archived_list) == [project["id"]]
    assert restored.json()["archived"] is False
    assert restored.json()["archived_at"] is None


@pytest.mark.parametrize(
    "payload", [{"name": None}, {"archived": None}, {"name": ""}, {"user_id": "x"}]
)
def test_invalid_project_update_is_rejected(client, alice, payload):
    project = _create_project(client, alice[1])

    response = client.patch(
        f"{PROJECTS_URL}/{project['id']}", json=payload, headers=alice[1]
    )

    assert response.status_code == 422
    assert (
        client.get(f"{PROJECTS_URL}/{project['id']}", headers=alice[1]).json()["name"]
        == "Podcast"
    )


def test_project_changes_are_logged_without_names(client, alice, caplog):
    with caplog.at_level(logging.INFO):
        project = _create_project(client, alice[1], "Secret launch plans")
        client.patch(
            f"{PROJECTS_URL}/{project['id']}",
            json={"name": "Private rename"},
            headers=alice[1],
        )

    events = [r.getMessage() for r in caplog.records]
    assert "project_created" in events
    assert "project_updated" in events
    assert "Secret launch plans" not in caplog.text
    assert "Private rename" not in caplog.text


def _age_projects(db, ids) -> None:
    """Give the projects distinct update times in the past, oldest first."""
    base = datetime(2025, 1, 1, tzinfo=timezone.utc)
    for index, project_id in enumerate(ids):
        project = db.get(Project, uuid.UUID(project_id))
        project.updated_at = base + timedelta(minutes=index)
    db.commit()


def test_projects_are_paginated_most_recently_updated_first(client, db, alice):
    ids = [_create_project(client, alice[1], f"P{i}")["id"] for i in range(3)]
    _age_projects(db, ids)
    # Renaming the oldest makes it the most recent.
    client.patch(f"{PROJECTS_URL}/{ids[0]}", json={"name": "P0b"}, headers=alice[1])

    first = client.get(f"{PROJECTS_URL}?limit=2", headers=alice[1])
    second = client.get(f"{PROJECTS_URL}?limit=2&offset=2", headers=alice[1])

    assert first.json()["total"] == 3
    assert _listed_ids(first) == [ids[0], ids[2]]
    assert _listed_ids(second) == [ids[1]]


@pytest.mark.parametrize("query", ["limit=0", "limit=101", "offset=-1"])
def test_invalid_project_pagination_is_rejected(client, alice, query):
    assert client.get(f"{PROJECTS_URL}?{query}", headers=alice[1]).status_code == 422


# Ownership


def test_another_users_project_is_not_found(client, db, alice, bob):
    project = _create_project(client, alice[1])
    url = f"{PROJECTS_URL}/{project['id']}"

    read = client.get(url, headers=bob[1])
    rename = client.patch(url, json={"name": "Mine now"}, headers=bob[1])
    archive = client.patch(url, json={"archived": True}, headers=bob[1])
    listing = client.get(PROJECTS_URL, headers=bob[1])
    videos = client.get(f"{VIDEOS_URL}?project_id={project['id']}", headers=bob[1])

    assert read.status_code == 404
    assert rename.status_code == 404
    assert archive.status_code == 404
    assert _listed_ids(listing) == []
    assert videos.status_code == 404
    db.expire_all()
    stored = db.scalars(select(Project)).one()
    assert stored.name == "Podcast"
    assert stored.archived_at is None


def test_video_cannot_be_moved_into_another_users_project(client, db, alice, bob):
    alice_project = _create_project(client, alice[1])
    bob_video = _add_video(db, bob[0])

    response = _move(client, bob[1], bob_video.id, alice_project["id"])

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "project_not_found"
    assert db.scalar(select(func.count()).select_from(ProjectVideo)) == 0


def test_another_users_video_cannot_be_moved(client, db, alice, bob):
    alice_video = _add_video(db, alice[0])
    bob_project = _create_project(client, bob[1])

    response = _move(client, bob[1], alice_video.id, bob_project["id"])

    assert response.status_code == 404
    assert db.scalar(select(func.count()).select_from(ProjectVideo)) == 0


def test_project_counts_only_include_the_owners_videos(client, db, alice, bob):
    project = _create_project(client, alice[1])
    video = _add_video(db, alice[0])
    _move(client, alice[1], video.id, project["id"])
    # A link to another user's video (impossible through the API) is ignored.
    stray = _add_video(db, bob[0])
    db.add(ProjectVideo(video_id=stray.id, project_id=uuid.UUID(project["id"])))
    db.commit()

    body = client.get(f"{PROJECTS_URL}/{project['id']}", headers=alice[1]).json()
    listing = client.get(f"{VIDEOS_URL}?project_id={project['id']}", headers=alice[1])

    assert body["video_count"] == 1
    assert _listed_ids(listing) == [str(video.id)]


# Moving videos


def test_move_video_between_projects_and_unassign(client, db, alice):
    first = _create_project(client, alice[1], "First")
    second = _create_project(client, alice[1], "Second")
    video = _add_video(db, alice[0])
    other = _add_video(db, alice[0])

    def project_videos(project):
        return _listed_ids(
            client.get(f"{VIDEOS_URL}?project_id={project['id']}", headers=alice[1])
        )

    def unassigned():
        return _listed_ids(
            client.get(f"{VIDEOS_URL}?unassigned=true", headers=alice[1])
        )

    assert unassigned() == [str(other.id), str(video.id)]

    moved = _move(client, alice[1], video.id, first["id"])
    assert moved.status_code == 200
    assert moved.json()["project"] == {
        "id": first["id"],
        "name": "First",
        "archived": False,
    }
    assert project_videos(first) == [str(video.id)]
    assert unassigned() == [str(other.id)]

    moved = _move(client, alice[1], video.id, second["id"])
    assert moved.json()["project"]["id"] == second["id"]
    assert project_videos(first) == []
    assert project_videos(second) == [str(video.id)]
    assert db.scalar(select(func.count()).select_from(ProjectVideo)) == 1

    detail = client.get(f"{VIDEOS_URL}/{video.id}", headers=alice[1])
    assert detail.json()["project"]["name"] == "Second"

    unassigned_response = _move(client, alice[1], video.id, None)
    assert unassigned_response.status_code == 200
    assert unassigned_response.json()["project"] is None
    assert project_videos(second) == []
    assert unassigned() == [str(other.id), str(video.id)]


def test_moving_into_the_same_project_is_a_no_op(client, db, alice):
    project = _create_project(client, alice[1])
    video = _add_video(db, alice[0])

    assert _move(client, alice[1], video.id, project["id"]).status_code == 200
    assert _move(client, alice[1], video.id, project["id"]).status_code == 200

    assert db.scalar(select(func.count()).select_from(ProjectVideo)) == 1


def test_video_cannot_be_moved_into_an_archived_project(client, db, alice):
    project = _create_project(client, alice[1])
    client.patch(
        f"{PROJECTS_URL}/{project['id']}", json={"archived": True}, headers=alice[1]
    )
    video = _add_video(db, alice[0])

    response = _move(client, alice[1], video.id, project["id"])

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "project_archived"
    assert db.scalar(select(func.count()).select_from(ProjectVideo)) == 0


def test_archiving_keeps_the_projects_videos(client, db, alice):
    project = _create_project(client, alice[1])
    video = _add_video(db, alice[0])
    _move(client, alice[1], video.id, project["id"])

    client.patch(
        f"{PROJECTS_URL}/{project['id']}", json={"archived": True}, headers=alice[1]
    )

    listing = client.get(f"{VIDEOS_URL}?project_id={project['id']}", headers=alice[1])
    assert _listed_ids(listing) == [str(video.id)]
    assert listing.json()["items"][0]["project"]["archived"] is True


@pytest.mark.parametrize(
    "payload",
    [{}, {"project_id": "not-a-uuid"}, {"project_id": None, "status": "ready"}],
)
def test_invalid_video_update_is_rejected(client, db, alice, payload):
    video = _add_video(db, alice[0])

    response = client.patch(f"{VIDEOS_URL}/{video.id}", json=payload, headers=alice[1])

    assert response.status_code == 422


def test_unknown_project_is_not_found(client, db, alice):
    video = _add_video(db, alice[0])

    assert _move(client, alice[1], video.id, uuid.uuid4()).status_code == 404
    listing = client.get(f"{VIDEOS_URL}?project_id={uuid.uuid4()}", headers=alice[1])
    assert listing.status_code == 404


def test_project_and_unassigned_filters_exclude_each_other(client, alice):
    project = _create_project(client, alice[1])

    response = client.get(
        f"{VIDEOS_URL}?project_id={project['id']}&unassigned=true", headers=alice[1]
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_filter"


def test_project_and_status_filters_combine(client, db, alice):
    project = _create_project(client, alice[1])
    ready = _add_video(db, alice[0], status=VideoStatus.READY)
    processing = _add_video(db, alice[0], status=VideoStatus.PROCESSING)
    _add_video(db, alice[0], status=VideoStatus.PROCESSING)
    for video in (ready, processing):
        _move(client, alice[1], video.id, project["id"])

    response = client.get(
        f"{VIDEOS_URL}?project_id={project['id']}&status=processing", headers=alice[1]
    )

    assert _listed_ids(response) == [str(processing.id)]
    assert response.json()["total"] == 1
    counts = client.get(f"{PROJECTS_URL}/{project['id']}", headers=alice[1]).json()
    assert counts["video_count"] == 2
    assert counts["status_counts"]["ready"] == 1
    assert counts["status_counts"]["processing"] == 1


def test_moving_a_video_makes_the_project_recent(client, db, alice):
    older = _create_project(client, alice[1], "Older")
    newer = _create_project(client, alice[1], "Newer")
    _age_projects(db, [older["id"], newer["id"]])
    assert (
        client.get(PROJECTS_URL, headers=alice[1]).json()["items"][0]["id"]
        == (newer["id"])
    )
    video = _add_video(db, alice[0])

    _move(client, alice[1], video.id, older["id"])

    listing = client.get(PROJECTS_URL, headers=alice[1])
    assert listing.json()["items"][0]["id"] == older["id"]


def test_deleting_a_video_removes_it_from_its_project(client, db, alice):
    project = _create_project(client, alice[1])
    video = _add_video(db, alice[0])
    _move(client, alice[1], video.id, project["id"])

    response = client.delete(f"{VIDEOS_URL}/{video.id}", headers=alice[1])

    assert response.status_code == 204
    assert db.scalar(select(func.count()).select_from(ProjectVideo)) == 0
    body = client.get(f"{PROJECTS_URL}/{project['id']}", headers=alice[1]).json()
    assert body["video_count"] == 0


def test_video_project_change_is_logged_with_ids_only(client, db, alice, caplog):
    project = _create_project(client, alice[1], "Confidential name")
    video = _add_video(db, alice[0], original_filename="private-file.mp4")

    with caplog.at_level(logging.INFO):
        _move(client, alice[1], video.id, project["id"])

    records = [r for r in caplog.records if r.getMessage() == "video_project_changed"]
    assert len(records) == 1
    assert records[0].video_id == str(video.id)
    assert records[0].to_project_id == project["id"]
    assert records[0].from_project_id is None
    assert "Confidential name" not in caplog.text
    assert "private-file" not in caplog.text


# Dashboard


def test_dashboard_summarizes_only_the_users_data(client, db, alice, bob):
    project = _create_project(client, alice[1], "Active")
    archived = _create_project(client, alice[1], "Old")
    client.patch(
        f"{PROJECTS_URL}/{archived['id']}", json={"archived": True}, headers=alice[1]
    )
    ready = _add_video(db, alice[0], size_bytes=1000, duration_seconds=60.0)
    _add_video(
        db,
        alice[0],
        status=VideoStatus.PROCESSING,
        size_bytes=500,
        duration_seconds=None,
    )
    failed = _add_video(
        db, alice[0], status=VideoStatus.FAILED, size_bytes=250, duration_seconds=None
    )
    _move(client, alice[1], ready.id, project["id"])
    db.add(
        Clip(
            video_id=ready.id,
            start_seconds=0.0,
            end_seconds=20.0,
            title="Intro",
            reason="r",
            score=0.5,
            source=ClipSource.HEURISTIC,
        )
    )
    # Bob's data never shows up in Alice's summary.
    bob_video = _add_video(db, bob[0], size_bytes=99999, duration_seconds=999.0)
    db.add(
        Clip(
            video_id=bob_video.id,
            start_seconds=0.0,
            end_seconds=20.0,
            title="Bob",
            reason="r",
            score=0.5,
            source=ClipSource.HEURISTIC,
        )
    )
    db.commit()
    _create_project(client, bob[1], "Bob's")

    response = client.get(DASHBOARD_URL, headers=alice[1])

    assert response.status_code == 200
    body = response.json()
    assert body["videos"] == {
        "total": 3,
        "status_counts": {"uploaded": 0, "processing": 1, "ready": 1, "failed": 1},
    }
    assert body["clips"] == 1
    assert body["projects"] == {"active": 1, "archived": 1}
    assert body["usage"] == {"storage_bytes": 1750, "video_seconds": 60.0}
    assert [p["id"] for p in body["recent_projects"]] == [project["id"]]
    assert body["recent_projects"][0]["video_count"] == 1
    assert body["recent_videos"][0]["id"] == str(failed.id)
    assert {v["id"] for v in body["recent_videos"]} <= {
        str(v.id) for v in db.scalars(select(Video).where(Video.user_id == alice[0]))
    }


def test_dashboard_for_a_new_user_is_empty(client, alice):
    body = client.get(DASHBOARD_URL, headers=alice[1]).json()

    assert body["videos"]["total"] == 0
    assert body["clips"] == 0
    assert body["projects"] == {"active": 0, "archived": 0}
    assert body["usage"] == {"storage_bytes": 0, "video_seconds": 0.0}
    assert body["recent_projects"] == []
    assert body["recent_videos"] == []


def test_dashboard_lists_at_most_five_recent_items(client, db, alice):
    for index in range(7):
        _create_project(client, alice[1], f"P{index}")
        _add_video(db, alice[0])

    body = client.get(DASHBOARD_URL, headers=alice[1]).json()

    assert len(body["recent_projects"]) == 5
    assert len(body["recent_videos"]) == 5
    assert body["projects"]["active"] == 7
    assert body["videos"]["total"] == 7

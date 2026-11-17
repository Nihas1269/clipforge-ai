"""Tests for creating, updating, deleting and protecting clips."""

import uuid

import pytest

from app.models.clip import ClipSource, ClipStatus
from app.models.video import VideoStatus
from tests.clips.helpers import (
    DURATION,
    clip_url,
    clips_url,
    create_clip,
    detect_url,
    detection_jobs,
    make_video,
    manual_clip,
    stored_clips,
)

# Manual clips


def test_manual_clip_creation(client, db, alice, video_id):
    response = create_clip(
        client,
        video_id,
        alice["headers"],
        start_seconds=65,
        end_seconds=90.5,
        title="  My\tbest\x00 moment ",
    )

    assert response.status_code == 201
    data = response.json()
    assert data["video_id"] == str(video_id)
    assert (data["start_seconds"], data["end_seconds"]) == (65.0, 90.5)
    assert data["title"] == "My best moment"
    assert data["source"] == "manual"
    assert data["status"] == "accepted"
    assert data["score"] == 1.0
    assert data["reason"] == "Created manually."
    assert data["detector_version"] is None
    [clip] = stored_clips(db, video_id)
    assert str(clip.id) == data["id"]


def test_manual_clip_gets_a_default_title(client, alice, video_id):
    data = manual_clip(client, video_id, alice["headers"], start=75.0, end=95.0)

    assert data["title"] == "Clip at 1:15"


@pytest.mark.parametrize(
    "start, end", [(0.0, 15.0), (30.0, 120.0), (105.0, DURATION), (0, 90)]
)
def test_manual_clip_accepts_boundary_values(client, alice, video_id, start, end):
    response = create_clip(
        client, video_id, alice["headers"], start_seconds=start, end_seconds=end
    )

    assert response.status_code == 201


@pytest.mark.parametrize(
    "body, code",
    [
        ({"start_seconds": -1, "end_seconds": 20}, "validation_error"),
        ({"start_seconds": 110, "end_seconds": 130}, "invalid_clip_bounds"),
        ({"start_seconds": 30, "end_seconds": 30}, "invalid_clip_bounds"),
        ({"start_seconds": 40, "end_seconds": 30}, "invalid_clip_bounds"),
        ({"start_seconds": 0, "end_seconds": 14.9}, "invalid_clip_length"),
        ({"start_seconds": 0, "end_seconds": 90.5}, "invalid_clip_length"),
        ({"start_seconds": "0", "end_seconds": 20}, "validation_error"),
        ({"start_seconds": True, "end_seconds": 20}, "validation_error"),
        ({"end_seconds": 20}, "validation_error"),
        ({"start_seconds": 0, "end_seconds": 20, "score": 1}, "validation_error"),
        ({"start_seconds": 0, "end_seconds": 20, "title": "  "}, "validation_error"),
        (
            {"start_seconds": 0, "end_seconds": 20, "title": "x" * 201},
            "validation_error",
        ),
    ],
)
def test_manual_clip_validation_errors(client, db, alice, video_id, body, code):
    response = client.post(clips_url(video_id), json=body, headers=alice["headers"])

    assert response.status_code == 422
    assert response.json()["error"]["code"] == code
    assert stored_clips(db, video_id) == []


def test_manual_clip_needs_a_ready_video(client, alice, session_factory, storage_dir):
    video_id = make_video(
        session_factory,
        storage_dir,
        alice["id"],
        status=VideoStatus.PROCESSING,
        duration=None,
    )

    response = create_clip(
        client, video_id, alice["headers"], start_seconds=0, end_seconds=20
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "video_not_ready"


# Updating


def test_update_clip_range_and_title(client, db, alice, video_id):
    clip = manual_clip(client, video_id, alice["headers"])

    response = client.patch(
        clip_url(clip["id"]),
        json={"start_seconds": 30, "end_seconds": 120, "title": "Ending"},
        headers=alice["headers"],
    )

    assert response.status_code == 200
    data = response.json()
    assert (data["start_seconds"], data["end_seconds"]) == (30.0, 120.0)
    assert data["title"] == "Ending"
    assert data["source"] == "manual"

    partial = client.patch(
        clip_url(clip["id"]), json={"end_seconds": 60}, headers=alice["headers"]
    )
    assert partial.status_code == 200
    assert (partial.json()["start_seconds"], partial.json()["end_seconds"]) == (
        30.0,
        60.0,
    )


@pytest.mark.parametrize(
    "body, code",
    [
        ({"end_seconds": 121}, "invalid_clip_bounds"),
        ({"start_seconds": 50}, "invalid_clip_bounds"),
        ({"start_seconds": 35}, "invalid_clip_length"),
        ({"end_seconds": 200, "start_seconds": 150}, "invalid_clip_bounds"),
        ({"start_seconds": -0.5}, "validation_error"),
        ({}, "validation_error"),
        ({"title": None}, "validation_error"),
        ({"start_seconds": None}, "validation_error"),
        ({"status": "maybe"}, "validation_error"),
        ({"source": "heuristic"}, "validation_error"),
    ],
)
def test_update_validation_errors(client, db, alice, video_id, body, code):
    clip = manual_clip(client, video_id, alice["headers"], start=10.0, end=40.0)

    response = client.patch(clip_url(clip["id"]), json=body, headers=alice["headers"])

    assert response.status_code == 422
    assert response.json()["error"]["code"] == code
    [stored] = stored_clips(db, video_id)
    assert (stored.start_seconds, stored.end_seconds) == (10.0, 40.0)


def test_status_changes(client, db, alice, transcribed_video_id, eager_jobs):
    headers = alice["headers"]
    client.post(detect_url(transcribed_video_id), headers=headers)
    clip_id = stored_clips(db, transcribed_video_id)[0].id

    def change(status):
        return client.patch(clip_url(clip_id), json={"status": status}, headers=headers)

    assert change("suggested").status_code == 200  # unchanged
    accepted = change("accepted")
    assert accepted.status_code == 200
    assert accepted.json()["status"] == "accepted"
    assert change("accepted").status_code == 200

    back = change("suggested")
    assert back.status_code == 409
    assert back.json()["error"]["code"] == "invalid_status_transition"
    assert back.json()["error"]["details"] == {"allowed": ["rejected"]}

    assert change("rejected").json()["status"] == "rejected"
    assert change("accepted").json()["status"] == "accepted"
    [stored] = [c for c in stored_clips(db, transcribed_video_id) if c.id == clip_id]
    assert stored.status is ClipStatus.ACCEPTED
    assert stored.source is ClipSource.HEURISTIC


def test_manual_clip_cannot_become_a_suggestion(client, alice, video_id):
    clip = manual_clip(client, video_id, alice["headers"])

    response = client.patch(
        clip_url(clip["id"]), json={"status": "suggested"}, headers=alice["headers"]
    )

    assert response.status_code == 409


# Deleting


def test_delete_clip(client, db, alice, video_id):
    clip = manual_clip(client, video_id, alice["headers"])

    response = client.delete(clip_url(clip["id"]), headers=alice["headers"])

    assert response.status_code == 204
    assert stored_clips(db, video_id) == []
    again = client.delete(clip_url(clip["id"]), headers=alice["headers"])
    assert again.status_code == 404


# Access control


def test_endpoints_require_authentication(client, alice, video_id):
    clip = manual_clip(client, video_id, alice["headers"])

    assert client.post(detect_url(video_id)).status_code == 401
    assert client.get(clips_url(video_id)).status_code == 401
    assert (
        client.post(
            clips_url(video_id), json={"start_seconds": 0, "end_seconds": 20}
        ).status_code
        == 401
    )
    assert client.patch(clip_url(clip["id"]), json={"title": "x"}).status_code == 401
    assert client.delete(clip_url(clip["id"])).status_code == 401


def test_cross_user_access_returns_404(
    client, db, alice, bob, transcribed_video_id, published_jobs
):
    clip = manual_clip(client, transcribed_video_id, alice["headers"])
    headers = bob["headers"]

    responses = [
        client.post(detect_url(transcribed_video_id), headers=headers),
        client.get(clips_url(transcribed_video_id), headers=headers),
        client.post(
            clips_url(transcribed_video_id),
            json={"start_seconds": 0, "end_seconds": 20},
            headers=headers,
        ),
        client.patch(
            clip_url(clip["id"]),
            json={"title": "Stolen", "status": "rejected"},
            headers=headers,
        ),
        client.delete(clip_url(clip["id"]), headers=headers),
    ]

    for response in responses:
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "not_found"
    assert detection_jobs(db) == []
    assert published_jobs == []
    [stored] = stored_clips(db, transcribed_video_id)
    assert stored.title == clip["title"]
    assert stored.status is ClipStatus.ACCEPTED


def test_unknown_clip_is_404(client, alice):
    response = client.patch(
        clip_url(uuid.uuid4()), json={"title": "x"}, headers=alice["headers"]
    )

    assert response.status_code == 404

"""Deletion of a video together with its stored file."""

import logging
import uuid
from typing import Any, cast

from sqlalchemy import CursorResult, delete, select
from sqlalchemy.orm import Session

from app.models.clip import Clip
from app.models.processing_job import ProcessingJob
from app.models.storage_deletion import PendingStorageDeletion
from app.models.transcript import Transcript, TranscriptSegment
from app.models.video import Video
from app.repositories.captions import delete_caption_data
from app.services.storage_cleanup import delete_pending_object
from app.storage import StorageBackend

logger = logging.getLogger(__name__)


class VideoNotFoundError(LookupError):
    """The video does not exist (any more) or belongs to another user."""


def delete_video(
    db: Session, storage: StorageBackend, video_id: uuid.UUID, user_id: uuid.UUID
) -> bool:
    """Delete the user's video, its jobs, transcript, clips and stored file.

    In one transaction the video row (only if it belongs to `user_id`), its
    jobs, clips and transcript are deleted and a PendingStorageDeletion for the file is recorded.
    Only after that commit is the file deleted; if that fails the pending
    deletion stays and is retried later, so the database never keeps a row for
    a deleted video and the file is never forgotten.

    Returns whether the file was deleted straight away. Raises
    VideoNotFoundError if no such video belongs to the user, and re-raises
    database errors (after rolling back, so nothing is deleted).
    """
    try:
        # Scoped to the owner again, so a stale or foreign id never deletes
        # anything.
        storage_key = db.scalar(
            select(Video.storage_key).where(
                Video.id == video_id, Video.user_id == user_id
            )
        )
        if storage_key is None:
            raise VideoNotFoundError(str(video_id))
        # The foreign keys also cascade, but deleting the jobs, clips (with
        # their captions) and transcript explicitly
        # keeps databases without enforced foreign keys consistent.
        db.execute(
            delete(ProcessingJob)
            .where(ProcessingJob.video_id == video_id)
            .execution_options(synchronize_session=False)
        )
        delete_caption_data(db, select(Clip.id).where(Clip.video_id == video_id))
        db.execute(
            delete(Clip)
            .where(Clip.video_id == video_id)
            .execution_options(synchronize_session=False)
        )
        transcript_ids = select(Transcript.id).where(Transcript.video_id == video_id)
        db.execute(
            delete(TranscriptSegment)
            .where(TranscriptSegment.transcript_id.in_(transcript_ids))
            .execution_options(synchronize_session=False)
        )
        db.execute(
            delete(Transcript)
            .where(Transcript.video_id == video_id)
            .execution_options(synchronize_session=False)
        )
        # A DELETE returns a CursorResult, which carries the rowcount.
        result = cast(
            CursorResult[Any],
            db.execute(
                delete(Video)
                .where(Video.id == video_id, Video.user_id == user_id)
                .execution_options(synchronize_session=False)
            ),
        )
        if result.rowcount != 1:
            # Deleted concurrently by another request.
            raise VideoNotFoundError(str(video_id))
        deletion = PendingStorageDeletion(storage_key=storage_key, attempts=0)
        db.add(deletion)
        db.commit()
    except BaseException:
        db.rollback()
        raise

    file_deleted = delete_pending_object(db, storage, deletion)
    logger.info(
        "video_deleted",
        extra={
            "user_id": str(user_id),
            "video_id": str(video_id),
            "file_deleted": file_deleted,
        },
    )
    return file_deleted

import logging
import uuid

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models.transcript import Transcript, TranscriptSegment
from app.transcription.base import TranscriptionResult, normalize_language

logger = logging.getLogger(__name__)


def get_transcript_for_video(db: Session, video_id: uuid.UUID) -> Transcript | None:
    """Return the video's transcript, or None if it has none."""
    return db.scalars(
        select(Transcript).where(Transcript.video_id == video_id)
    ).one_or_none()


def replace_transcript(
    db: Session,
    video_id: uuid.UUID,
    result: TranscriptionResult,
    *,
    requested_language: str | None = None,
) -> Transcript:
    """Store `result` as the video's transcript, replacing any earlier one.

    The language, timestamps and word timings are stored exactly as the
    result reports them. Replacing (rather than adding) keeps retries from
    creating duplicates. The change is flushed but not committed; the caller
    owns the session. Raises InvalidLanguageError for an invalid
    `requested_language`.
    """
    requested = normalize_language(requested_language)
    # Delete the segments explicitly too, so databases without enforced
    # foreign keys never keep orphaned segments. The old transcript is gone
    # before its replacement is inserted (video_id is unique).
    old_ids = select(Transcript.id).where(Transcript.video_id == video_id)
    db.execute(
        delete(TranscriptSegment).where(TranscriptSegment.transcript_id.in_(old_ids))
    )
    db.execute(delete(Transcript).where(Transcript.video_id == video_id))

    transcript = Transcript(
        video_id=video_id,
        language=result.language,
        requested_language=requested,
        provider=result.provider,
        model=result.model,
        full_text=result.full_text,
        segments=[
            TranscriptSegment(
                index=index,
                start_seconds=segment.start,
                end_seconds=segment.end,
                text=segment.text,
                words=(
                    None
                    if segment.words is None
                    else [word.to_dict() for word in segment.words]
                ),
            )
            for index, segment in enumerate(result.segments)
        ],
    )
    db.add(transcript)
    db.flush()
    logger.info(
        "transcript_saved",
        extra={
            "video_id": str(video_id),
            "transcript_id": str(transcript.id),
            "provider": result.provider,
            "language": result.language,
            "segment_count": len(result.segments),
        },
    )
    return transcript

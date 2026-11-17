"""Loading and replacing a clip's captions and caption style.

Ownership is checked by the caller (get_owned_clip); these helpers only take
a clip id.
"""

import uuid
from collections.abc import Sequence

from sqlalchemy import Select, delete, select
from sqlalchemy.orm import Session

from app.captions.generate import CaptionLine
from app.models.caption import CaptionStyle, ClipCaption


def list_captions(db: Session, clip_id: uuid.UUID) -> list[ClipCaption]:
    """Return the clip's captions in order."""
    return list(
        db.scalars(
            select(ClipCaption)
            .where(ClipCaption.clip_id == clip_id)
            .order_by(ClipCaption.index)
        )
    )


def replace_captions(
    db: Session, clip_id: uuid.UUID, lines: Sequence[CaptionLine]
) -> list[ClipCaption]:
    """Replace all of the clip's captions with `lines` (in the given order).

    The change is flushed but not committed; the caller owns the transaction.
    """
    db.execute(
        delete(ClipCaption)
        .where(ClipCaption.clip_id == clip_id)
        .execution_options(synchronize_session="fetch")
    )
    captions = [
        ClipCaption(
            clip_id=clip_id,
            index=index,
            start_seconds=line.start,
            end_seconds=line.end,
            text=line.text,
        )
        for index, line in enumerate(lines)
    ]
    db.add_all(captions)
    db.flush()
    return captions


def get_caption_style(db: Session, clip_id: uuid.UUID) -> CaptionStyle | None:
    """Return the clip's stored caption style, or None for the default."""
    return db.scalars(
        select(CaptionStyle).where(CaptionStyle.clip_id == clip_id)
    ).one_or_none()


def delete_caption_data(db: Session, clip_ids: Select[tuple[uuid.UUID]]) -> None:
    """Delete the captions and styles of the clips selected by `clip_ids`.

    `clip_ids` is a SELECT of clip ids. The foreign keys also cascade; this
    keeps databases without enforced foreign keys consistent when clips are
    deleted in bulk. Not committed.
    """
    db.execute(
        delete(ClipCaption)
        .where(ClipCaption.clip_id.in_(clip_ids))
        .execution_options(synchronize_session=False)
    )
    db.execute(
        delete(CaptionStyle)
        .where(CaptionStyle.clip_id.in_(clip_ids))
        .execution_options(synchronize_session=False)
    )

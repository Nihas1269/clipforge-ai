"""Database models.

Import every model here so that `Base.metadata` is complete for Alembic.
"""

from app.models.auth_token import AuthToken, AuthTokenPurpose
from app.models.billing import (
    Payment,
    PaymentStatus,
    Subscription,
    SubscriptionStatus,
    UsageMetric,
    UsageRecord,
)
from app.models.caption import CaptionStyle, ClipCaption
from app.models.clip import Clip, ClipSource, ClipStatus
from app.models.processing_job import JobStatus, JobType, ProcessingJob
from app.models.storage_deletion import PendingStorageDeletion
from app.models.transcript import Transcript, TranscriptSegment
from app.models.user import User
from app.models.video import Video

__all__ = [
    "AuthToken",
    "AuthTokenPurpose",
    "CaptionStyle",
    "Clip",
    "ClipCaption",
    "ClipSource",
    "ClipStatus",
    "JobStatus",
    "JobType",
    "Payment",
    "PaymentStatus",
    "PendingStorageDeletion",
    "ProcessingJob",
    "Subscription",
    "SubscriptionStatus",
    "Transcript",
    "TranscriptSegment",
    "UsageMetric",
    "UsageRecord",
    "User",
    "Video",
]

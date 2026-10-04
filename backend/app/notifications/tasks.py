"""Web Push jobs (#181, #185).

``notifications.web_push`` (queue ``push``, own job slots) pushes one announced message to
the devices of all its recipients. Arguments are IDs only. The devices are read in a short
database session, sent to in parallel without one, and the outcome is stored in a second
short session. Devices whose push service was not reachable or asked to wait are tried
again in a follow-up job for just those devices (at most ``WEB_PUSH_ATTEMPTS`` in total), so
other devices do not get the message twice.
"""

import contextlib
import uuid
from collections.abc import Iterable

from procrastinate import RetryStrategy
from procrastinate.exceptions import AlreadyEnqueued

from app.core.config import get_settings
from app.notifications import push
from app.notifications.webpush import push_breaker, push_client
from app.processing.tasks import get_database
from app.worker import app

WEB_PUSH_ATTEMPTS = 4
# Seconds before the n-th follow-up: 30, 60, 120.
WEB_PUSH_RETRY_BASE_SECONDS = 30


def _lock(message_id: str) -> str:
    return f"web_push:{message_id}"


async def enqueue_web_push(
    user_ids: Iterable[uuid.UUID], message_id: uuid.UUID, mailbox_id: uuid.UUID
) -> None:
    """Queue one push job for the message. Call it after the announcement was committed."""
    if not get_settings().notifications.web_push_available:
        return
    recipients = sorted({str(user_id) for user_id in user_ids})
    if not recipients:
        return
    with contextlib.suppress(AlreadyEnqueued):
        await web_push_job.configure(queueing_lock=_lock(str(message_id))).defer_async(
            message_id=str(message_id),
            mailbox_id=str(mailbox_id),
            user_ids=[str(user_id) for user_id in recipients],
        )


@app.task(
    name="notifications.web_push",
    queue="push",
    # Errors outside the push itself (database); the push services' answers are handled
    # below. A retry may reach a device twice; the notification is replaced (same tag).
    retry=RetryStrategy(max_attempts=3, exponential_wait=5),
)
async def web_push_job(
    message_id: str,
    mailbox_id: str,
    user_ids: list[str] | None = None,
    device_ids: list[str] | None = None,
    attempt: int = 1,
    # Jobs queued before #185 carry a single user.
    user_id: str | None = None,
) -> None:
    settings = get_settings()
    config = settings.notifications
    if not config.web_push_available:
        return
    users = [uuid.UUID(u) for u in (user_ids or ([user_id] if user_id else []))]
    only = [uuid.UUID(device_id) for device_id in device_ids] if device_ids is not None else None
    sessionmaker = get_database().sessionmaker
    async with sessionmaker() as session:
        targets = await push.load_targets(session, users, settings.auth, only=only)
    if not targets:
        return

    results = await push.deliver(
        push_client(),
        push.vapid_of(config),
        targets,
        uuid.UUID(message_id),
        uuid.UUID(mailbox_id),
        config,
        breaker=push_breaker(),
    )

    if results.sent or results.gone:
        async with sessionmaker() as session:
            await push.record_results(session, results)
            await session.commit()
    if results.retry and attempt < WEB_PUSH_ATTEMPTS:
        await web_push_job.configure(
            schedule_in={"seconds": WEB_PUSH_RETRY_BASE_SECONDS * 2 ** (attempt - 1)}
        ).defer_async(
            message_id=message_id,
            mailbox_id=mailbox_id,
            user_ids=[str(u) for u in users],
            device_ids=[str(device_id) for device_id in results.retry],
            attempt=attempt + 1,
        )

"""Web Push jobs (#181).

``notifications.web_push`` (queue ``default``) pushes one announced message to the devices
of one user. Arguments are IDs only. Devices whose push service was not reachable or asked
to wait are tried again in a follow-up job for just those devices (at most
``WEB_PUSH_ATTEMPTS`` in total), so other devices do not get the message twice.
"""

import contextlib
import uuid
from collections.abc import Iterable

from procrastinate import RetryStrategy
from procrastinate.exceptions import AlreadyEnqueued

from app.core.config import get_settings
from app.notifications import push
from app.notifications.webpush import http_client
from app.processing.tasks import get_database
from app.worker import app

WEB_PUSH_ATTEMPTS = 4
# Seconds before the n-th follow-up: 30, 60, 120.
WEB_PUSH_RETRY_BASE_SECONDS = 30


def _lock(user_id: str, message_id: str) -> str:
    return f"web_push:{user_id}:{message_id}"


async def enqueue_web_push(
    user_ids: Iterable[uuid.UUID], message_id: uuid.UUID, mailbox_id: uuid.UUID
) -> None:
    """Queue one push job per user. Call it after the announcement was committed."""
    if not get_settings().notifications.web_push_available:
        return
    for user_id in user_ids:
        with contextlib.suppress(AlreadyEnqueued):
            await web_push_job.configure(
                queueing_lock=_lock(str(user_id), str(message_id))
            ).defer_async(
                user_id=str(user_id), message_id=str(message_id), mailbox_id=str(mailbox_id)
            )


@app.task(
    name="notifications.web_push",
    queue="default",
    # Errors outside the push itself (database); the push services' answers are handled
    # below. A retry may reach a device twice; the notification is replaced (same tag).
    retry=RetryStrategy(max_attempts=3, exponential_wait=5),
)
async def web_push_job(
    user_id: str,
    message_id: str,
    mailbox_id: str,
    device_ids: list[str] | None = None,
    attempt: int = 1,
) -> None:
    settings = get_settings().notifications
    only = [uuid.UUID(device_id) for device_id in device_ids] if device_ids is not None else None
    async with http_client() as http, get_database().sessionmaker() as session:
        retry = await push.send_to_user(
            session,
            http,
            uuid.UUID(user_id),
            uuid.UUID(message_id),
            uuid.UUID(mailbox_id),
            settings,
            only=only,
        )
        await session.commit()
    if retry and attempt < WEB_PUSH_ATTEMPTS:
        await web_push_job.configure(
            schedule_in={"seconds": WEB_PUSH_RETRY_BASE_SECONDS * 2 ** (attempt - 1)}
        ).defer_async(
            user_id=user_id,
            message_id=message_id,
            mailbox_id=mailbox_id,
            device_ids=[str(device_id) for device_id in retry],
            attempt=attempt + 1,
        )

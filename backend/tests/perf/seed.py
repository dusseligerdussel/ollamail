"""Synthetic mailbox with many messages for the performance tests of the lists (#140).

All data is generated (``generate_series``); no real mail content. Rows are inserted with
plain SQL in a few statements, so 100k messages take seconds, not minutes.
"""

from dataclasses import dataclass

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.mail.models import Folder, FolderRole
from app.triage.models import TriageCategory
from tests.triage.conftest import Account, make_account


@dataclass(frozen=True, slots=True)
class LargeMailbox:
    account: Account
    archive: Folder
    messages: int


async def seed_large_mailbox(
    session: AsyncSession, messages: int, account: Account | None = None
) -> LargeMailbox:
    """``account`` (default: a new user with one mailbox) gets ``messages`` messages, 95 % in the inbox (the rest in the
    archive), every fifth unread, 1 % without a received date, 90 % triaged into the
    built-in categories with priorities 1-3. Bodies are a few KB, like real mails."""
    account = account or await make_account(session)
    archive = Folder(
        mailbox_id=account.mailbox.id, remote_id="Archive", name="Archive", role=FolderRole.ARCHIVE
    )
    session.add(archive)
    await session.flush()
    categories = list(
        await session.scalars(
            select(TriageCategory.id)
            .where(TriageCategory.builtin_key.is_not(None))
            .order_by(TriageCategory.position)
        )
    )
    params = {
        "mailbox": account.mailbox.id,
        "inbox": account.inbox.id,
        "archive": archive.id,
        "n": messages,
    }
    await session.execute(
        text(
            """
            INSERT INTO mail_messages (
                id, mailbox_id, remote_ref, message_id_header, subject, sender, "to",
                headers, sent_at, received_at, body_text, body_html, body_main, size, flags,
                has_attachments
            )
            SELECT
                gen_random_uuid(), :mailbox, 'ref-' || g, '<' || g || '@perf.example.org>',
                'Synthetic subject ' || g,
                jsonb_build_object('name', 'Sender ' || (g % 500),
                                   'address', 'sender' || (g % 500) || '@example.org'),
                '[{"name": null, "address": "me@example.org"}]'::jsonb,
                '[["Subject", "Synthetic"]]'::jsonb,
                timestamptz '2026-09-30 12:00+00' - g * interval '5 minutes' - interval '1 minute',
                CASE WHEN g % 100 = 0 THEN NULL
                     ELSE timestamptz '2026-09-30 12:00+00' - g * interval '5 minutes' END,
                repeat(md5(g::text) || ' ', 60),
                '<p>' || repeat(md5((g + 1)::text) || ' ', 120) || '</p>',
                repeat(md5(g::text) || ' ', 8),
                4096,
                CASE WHEN g % 5 = 0 THEN ARRAY[]::text[] ELSE ARRAY['seen'] END,
                g % 10 = 0
            FROM generate_series(1, :n) AS g
            """
        ),
        params,
    )
    await session.execute(
        text(
            """
            INSERT INTO mail_message_folders (message_id, folder_id)
            SELECT id, CASE WHEN substr(remote_ref, 5)::int % 20 = 0
                            THEN CAST(:archive AS uuid) ELSE CAST(:inbox AS uuid) END
            FROM mail_messages WHERE mailbox_id = :mailbox
            """
        ),
        params,
    )
    await session.execute(
        text(
            """
            INSERT INTO triage_results (id, message_id, category_id, priority, source, reason)
            SELECT gen_random_uuid(), id, (CAST(:categories AS uuid[]))[1 + n % :k],
                   1 + (n / 7) % 3, 'llm', 'Synthetic reason.'
            FROM (SELECT id, substr(remote_ref, 5)::int AS n FROM mail_messages
                  WHERE mailbox_id = :mailbox) AS m
            WHERE n % 10 <> 0
            """
        ),
        {**params, "categories": categories, "k": len(categories)},
    )
    for table in ("mail_messages", "mail_message_folders", "triage_results"):
        await session.execute(text(f"ANALYZE {table}"))
    return LargeMailbox(account, archive, messages)

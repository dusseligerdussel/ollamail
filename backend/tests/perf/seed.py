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

_TRIAGE = """
INSERT INTO triage_results (id, message_id, category_id, priority, source, reason)
SELECT gen_random_uuid(), id, (CAST(:categories AS uuid[]))[1 + n % :k],
       1 + (n / 7) % 3, 'llm', 'Synthetic reason.'
FROM (SELECT id, substr(remote_ref, 5)::int AS n FROM mail_messages
      WHERE mailbox_id = :mailbox) AS m
WHERE n % 10 <> 0
"""

# Built-in categories in position order: important, action_required, waiting_for, info,
# newsletter, notification, spam; ``NULL`` for a deleted category.
_SKEWED_TRIAGE = """
INSERT INTO triage_results (id, message_id, category_id, priority, source, reason)
SELECT gen_random_uuid(), id,
       CASE WHEN r < 500 THEN c[5] WHEN r < 750 THEN c[6] WHEN r < 890 THEN c[4]
            WHEN r < 950 THEN c[2] WHEN r < 980 THEN c[3] WHEN r < 984 THEN c[1]
            WHEN r < 985 THEN c[7] END,
       CASE WHEN p < 3 THEN 1 WHEN p < 40 THEN 2 ELSE 3 END, 'llm', 'Synthetic reason.'
FROM (SELECT id, abs(hashtext('r' || n)) % 1000 AS r, abs(hashtext('p' || n)) % 100 AS p,
             CAST(:categories AS uuid[]) AS c
      FROM (SELECT id, substr(remote_ref, 5)::int AS n FROM mail_messages
            WHERE mailbox_id = :mailbox AND substr(remote_ref, 5)::int % 20 <> 0) AS m) AS x
"""


@dataclass(frozen=True, slots=True)
class LargeMailbox:
    account: Account
    archive: Folder
    messages: int


async def seed_large_mailbox(
    session: AsyncSession, messages: int, account: Account | None = None, *, skewed: bool = False
) -> LargeMailbox:
    """``account`` (default: a new user with one mailbox) gets ``messages`` messages, 95 %
    in the inbox (the rest in the archive), every fifth unread, 1 % without a received date,
    90 % triaged into the built-in categories with priorities 1-3. Bodies are a few KB, like
    real mails.

    ``skewed`` (#186): an archive-heavy mailbox as it grows over the years. 30 % in the
    inbox, 0.5 % unread, 95 % triaged with few mails in most categories (half newsletters,
    0.4 % important, 0.1 % spam, 1.5 % in a deleted category) and priority 1 for 3 %, so
    several segments of the triage inbox hold only a handful of messages."""
    account = account or await make_account(session)
    # The rows stay uncommitted (the test rolls back), so autovacuum cannot see them: a
    # VACUUM of these tables (dead rows of earlier tests) would write ``reltuples = 0`` over
    # the statistics below, and the triggers and lists would be planned for empty tables
    # (one scan of a whole folder or index per row: minutes instead of seconds). The lock
    # keeps autovacuum away until the test ends; it skips locked tables.
    await session.execute(
        text(
            "LOCK TABLE mail_messages, mail_message_folders, triage_results"
            " IN SHARE UPDATE EXCLUSIVE MODE"
        )
    )
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
        "skewed": skewed,
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
                CASE WHEN (CASE WHEN :skewed THEN abs(hashtext('u' || g)) % 200
                                ELSE g % 5 END) = 0
                     THEN ARRAY[]::text[] ELSE ARRAY['seen'] END,
                g % 10 = 0
            FROM generate_series(1, :n) AS g
            """
        ),
        params,
    )
    # The triggers of the next inserts look up messages and their folders row by row; plan
    # them for the rows just inserted, not for the statistics of an empty table.
    await session.execute(text("ANALYZE mail_messages"))
    await session.execute(
        text(
            """
            INSERT INTO mail_message_folders (message_id, folder_id)
            SELECT id, CASE WHEN CASE WHEN :skewed THEN substr(remote_ref, 5)::int % 10 >= 3
                                      ELSE substr(remote_ref, 5)::int % 20 = 0 END
                            THEN CAST(:archive AS uuid) ELSE CAST(:inbox AS uuid) END
            FROM mail_messages WHERE mailbox_id = :mailbox
            """
        ),
        params,
    )
    await session.execute(text("ANALYZE mail_message_folders"))
    await session.execute(
        text(_SKEWED_TRIAGE if skewed else _TRIAGE),
        {**params, "categories": categories, "k": len(categories)},
    )
    await session.execute(text("ANALYZE triage_results"))
    return LargeMailbox(account, archive, messages)

"""Which mailboxes a user may search: the single place for this rule.

Access is enforced in SQL (docs/PRIVACY.md: RAG access strictly via SQL filter, never
in a prompt or in Python after the fact): every search query restricts chunks with
``mailbox_id IN (readable_mailbox_ids(user_id))``.

Today a user reads the mailboxes they own. Shared mailboxes (``owner_user_id IS NULL``)
are readable by nobody until their assignment to users and groups exists (#34); that
rule belongs here and nowhere else.
"""

import uuid

from sqlalchemy import Select, select

from app.mail.models import Mailbox


def readable_mailbox_ids(user_id: uuid.UUID) -> Select[uuid.UUID]:
    """Subquery of the IDs of all mailboxes ``user_id`` may read."""
    return select(Mailbox.id).where(Mailbox.owner_user_id == user_id)

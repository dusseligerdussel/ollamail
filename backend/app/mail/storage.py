"""Attachment files in the data directory.

Layout: ``<data_dir>/attachments/<mailbox_id>/<attachment_id>``. File names contain only
IDs, never the original filename (personal data), and one directory per mailbox makes
deleting a mailbox's files a single tree removal.
"""

import os
import shutil
import tempfile
import uuid
from pathlib import Path

ATTACHMENTS_DIR = "attachments"


class AttachmentStorage:
    def __init__(self, data_dir: Path) -> None:
        self.root = data_dir.resolve()

    def _resolve(self, relative: str) -> Path:
        path = (self.root / relative).resolve()
        if not path.is_relative_to(self.root / ATTACHMENTS_DIR):
            raise ValueError("path outside the attachment directory")
        return path

    def mailbox_dir(self, mailbox_id: uuid.UUID) -> Path:
        return self.root / ATTACHMENTS_DIR / str(mailbox_id)

    def write(self, mailbox_id: uuid.UUID, attachment_id: uuid.UUID, data: bytes) -> str:
        """Store ``data`` atomically; returns the path relative to the data directory."""
        relative = f"{ATTACHMENTS_DIR}/{mailbox_id}/{attachment_id}"
        path = self._resolve(relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        return relative

    def read(self, relative: str) -> bytes:
        return self._resolve(relative).read_bytes()

    def exists(self, relative: str) -> bool:
        return self._resolve(relative).is_file()

    def delete(self, relative: str) -> None:
        self._resolve(relative).unlink(missing_ok=True)

    def delete_mailbox(self, mailbox_id: uuid.UUID) -> None:
        shutil.rmtree(self.mailbox_dir(mailbox_id), ignore_errors=True)

"""Digest audio files in the data directory.

Layout: ``<data_dir>/digests/<user_id>/<digest_id>.<ext>`` (``mp3``, ``opus``). Names
contain IDs only, and one directory per user makes deleting a user's files a single tree
removal. Paths stored in the database are relative to the data directory and resolved
here, so a stored path can never point outside the digest directory.
"""

import shutil
import time
import uuid
from pathlib import Path

DIGESTS_DIR = "digests"
# Files younger than this are never treated as orphans (a digest may be in progress).
ORPHAN_MIN_AGE_SECONDS = 6 * 3600


class DigestStorage:
    def __init__(self, data_dir: Path) -> None:
        self.root = data_dir.resolve()
        self.base = self.root / DIGESTS_DIR

    def target(self, user_id: uuid.UUID, digest_id: uuid.UUID) -> Path:
        """Path without extension, as expected by ``TTSService.synthesize``."""
        return self.base / str(user_id) / str(digest_id)

    def relative(self, path: Path) -> str:
        return path.resolve().relative_to(self.root).as_posix()

    def resolve(self, relative: str) -> Path:
        path = (self.root / relative).resolve()
        if not path.is_relative_to(self.base):
            raise ValueError("path outside the digest directory")
        return path

    def delete(self, relative: str) -> None:
        self.resolve(relative).unlink(missing_ok=True)

    def delete_digest(self, user_id: uuid.UUID, digest_id: uuid.UUID) -> None:
        """All files of a digest, including leftovers of an interrupted encoding."""
        directory = self.base / str(user_id)
        if directory.is_dir():
            for path in directory.glob(f"{digest_id}.*"):
                path.unlink(missing_ok=True)

    def delete_user(self, user_id: uuid.UUID) -> None:
        shutil.rmtree(self.base / str(user_id), ignore_errors=True)

    def user_ids(self) -> list[uuid.UUID]:
        if not self.base.is_dir():
            return []
        found = []
        for path in self.base.iterdir():
            try:
                found.append(uuid.UUID(path.name))
            except ValueError:
                continue
        return found

    def orphans(self, user_id: uuid.UUID, known: set[uuid.UUID]) -> list[Path]:
        """Files of ``user_id`` that belong to no known digest and are old enough."""
        directory = self.base / str(user_id)
        if not directory.is_dir():
            return []
        cutoff = time.time() - ORPHAN_MIN_AGE_SECONDS
        found = []
        for path in directory.iterdir():
            try:
                digest_id = uuid.UUID(path.name.split(".", 1)[0])
            except ValueError:
                digest_id = None
            if digest_id not in known and path.stat().st_mtime < cutoff:
                found.append(path)
        return found

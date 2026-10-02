"""Export files in the data directory.

Layout: ``<data_dir>/exports/<user_id>/<export_id>.zip``. Names contain IDs only, and one
directory per user makes deleting a user's exports a single tree removal. Stored paths are
relative to the data directory and resolved here, so they can never point elsewhere.
"""

import shutil
import uuid
from pathlib import Path

EXPORTS_DIR = "exports"


class ExportStorage:
    def __init__(self, data_dir: Path) -> None:
        self.root = data_dir.resolve()
        self.base = self.root / EXPORTS_DIR

    def path(self, user_id: uuid.UUID, export_id: uuid.UUID) -> Path:
        return self.base / str(user_id) / f"{export_id}.zip"

    def relative(self, path: Path) -> str:
        return path.resolve().relative_to(self.root).as_posix()

    def resolve(self, relative: str) -> Path:
        path = (self.root / relative).resolve()
        if not path.is_relative_to(self.base):
            raise ValueError("path outside the export directory")
        return path

    def delete_export(self, user_id: uuid.UUID, export_id: uuid.UUID) -> None:
        """The file and leftovers of an interrupted run (``<export_id>.zip.tmp-*``)."""
        directory = self.base / str(user_id)
        if directory.is_dir():
            for path in directory.glob(f"{export_id}.zip*"):
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

    def export_ids(self, user_id: uuid.UUID) -> set[uuid.UUID]:
        directory = self.base / str(user_id)
        if not directory.is_dir():
            return set()
        found = set()
        for path in directory.iterdir():
            try:
                found.add(uuid.UUID(path.name.split(".", 1)[0]))
            except ValueError:
                continue
        return found

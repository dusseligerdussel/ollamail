import uuid

import pytest

from app.mail.storage import AttachmentStorage


def test_write_read_delete(storage: AttachmentStorage) -> None:
    mailbox_id, attachment_id = uuid.uuid4(), uuid.uuid4()

    path = storage.write(mailbox_id, attachment_id, b"data")

    assert path == f"attachments/{mailbox_id}/{attachment_id}"
    assert storage.read(path) == b"data"
    storage.delete(path)
    assert not storage.exists(path)
    storage.delete(path)  # idempotent


def test_delete_mailbox_removes_all_files(storage: AttachmentStorage) -> None:
    mailbox_id, other = uuid.uuid4(), uuid.uuid4()
    paths = [storage.write(mailbox_id, uuid.uuid4(), b"x") for _ in range(3)]
    kept = storage.write(other, uuid.uuid4(), b"y")

    storage.delete_mailbox(mailbox_id)

    assert not any(storage.exists(p) for p in paths)
    assert not storage.mailbox_dir(mailbox_id).exists()
    assert storage.exists(kept)


@pytest.mark.parametrize("path", ["../outside", "attachments/../../etc/passwd", "/etc/passwd"])
def test_paths_outside_the_attachment_directory_are_rejected(
    storage: AttachmentStorage, path: str
) -> None:
    with pytest.raises(ValueError):
        storage.read(path)

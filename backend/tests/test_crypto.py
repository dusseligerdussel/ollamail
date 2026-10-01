import base64
import io
import json
from collections.abc import Iterator

import pytest
from pydantic import SecretStr
from sqlalchemy import Column, Integer, MetaData, Table, Text, select, type_coerce
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import DatabaseSettings, LoggingSettings, SecuritySettings, Settings
from app.core.crypto import (
    DecryptionError,
    EncryptedJSON,
    EncryptedStr,
    KeyRing,
    SecretKeyError,
    configure_keyring,
    decode_key,
    generate_key,
    rotate_keys,
    set_keyring,
)
from app.core.logging import configure_logging
from app.main import create_app
from tests.conftest import TEST_DATABASE_URL

OLD_KEY = generate_key()
NEW_KEY = generate_key()


def keyring(current: str, *old: str) -> KeyRing:
    return KeyRing(decode_key(current), [decode_key(key) for key in old])


def flip(token: str, index: int) -> str:
    raw = bytearray(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)))
    raw[index] ^= 0x01
    return base64.urlsafe_b64encode(bytes(raw)).rstrip(b"=").decode()


@pytest.fixture(autouse=True)
def _reset_keyring() -> Iterator[None]:
    yield
    set_keyring(None)


# --- keys ---------------------------------------------------------------------------


def test_generated_key_is_valid() -> None:
    key = generate_key()

    assert len(decode_key(key)) == 32
    assert generate_key() != key


def test_url_safe_key_is_accepted() -> None:
    raw = bytes(range(32))

    assert decode_key(base64.urlsafe_b64encode(raw).decode()) == raw


@pytest.mark.parametrize(
    "value",
    [
        "",
        "   ",
        "not base64 at all!",
        base64.b64encode(b"x" * 16).decode(),  # too short
        base64.b64encode(bytes(32)).decode(),  # all zeros
        base64.b64encode(b"ab" * 16).decode(),  # low entropy
        "change-me",
    ],
)
def test_missing_or_weak_key_is_rejected(value: str) -> None:
    with pytest.raises(SecretKeyError):
        decode_key(value)


def test_error_message_does_not_contain_key() -> None:
    weak = base64.b64encode(b"hunter2!" * 2).decode()

    with pytest.raises(SecretKeyError) as info:
        decode_key(weak)

    assert weak not in str(info.value)


def test_keyring_from_settings() -> None:
    settings = SecuritySettings(secret_key=SecretStr(NEW_KEY), secret_keys_old=[SecretStr(OLD_KEY)])

    ring = KeyRing.from_settings(settings)

    assert ring.decrypt(keyring(OLD_KEY).encrypt(b"x")) == b"x"
    assert NEW_KEY not in repr(ring)


def test_keyring_requires_secret_key() -> None:
    with pytest.raises(SecretKeyError):
        KeyRing.from_settings(SecuritySettings())


def test_old_keys_are_read_comma_separated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OLLAMAIL_SECRET_KEY", NEW_KEY)
    monkeypatch.setenv("OLLAMAIL_SECRET_KEYS_OLD", f"{OLD_KEY}, {generate_key()}")

    settings = SecuritySettings()

    assert len(settings.secret_keys_old) == 2
    assert settings.secret_keys_old[0].get_secret_value() == OLD_KEY
    assert OLD_KEY not in repr(settings)


def test_old_keys_default_to_empty() -> None:
    assert SecuritySettings().secret_keys_old == []


# --- encryption ---------------------------------------------------------------------


@pytest.mark.parametrize("plaintext", [b"", b"hunter2", "pässwört ✓".encode(), b"x" * 10_000])
def test_round_trip(plaintext: bytes) -> None:
    ring = keyring(NEW_KEY)

    token = ring.encrypt(plaintext)

    assert ring.decrypt(token) == plaintext
    assert ring.key_id(token) == ring.current_key_id


def test_each_encryption_is_randomised() -> None:
    ring = keyring(NEW_KEY)

    assert ring.encrypt(b"same") != ring.encrypt(b"same")


def test_token_does_not_contain_plaintext() -> None:
    token = keyring(NEW_KEY).encrypt(b"hunter2hunter2")

    assert b"hunter2" not in base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))


@pytest.mark.parametrize("index", [0, 1, 5, 20, 40, 70, -1])
def test_tampering_is_detected(index: int) -> None:
    ring = keyring(NEW_KEY)
    token = ring.encrypt(b"hunter2")

    with pytest.raises(DecryptionError):
        ring.decrypt(flip(token, index))


@pytest.mark.parametrize("token", ["", "AAAA", "not*base64", "A" * 200])
def test_malformed_token_is_rejected(token: str) -> None:
    with pytest.raises(DecryptionError):
        keyring(NEW_KEY).decrypt(token)


def test_swapping_encrypted_values_between_tokens_is_detected() -> None:
    ring = keyring(NEW_KEY)
    first = base64.urlsafe_b64decode(ring.encrypt(b"first") + "==")
    second = base64.urlsafe_b64decode(ring.encrypt(b"other") + "==")
    data_offset = 1 + 4 + 12 + 48
    mixed = base64.urlsafe_b64encode(first[:data_offset] + second[data_offset:]).decode()

    with pytest.raises(DecryptionError):
        ring.decrypt(mixed.rstrip("="))


def test_unknown_master_key_is_rejected() -> None:
    token = keyring(OLD_KEY).encrypt(b"hunter2")

    with pytest.raises(DecryptionError):
        keyring(NEW_KEY).decrypt(token)


# --- rotation -----------------------------------------------------------------------


def test_rotation_keeps_values_readable() -> None:
    old_ring = keyring(OLD_KEY)
    tokens = [old_ring.encrypt(value) for value in (b"a", b"b" * 100, b"")]
    ring = keyring(NEW_KEY, OLD_KEY)

    assert all(ring.needs_rotation(token) for token in tokens)
    rotated = [ring.rewrap(token) for token in tokens]

    assert not any(ring.needs_rotation(token) for token in rotated)
    assert [ring.decrypt(token) for token in rotated] == [b"a", b"b" * 100, b""]
    # Afterwards the old key is no longer needed.
    assert [keyring(NEW_KEY).decrypt(token) for token in rotated] == [b"a", b"b" * 100, b""]
    with pytest.raises(DecryptionError):
        old_ring.decrypt(rotated[0])


def test_rewrap_detects_tampered_data_key() -> None:
    token = keyring(OLD_KEY).encrypt(b"hunter2")

    with pytest.raises(DecryptionError):
        keyring(NEW_KEY, OLD_KEY).rewrap(flip(token, 30))


def test_rewrap_with_unknown_key_fails() -> None:
    token = keyring(OLD_KEY).encrypt(b"hunter2")

    with pytest.raises(DecryptionError):
        keyring(NEW_KEY).rewrap(token)


# --- SQLAlchemy types ---------------------------------------------------------------


def test_column_types_round_trip_through_bind_and_result() -> None:
    set_keyring(keyring(NEW_KEY))
    dialect = None  # not used by the types
    value = {"access_token": "t", "expires_in": 3600, "scopes": ["mail"]}

    stored_str = EncryptedStr().process_bind_param("hunter2", dialect)  # type: ignore[arg-type]
    stored_json = EncryptedJSON().process_bind_param(value, dialect)  # type: ignore[arg-type]

    assert stored_str is not None
    assert stored_json is not None
    assert "hunter2" not in stored_str
    assert "access_token" not in stored_json
    assert EncryptedStr().process_result_value(stored_str, dialect) == "hunter2"  # type: ignore[arg-type]
    assert EncryptedJSON().process_result_value(stored_json, dialect) == value  # type: ignore[arg-type]
    assert EncryptedStr().process_bind_param(None, dialect) is None  # type: ignore[arg-type]
    assert EncryptedJSON().process_result_value(None, dialect) is None  # type: ignore[arg-type]


# --- startup ------------------------------------------------------------------------


async def test_app_refuses_to_start_without_secret_key(settings_without_key: Settings) -> None:
    app = create_app(settings_without_key)

    with pytest.raises(SecretKeyError):
        async with app.router.lifespan_context(app):
            pass
    await app.state.database.dispose()


async def test_app_starts_with_valid_secret_key(settings_without_key: Settings) -> None:
    settings = settings_without_key.model_copy(
        update={"security": SecuritySettings(secret_key=SecretStr(NEW_KEY))}
    )
    app = create_app(settings)

    async with app.router.lifespan_context(app):
        pass


def test_invalid_key_is_logged_without_key_material() -> None:
    stream = io.StringIO()
    configure_logging(LoggingSettings(), stream=stream)
    weak = base64.b64encode(b"short").decode()

    with pytest.raises(SecretKeyError):
        configure_keyring(SecuritySettings(secret_key=SecretStr(weak)))

    events = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert [event["event"] for event in events] == ["secret_key_invalid"]
    assert "OLLAMAIL_SECRET_KEY" in events[0]["reason"]
    assert weak not in stream.getvalue()


@pytest.fixture
def settings_without_key() -> Settings:
    return Settings(database=DatabaseSettings.model_validate({"url": TEST_DATABASE_URL}))


# --- rotate_keys against PostgreSQL ------------------------------------------------


@pytest.mark.db
async def test_rotate_keys_re_encrypts_all_columns() -> None:
    metadata = MetaData()
    table = Table(
        "crypto_rotation_test",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("password", EncryptedStr),
        Column("token", EncryptedJSON),
        Column("note", Text),
        prefixes=["TEMPORARY"],
    )
    engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            await connection.run_sync(metadata.create_all)
            set_keyring(keyring(OLD_KEY))
            await connection.execute(
                table.insert(),
                [
                    {"id": 1, "password": "pw1", "token": {"a": 1}, "note": "n"},
                    {"id": 2, "password": None, "token": ["x"], "note": None},
                ],
            )
            new_ring = keyring(NEW_KEY, OLD_KEY)
            set_keyring(new_ring)
            # A value already encrypted with the current key stays as it is.
            await connection.execute(table.insert(), [{"id": 3, "password": "pw3"}])

            result = await rotate_keys(connection, new_ring, metadata)

            assert (result.tables, result.checked, result.rotated) == (1, 4, 3)
            set_keyring(keyring(NEW_KEY))
            rows = (await connection.execute(select(table).order_by(table.c.id))).all()
            assert [(r.password, r.token, r.note) for r in rows] == [
                ("pw1", {"a": 1}, "n"),
                (None, ["x"], None),
                ("pw3", None, None),
            ]
            raw = await connection.execute(select(type_coerce(table.c.password, Text)))
            assert all(token is None or not new_ring.needs_rotation(token) for (token,) in raw)
            again = await rotate_keys(connection, new_ring, metadata)
            assert again.rotated == 0
            await connection.rollback()
    finally:
        await engine.dispose()

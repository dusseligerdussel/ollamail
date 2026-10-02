from datetime import UTC, datetime, timedelta

import pyotp

from app.auth.mfa import recovery, totp
from app.core.config import SecuritySettings
from app.core.crypto import generate_key

NOW = datetime(2026, 10, 2, 12, 0, 10, tzinfo=UTC)


def test_codes_of_the_neighbouring_steps_are_accepted() -> None:
    secret = totp.new_secret()
    otp = pyotp.TOTP(secret)
    current = otp.timecode(NOW)

    assert totp.matching_step(secret, otp.at(NOW), NOW) == current
    assert totp.matching_step(secret, otp.at(NOW - timedelta(seconds=30)), NOW) == current - 1
    assert totp.matching_step(secret, otp.at(NOW + timedelta(seconds=30)), NOW) == current + 1


def test_codes_outside_the_window_are_rejected() -> None:
    secret = totp.new_secret()
    otp = pyotp.TOTP(secret)

    assert totp.matching_step(secret, otp.at(NOW - timedelta(seconds=60)), NOW) is None
    assert totp.matching_step(secret, otp.at(NOW + timedelta(seconds=60)), NOW) is None


def test_malformed_codes_are_rejected_and_spaces_ignored() -> None:
    secret = totp.new_secret()
    code = pyotp.TOTP(secret).at(NOW)

    assert totp.matching_step(secret, f"{code[:3]} {code[3:]}", NOW) is not None
    assert totp.matching_step(secret, "12345", NOW) is None
    assert totp.matching_step(secret, "abcdef", NOW) is None
    assert totp.matching_step(secret, "", NOW) is None


def test_secret_and_uri_follow_rfc_6238_defaults() -> None:
    secret = totp.new_secret()
    uri = totp.provisioning_uri(secret, "erika@example.org")

    assert len(secret) == 32
    assert uri.startswith("otpauth://totp/ollamail:erika%40example.org?")
    assert f"secret={secret}" in uri
    assert "issuer=ollamail" in uri
    assert totp.qr_svg(uri).startswith("data:image/svg+xml")


def test_recovery_codes_are_random_and_hashed_with_a_key() -> None:
    codes = recovery.generate()
    first = SecuritySettings.model_validate({"secret_key": generate_key()})
    second = SecuritySettings.model_validate({"secret_key": generate_key()})

    assert len(codes) == len(set(codes)) == recovery.COUNT
    assert all(recovery.looks_like_code(code) for code in codes)
    code = codes[0]
    assert recovery.code_hash(first, code) == recovery.code_hash(first, code.upper())
    assert recovery.code_hash(first, code) == recovery.code_hash(first, code.replace("-", " "))
    assert recovery.code_hash(first, code) != recovery.code_hash(second, code)
    assert code.encode() not in recovery.code_hash(first, code)
    assert not recovery.looks_like_code("123456")

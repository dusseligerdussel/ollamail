"""TOTP (RFC 6238) with ``pyotp``: 6 digits, 30-second steps, HMAC-SHA1.

These are the parameters every authenticator app supports. A code is accepted for the
current step and one step before or after (clock drift), and every step only once.
The QR code is rendered locally as SVG (``segno``); the secret never leaves the instance
except to the user's own browser.
"""

import hmac
from datetime import datetime

import pyotp
import segno

ISSUER = "ollamail"
DIGITS = 6
STEP_SECONDS = 30
# Steps accepted before and after the current one.
VALID_WINDOW = 1


def new_secret() -> str:
    """A random base32 secret (160 bits, as RFC 4226 recommends)."""
    return pyotp.random_base32(length=32)


def provisioning_uri(secret: str, account: str) -> str:
    return pyotp.TOTP(secret, digits=DIGITS, interval=STEP_SECONDS).provisioning_uri(
        name=account, issuer_name=ISSUER
    )


def qr_svg(uri: str) -> str:
    """The URI as QR code: an SVG data URI, dark modules on white (also in dark mode)."""
    qr = segno.make(uri, error="m")
    return qr.svg_data_uri(scale=4, border=2, dark="#000", light="#fff")


def normalize(code: str) -> str:
    return "".join(code.split())


def matching_step(secret: str, code: str, now: datetime) -> int | None:
    """The time step ``code`` belongs to (within the window), or ``None``."""
    code = normalize(code)
    if len(code) != DIGITS or not code.isdigit():
        return None
    totp = pyotp.TOTP(secret, digits=DIGITS, interval=STEP_SECONDS)
    current = totp.timecode(now)
    match = None
    # Compare against every candidate (no early exit) to keep the timing uniform.
    for step in range(current - VALID_WINDOW, current + VALID_WINDOW + 1):
        if hmac.compare_digest(totp.generate_otp(step), code):
            match = step
    return match

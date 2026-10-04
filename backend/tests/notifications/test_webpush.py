"""Web Push building blocks without a database (#181, #185): keys, encryption, VAPID,
sending, circuit breaker and shared client."""

import json
import time

import httpx
import pytest
import respx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from pydantic import ValidationError

from app.core.config import NotificationsSettings
from app.notifications.push import describe_device
from app.notifications.vapid import (
    Vapid,
    VapidKeyError,
    b64url_decode,
    b64url_encode,
    check_key_pair,
    check_subscription_keys,
    encrypt,
    generate_key_pair,
    private_key_from_raw,
    public_key_from_bytes,
)
from app.notifications.webpush import (
    PushOutcome,
    PushServiceBreaker,
    close_push_client,
    endpoint_allowed,
    push_client,
    send_push,
)
from tests.notifications.webpush import FCM, Browser, push_settings

HOSTS = NotificationsSettings().web_push_allowed_hosts


def test_encryption_matches_the_example_of_rfc_8291() -> None:
    body = encrypt(
        b"When I grow up, I want to be a watermelon",
        "BCVxsr7N_eNgVRqvHtD0zTZsEc6-VV-JvLexhqUzORcxaOzi6-AYWXvTBHm4bjyPjs7Vd8pZGH6SRpkNtoIAiw4",
        "BTBZMqHH6r4Tts7J_aSIgg",
        sender_key=private_key_from_raw("yfWPiYE-n46HLnH0KqZOF1fJJU3MYrct3AELtAQ-oRw"),
        salt=b64url_decode("DGv6ra1nlYgDCS1FRnbzlw"),
    )
    assert b64url_encode(body) == (
        "DGv6ra1nlYgDCS1FRnbzlwAAEABBBP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27mlmlMoZIIgDll6e3vCYLocInmY"
        "WAmS6TlzAC8wEqKK6PBru3jl7A_yl95bQpu6cVPTpK4Mqgkf1CXztLVBSt2Ks3oZwbuwXPXLWyouBWLVWGNWQexSg"
        "Sxsj_Qulcy4a-fN"
    )


def test_each_message_is_encrypted_with_fresh_keys() -> None:
    browser = Browser(FCM + "abc")
    first = encrypt(b'{"message_id":"x"}', browser.p256dh, browser.auth)
    second = encrypt(b'{"message_id":"x"}', browser.p256dh, browser.auth)
    assert first != second
    assert browser.decrypt(first) == browser.decrypt(second) == b'{"message_id":"x"}'


def test_key_pair_checks() -> None:
    public, private = generate_key_pair()
    check_key_pair(public, private)
    other_public, _ = generate_key_pair()
    with pytest.raises(VapidKeyError, match="does not belong"):
        check_key_pair(other_public, private)
    for bad_public, bad_private in (
        (public[:-4], private),
        (public, private[:-4]),
        ("%%", private),
    ):
        with pytest.raises(VapidKeyError) as error:
            check_key_pair(bad_public, bad_private)
        # Errors never contain key material.
        assert private not in str(error.value)


def test_subscription_key_checks() -> None:
    browser = Browser(FCM + "abc")
    check_subscription_keys(browser.p256dh, browser.auth)
    with pytest.raises(VapidKeyError):
        check_subscription_keys(browser.p256dh, b64url_encode(b"short"))
    with pytest.raises(VapidKeyError):
        # 65 bytes, but not a point on the curve.
        check_subscription_keys(b64url_encode(b"\x04" + b"\x01" * 64), browser.auth)


def test_vapid_token_is_a_valid_es256_jwt() -> None:
    public, private = generate_key_pair()
    vapid = Vapid.from_keys(public, private, "mailto:admin@example.org")
    now = time.time()
    header = vapid.authorization("https://fcm.googleapis.com", now=now)
    assert header.startswith("vapid t=") and header.endswith(f", k={public}")
    token = header.removeprefix("vapid t=").split(",")[0]
    encoded_header, encoded_claims, signature = token.split(".")
    assert json.loads(b64url_decode(encoded_header)) == {"typ": "JWT", "alg": "ES256"}
    claims = json.loads(b64url_decode(encoded_claims))
    assert claims["aud"] == "https://fcm.googleapis.com"
    assert claims["sub"] == "mailto:admin@example.org"
    assert now < claims["exp"] <= now + 24 * 3600
    raw = b64url_decode(signature)
    der = encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big"))
    key = public_key_from_bytes(b64url_decode(public))
    key.verify(der, f"{encoded_header}.{encoded_claims}".encode(), ec.ECDSA(hashes.SHA256()))
    with pytest.raises(InvalidSignature):
        key.verify(der, b"tampered", ec.ECDSA(hashes.SHA256()))


def test_settings_need_both_keys_that_belong_together() -> None:
    public, private = generate_key_pair()
    other_public, _ = generate_key_pair()
    assert not NotificationsSettings().web_push_available
    with pytest.raises(ValidationError, match="set both"):
        NotificationsSettings(vapid_public_key=public)
    with pytest.raises(ValidationError, match="does not belong") as error:
        NotificationsSettings.model_validate(
            {"vapid_public_key": other_public, "vapid_private_key": private}
        )
    assert private not in str(error.value)
    with pytest.raises(ValidationError, match="mailto"):
        push_settings(vapid_subject="admin@example.org")


def test_web_push_is_available_only_when_switched_on_and_configured() -> None:
    assert push_settings().web_push_available
    assert not push_settings(web_push_enabled=False).web_push_available
    assert not push_settings(enabled=False).web_push_available
    assert not push_settings(vapid_subject="").web_push_available


def test_allowed_hosts_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OLLAMAIL_NOTIFICATIONS_WEB_PUSH_ALLOWED_HOSTS", " Push.Example.org., ")
    assert NotificationsSettings().web_push_allowed_hosts == ["push.example.org"]


@pytest.mark.parametrize(
    ("endpoint", "allowed"),
    [
        ("https://fcm.googleapis.com/fcm/send/abc", True),
        ("https://updates.push.services.mozilla.com/wpush/v2/abc", True),
        ("https://web.push.apple.com/abc", True),
        ("https://wns2-par02p.notify.windows.com/w/?token=abc", True),
        ("https://fcm.googleapis.com:443/fcm/send/abc", True),
        ("http://fcm.googleapis.com/fcm/send/abc", False),
        ("https://fcm.googleapis.com:8443/fcm/send/abc", False),
        ("https://fcm.googleapis.com.evil.example/abc", False),
        ("https://evilfcm.googleapis.com/abc", False),
        ("https://user:pw@fcm.googleapis.com/abc", False),
        ("https://localhost/abc", False),
        ("https://10.0.0.1/abc", False),
        ("https://fcm.googleapis.com:99999/abc", False),
        ("https://fcm.googleapis.com/" + "a" * 2100, False),
        ("not a url", False),
    ],
)
def test_only_https_endpoints_of_push_services_are_allowed(endpoint: str, allowed: bool) -> None:
    assert endpoint_allowed(endpoint, HOSTS) is allowed


@pytest.mark.parametrize(
    ("user_agent", "browser", "system", "mobile"),
    [
        (
            "Mozilla/5.0 (X11; Linux x86_64; rv:131.0) Gecko/20100101 Firefox/131.0",
            "Firefox",
            "Linux",
            False,
        ),
        (
            "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/129.0.0.0 Mobile Safari/537.36",
            "Chrome",
            "Android",
            True,
        ),
        (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 "
            "(KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1",
            "Safari",
            "iOS",
            True,
        ),
        (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/129.0.0.0 Safari/537.36 Edg/129.0.0.0",
            "Edge",
            "Windows",
            False,
        ),
        (None, None, None, False),
        ("curl/8.0", None, None, False),
    ],
)
def test_device_labels_from_the_user_agent(
    user_agent: str | None, browser: str | None, system: str | None, mobile: bool
) -> None:
    device = describe_device(user_agent)
    assert (device.browser, device.os, device.mobile) == (browser, system, mobile)


@pytest.mark.parametrize(
    ("status", "outcome"),
    [
        (201, PushOutcome.SENT),
        (404, PushOutcome.GONE),
        (410, PushOutcome.GONE),
        (429, PushOutcome.RETRY),
        (503, PushOutcome.RETRY),
        (400, PushOutcome.FAILED),
        (403, PushOutcome.FAILED),
        (413, PushOutcome.FAILED),
        # A redirect is not followed.
        (301, PushOutcome.FAILED),
    ],
)
@respx.mock
async def test_send_push(status: int, outcome: PushOutcome) -> None:
    settings = push_settings()
    browser = Browser(FCM + "device-1")
    route = respx.post(browser.endpoint).mock(
        return_value=httpx.Response(status, headers={"Location": "https://10.0.0.1/"})
    )
    vapid = Vapid.from_keys(settings.vapid_public_key, private_of(settings), "mailto:a@b.example")
    async with httpx.AsyncClient() as http:
        result = await send_push(
            http,
            vapid,
            {"endpoint": browser.endpoint, "p256dh": browser.p256dh, "auth": browser.auth},
            b'{"message_id":"m"}',
            ttl=600,
            allowed_hosts=HOSTS,
        )
    assert (result.outcome, result.status) == (outcome, status)
    request = route.calls.last.request
    assert request.headers["TTL"] == "600"
    assert request.headers["Content-Encoding"] == "aes128gcm"
    assert request.headers["Authorization"].startswith("vapid t=")
    assert browser.decrypt(request.content) == b'{"message_id":"m"}'


@respx.mock
async def test_send_push_without_connection_is_retried_and_other_hosts_are_not_called() -> None:
    settings = push_settings()
    vapid = Vapid.from_keys(settings.vapid_public_key, private_of(settings), "mailto:a@b.example")
    browser = Browser(FCM + "device-1")
    respx.post(browser.endpoint).mock(side_effect=httpx.ConnectError("down"))
    internal = respx.post("https://ollama:11434/api").mock(return_value=httpx.Response(200))
    async with httpx.AsyncClient() as http:
        keys = {"p256dh": browser.p256dh, "auth": browser.auth}
        result = await send_push(
            http, vapid, {"endpoint": browser.endpoint, **keys}, b"{}", ttl=0, allowed_hosts=HOSTS
        )
        assert result.outcome is PushOutcome.RETRY
        refused = await send_push(
            http,
            vapid,
            {"endpoint": "https://ollama:11434/api", **keys},
            b"{}",
            ttl=0,
            allowed_hosts=HOSTS,
        )
    assert refused.outcome is PushOutcome.FAILED
    assert not internal.called


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_breaker_opens_after_failures_in_a_row_and_closes_after_a_success() -> None:
    clock = FakeClock()
    breaker = PushServiceBreaker(threshold=3, cooldown=60, clock=clock)
    host = "fcm.googleapis.com"
    for outcome in (PushOutcome.RETRY, PushOutcome.RETRY, PushOutcome.SENT, PushOutcome.RETRY):
        breaker.record(host, outcome)
    # The success in between reset the count.
    assert breaker.allows(host)
    breaker.record(host, PushOutcome.RETRY)
    breaker.record(host, PushOutcome.RETRY)
    assert not breaker.allows(host)
    assert breaker.allows("push.services.mozilla.com")

    # After the cool-down one attempt gets through; failing, it opens the breaker again.
    clock.now = 61
    assert breaker.allows(host)
    assert not breaker.allows(host)
    breaker.record(host, PushOutcome.RETRY)
    clock.now = 100
    assert not breaker.allows(host)

    # An answer (also a refusal like 403) shows the service is reachable.
    clock.now = 125
    assert breaker.allows(host)
    breaker.record(host, PushOutcome.FAILED)
    assert breaker.allows(host)
    assert breaker.allows(host)


@respx.mock
async def test_send_push_skips_a_push_service_the_breaker_holds_open() -> None:
    settings = push_settings()
    vapid = Vapid.from_keys(settings.vapid_public_key, private_of(settings), "mailto:a@b.example")
    browser = Browser(FCM + "device-1")
    route = respx.post(browser.endpoint).mock(side_effect=httpx.ConnectTimeout("blocked"))
    breaker = PushServiceBreaker(threshold=2)
    subscription = {"endpoint": browser.endpoint, "p256dh": browser.p256dh, "auth": browser.auth}
    async with httpx.AsyncClient() as http:
        for _ in range(5):
            result = await send_push(
                http, vapid, subscription, b"{}", ttl=0, allowed_hosts=HOSTS, breaker=breaker
            )
            assert result.outcome is PushOutcome.RETRY
    assert route.call_count == 2


async def test_the_push_client_is_shared_and_does_not_wait_long_to_connect() -> None:
    client = push_client()
    assert push_client() is client
    assert client.timeout.connect == 3.0 and client.timeout.read == 10.0
    assert not client.follow_redirects
    await close_push_client()
    assert client.is_closed
    assert push_client() is not client
    await close_push_client()


def private_of(settings: NotificationsSettings) -> str:
    assert settings.vapid_private_key is not None
    return settings.vapid_private_key.get_secret_value()

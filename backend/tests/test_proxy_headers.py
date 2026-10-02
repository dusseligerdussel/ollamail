"""Client IP behind the frontend proxy (X-Forwarded-For, #137)."""

import pytest
from fastapi import Request
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from app.core.config import PRIVATE_NETWORKS, SecuritySettings, Settings
from app.main import create_app


async def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


async def _seen_ip(settings: Settings, peer: str, forwarded_for: str | None) -> str | None:
    app = create_app(settings)
    app.add_api_route("/test/client-ip", _client_ip, methods=["GET"])
    transport = ASGITransport(app=app, client=(peer, 40000))
    headers = {"X-Forwarded-For": forwarded_for} if forwarded_for is not None else {}
    try:
        async with AsyncClient(transport=transport, base_url="https://test") as http:
            response = await http.get("/test/client-ip", headers=headers)
    finally:
        await app.state.database.dispose()
    assert response.status_code == 200
    result: str | None = response.json()
    return result


@pytest.mark.parametrize(
    ("forwarded_for", "expected"),
    [
        # What the bundled Caddy sends: exactly one value.
        ("198.51.100.7", "198.51.100.7"),
        # An outer proxy appended the real IP to a value forged by the client.
        ("6.6.6.6, 198.51.100.7", "198.51.100.7"),
        ("6.6.6.6, 198.51.100.7, 10.0.0.2", "198.51.100.7"),
        (None, "172.18.0.5"),
    ],
)
async def test_trusted_proxy_sets_right_most_untrusted_client(
    settings: Settings, forwarded_for: str | None, expected: str
) -> None:
    assert await _seen_ip(settings, "172.18.0.5", forwarded_for) == expected


async def test_forwarded_for_from_untrusted_peer_is_ignored(settings: Settings) -> None:
    assert await _seen_ip(settings, "203.0.113.9", "6.6.6.6") == "203.0.113.9"


async def test_forwarded_allow_ips_is_configurable(settings: Settings) -> None:
    settings.security.forwarded_allow_ips = ["192.0.2.10"]

    assert await _seen_ip(settings, "192.0.2.10", "6.6.6.6") == "6.6.6.6"
    assert await _seen_ip(settings, "172.18.0.5", "6.6.6.6") == "172.18.0.5"


def test_forwarded_allow_ips_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OLLAMAIL_FORWARDED_ALLOW_IPS", " 10.1.0.0/16, fd00::1 ,")

    assert SecuritySettings().forwarded_allow_ips == ["10.1.0.0/16", "fd00::1"]


@pytest.mark.parametrize("value", ["*", "frontend", "10.0.0.0/33"])
def test_forwarded_allow_ips_rejects_wildcards_and_names(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("OLLAMAIL_FORWARDED_ALLOW_IPS", value)

    with pytest.raises(ValidationError, match="not an IP address or network"):
        SecuritySettings()


def test_empty_forwarded_allow_ips_is_the_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OLLAMAIL_FORWARDED_ALLOW_IPS", "")

    assert SecuritySettings().forwarded_allow_ips == list(PRIVATE_NETWORKS)

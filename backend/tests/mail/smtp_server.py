"""Access to the SMTP test server for integration tests (marker ``smtp``).

The tests run against Mailpit (``axllent/mailpit`` image, the ``mailpit`` service in
``.github/workflows/ci.yml``): it accepts every login, requires STARTTLS with a self-signed
certificate it generates itself, and keeps every message for inspection through its HTTP
API. Locally::

    docker run -d -p 31025:1025 -p 31080:8025 -e MP_SMTP_TLS_CERT=sans:localhost \\
        -e MP_SMTP_TLS_KEY=sans:localhost -e MP_SMTP_REQUIRE_STARTTLS=true \\
        -e MP_SMTP_AUTH_ACCEPT_ANY=true axllent/mailpit:v1.31.3

Environment: ``OLLAMAIL_TEST_SMTP_HOST`` (``localhost``), ``OLLAMAIL_TEST_SMTP_PORT``
(STARTTLS, ``31025``), ``OLLAMAIL_TEST_SMTP_API`` (``http://localhost:31080``). If the
server is unreachable the tests are skipped; ``OLLAMAIL_TEST_REQUIRE_SMTP=1`` (CI) fails
instead.

Only synthetic messages (``example.org`` addresses) are used.
"""

import asyncio
import os
import socket
from typing import Any

import httpx

HOST = os.environ.get("OLLAMAIL_TEST_SMTP_HOST", "localhost")
PORT = int(os.environ.get("OLLAMAIL_TEST_SMTP_PORT", "31025"))
API = os.environ.get("OLLAMAIL_TEST_SMTP_API", "http://localhost:31080").rstrip("/")
REQUIRE = os.environ.get("OLLAMAIL_TEST_REQUIRE_SMTP", "").lower() in {"1", "true", "yes"}


def probe() -> str | None:
    """``None`` if the server accepts TCP connections, else the error type."""
    try:
        with socket.create_connection((HOST, PORT), timeout=3):
            return None
    except OSError as exc:
        return type(exc).__name__


async def find(message_id: str) -> dict[str, Any]:
    """Summary of the message with ``message_id`` (without angle brackets) as Mailpit
    received it, waiting up to 10 seconds until it arrived."""
    async with httpx.AsyncClient(base_url=API, timeout=10) as client, asyncio.timeout(10):
        while True:
            response = await client.get(
                "/api/v1/search", params={"query": f"message-id:{message_id}"}
            )
            response.raise_for_status()
            messages = response.json()["messages"]
            if messages:
                result: dict[str, Any] = messages[0]
                return result
            await asyncio.sleep(0.2)


async def headers(mailpit_id: str) -> dict[str, list[str]]:
    async with httpx.AsyncClient(base_url=API, timeout=10) as client:
        response = await client.get(f"/api/v1/message/{mailpit_id}/headers")
        response.raise_for_status()
        result: dict[str, list[str]] = response.json()
        return result


async def raw(mailpit_id: str) -> bytes:
    async with httpx.AsyncClient(base_url=API, timeout=10) as client:
        response = await client.get(f"/api/v1/message/{mailpit_id}/raw")
        response.raise_for_status()
        return response.content

"""Synthetic Gmail API (respx) for contract tests of the Gmail provider.

Request and response shapes follow the Gmail API reference (users.getProfile, labels.list,
labels.create, messages.list, messages.get ``format=raw`` via batch requests,
messages.modify, messages.trash, history.list, users.watch) and the Pub/Sub REST API
(subscriptions.pull/acknowledge/modifyAckDeadline). All data is synthetic.
"""

import base64
import itertools
import json
import re
from dataclasses import dataclass, field
from typing import Any

import httpx
import respx

API = "https://gmail.googleapis.com"
USER = f"{API}/gmail/v1/users/me"
PUBSUB = "https://pubsub.googleapis.com/v1"
TOKEN_URL = "https://oauth2.googleapis.com/token"

SYSTEM_LABELS = [
    "INBOX",
    "SENT",
    "DRAFT",
    "SPAM",
    "TRASH",
    "UNREAD",
    "STARRED",
    "IMPORTANT",
    "CHAT",
    "CATEGORY_PERSONAL",
    "CATEGORY_SOCIAL",
]


def google_error(status: int, reason: str, text: str = "synthetic error") -> httpx.Response:
    body = {
        "error": {
            "code": status,
            "message": text,
            "errors": [{"message": text, "domain": "global", "reason": reason}],
            "status": {404: "NOT_FOUND", 429: "RESOURCE_EXHAUSTED"}.get(status, "UNKNOWN"),
        }
    }
    return httpx.Response(status, json=body)


@dataclass
class StoredMessage:
    id: str
    thread_id: str
    raw: bytes
    label_ids: list[str]
    internal_date: int

    def resource(self, *, raw: bool = True) -> dict[str, Any]:
        data: dict[str, Any] = {
            "id": self.id,
            "threadId": self.thread_id,
            "labelIds": list(self.label_ids),
            "internalDate": str(self.internal_date),
            "sizeEstimate": len(self.raw),
        }
        if raw:
            data["raw"] = base64.urlsafe_b64encode(self.raw).decode().rstrip("=")
        return data

    def ref(self) -> dict[str, Any]:
        return {"id": self.id, "threadId": self.thread_id, "labelIds": list(self.label_ids)}


@dataclass
class Fault:
    method: str
    pattern: str
    response: httpx.Response | Exception
    remaining: int = 1


@dataclass
class GmailServer:
    address: str = "test.user@example.com"
    labels: dict[str, dict[str, Any]] = field(default_factory=dict)
    messages: dict[str, StoredMessage] = field(default_factory=dict)
    history: list[dict[str, Any]] = field(default_factory=list)
    history_id: int = 1000
    # history.list answers 404 for older start IDs (expired history).
    oldest_history_id: int = 0
    faults: list[Fault] = field(default_factory=list)
    requests: list[httpx.Request] = field(default_factory=list)
    batch_sizes: list[int] = field(default_factory=list)
    modifications: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    pubsub_messages: list[dict[str, Any]] = field(default_factory=list)
    acked: list[str] = field(default_factory=list)
    nacked: list[str] = field(default_factory=list)
    watch_calls: list[dict[str, Any]] = field(default_factory=list)
    _ids: Any = field(default_factory=lambda: itertools.count(1))
    _labels_seq: Any = field(default_factory=lambda: itertools.count(1))

    def __post_init__(self) -> None:
        for label_id in SYSTEM_LABELS:
            self.labels[label_id] = {"id": label_id, "name": label_id, "type": "system"}

    # -- test setup -------------------------------------------------------------------

    def add_label(self, name: str) -> str:
        label_id = f"Label_{next(self._labels_seq)}"
        self.labels[label_id] = {
            "id": label_id,
            "name": name,
            "type": "user",
            "messageListVisibility": "show",
            "labelListVisibility": "labelShow",
        }
        return label_id

    def _record(self, **changes: Any) -> None:
        self.history_id += 1
        self.history.append({"id": str(self.history_id), **changes})

    def add_message(
        self,
        raw: bytes,
        labels: list[str] | None = None,
        *,
        internal_date: int = 1_790_000_000_000,
        thread_id: str | None = None,
    ) -> str:
        message_id = f"18f{next(self._ids):013x}"
        message = StoredMessage(
            message_id,
            thread_id or message_id,
            raw,
            list(labels if labels is not None else ["INBOX", "UNREAD"]),
            internal_date,
        )
        self.messages[message_id] = message
        self._record(messages=[message.ref()], messagesAdded=[{"message": message.ref()}])
        return message_id

    def delete_message(self, message_id: str) -> None:
        message = self.messages.pop(message_id)
        self._record(messages=[message.ref()], messagesDeleted=[{"message": message.ref()}])

    def change_labels(
        self, message_id: str, add: list[str] | None = None, remove: list[str] | None = None
    ) -> None:
        message = self.messages[message_id]
        changes: dict[str, Any] = {}
        added = [label for label in add or [] if label not in message.label_ids]
        message.label_ids += added
        removed = [label for label in remove or [] if label in message.label_ids]
        message.label_ids = [label for label in message.label_ids if label not in removed]
        if added:
            changes["labelsAdded"] = [{"message": message.ref(), "labelIds": added}]
        if removed:
            changes["labelsRemoved"] = [{"message": message.ref(), "labelIds": removed}]
        if changes:
            self._record(messages=[message.ref()], **changes)

    def expire_history(self) -> None:
        self.oldest_history_id = self.history_id + 1

    def fail(
        self, method: str, pattern: str, response: httpx.Response | Exception, times: int = 1
    ) -> None:
        """The next ``times`` requests matching ``pattern`` (regex on the URL path) fail
        with ``response`` (or raise it, for network errors)."""
        self.faults.append(Fault(method, pattern, response, times))

    def notify(self, address: str | None = None, ack_id: str | None = None) -> None:
        data = {"emailAddress": address or self.address, "historyId": self.history_id}
        self.pubsub_messages.append(
            {
                "ackId": ack_id or f"ack-{len(self.pubsub_messages) + 1}",
                "message": {
                    "data": base64.b64encode(json.dumps(data).encode()).decode(),
                    "messageId": str(len(self.pubsub_messages) + 1),
                },
            }
        )

    def paths(self, method: str | None = None) -> list[str]:
        return [r.url.path for r in self.requests if method is None or r.method == method]

    # -- routes -----------------------------------------------------------------------

    def install(self, router: respx.MockRouter) -> None:
        router.route(host="gmail.googleapis.com").mock(side_effect=self._handle)
        router.route(host="pubsub.googleapis.com").mock(side_effect=self._handle_pubsub)

    def _fault(self, request: httpx.Request) -> httpx.Response | None:
        for fault in self.faults:
            if (
                fault.remaining > 0
                and fault.method == request.method
                and re.search(fault.pattern, request.url.path)
            ):
                fault.remaining -= 1
                if isinstance(fault.response, Exception):
                    raise fault.response
                return fault.response
        return None

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        fault = self._fault(request)
        if fault is not None:
            return fault
        path = request.url.path.removeprefix("/gmail/v1/users/me")
        method = request.method
        if request.url.path == "/batch/gmail/v1" and method == "POST":
            return self._batch(request)
        if path == "/profile":
            return httpx.Response(
                200,
                json={
                    "emailAddress": self.address,
                    "messagesTotal": len(self.messages),
                    "threadsTotal": len(self.messages),
                    "historyId": str(self.history_id),
                },
            )
        if path == "/labels" and method == "GET":
            return httpx.Response(200, json={"labels": list(self.labels.values())})
        if path == "/labels" and method == "POST":
            name = json.loads(request.content)["name"]
            if any(label["name"] == name for label in self.labels.values()):
                return google_error(409, "duplicate", "Label name exists or conflicts")
            label_id = self.add_label(name)
            return httpx.Response(200, json=self.labels[label_id])
        if path == "/messages" and method == "GET":
            return self._list(request)
        if path == "/history" and method == "GET":
            return self._history(request)
        if path == "/watch" and method == "POST":
            self.watch_calls.append(json.loads(request.content))
            return httpx.Response(
                200, json={"historyId": str(self.history_id), "expiration": "1790604800000"}
            )
        match = re.fullmatch(r"/messages/([^/]+)/(modify|trash)", path)
        if match and method == "POST":
            message = self.messages.get(match.group(1))
            if message is None:
                return google_error(404, "notFound", "Requested entity was not found.")
            if match.group(2) == "trash":
                self.modifications.append((message.id, {"trash": True}))
                self.change_labels(message.id, add=["TRASH"], remove=["INBOX"])
            else:
                body = json.loads(request.content)
                self.modifications.append((message.id, body))
                self.change_labels(
                    message.id, add=body.get("addLabelIds"), remove=body.get("removeLabelIds")
                )
            return httpx.Response(200, json=message.resource(raw=False))
        return google_error(404, "notFound", "Unknown path")

    def _list(self, request: httpx.Request) -> httpx.Response:
        params = request.url.params
        size = int(params.get("maxResults", "100"))
        if params.get("pageToken") == "expired":
            return google_error(400, "invalidArgument", "Invalid pageToken")
        offset = int(params.get("pageToken", "0") or 0)
        include = params.get("includeSpamTrash") == "true"
        after = None
        match = re.search(r"after:(\d+)", params.get("q", ""))
        if match:
            after = int(match.group(1)) * 1000
        found = [
            m
            for m in sorted(self.messages.values(), key=lambda m: m.internal_date, reverse=True)
            if (include or not {"SPAM", "TRASH"} & set(m.label_ids))
            and (after is None or m.internal_date >= after)
        ]
        page = found[offset : offset + size]
        body: dict[str, Any] = {"resultSizeEstimate": len(found)}
        if page:
            body["messages"] = [{"id": m.id, "threadId": m.thread_id} for m in page]
        if offset + size < len(found):
            body["nextPageToken"] = str(offset + size)
        return httpx.Response(200, json=body)

    def _history(self, request: httpx.Request) -> httpx.Response:
        params = request.url.params
        start = int(params["startHistoryId"])
        if start < self.oldest_history_id:
            return google_error(404, "notFound", "Requested entity was not found.")
        size = int(params.get("maxResults", "100"))
        offset = int(params.get("pageToken", "0") or 0)
        records = [r for r in self.history if int(r["id"]) > start]
        page = records[offset : offset + size]
        body: dict[str, Any] = {"historyId": str(self.history_id)}
        if page:
            body["history"] = page
        if offset + size < len(records):
            body["nextPageToken"] = str(offset + size)
        return httpx.Response(200, json=body)

    def _batch(self, request: httpx.Request) -> httpx.Response:
        content_type = request.headers["Content-Type"]
        boundary = content_type.split("boundary=")[1]
        parts = [p for p in request.content.decode().split(f"--{boundary}") if "Content-ID" in p]
        self.batch_sizes.append(len(parts))
        out = "batch_response_boundary"
        chunks: list[str] = []
        for part in parts:
            content_id = re.search(r"Content-ID: <([^>]+)>", part).group(1)  # type: ignore[union-attr]
            target = re.search(r"GET (\S+)", part).group(1)  # type: ignore[union-attr]
            sub = httpx.Request("GET", f"{API}{target}")
            fault = self._fault(sub)
            if fault is not None:
                status, body = fault.status_code, fault.content.decode()
            else:
                message_id = re.fullmatch(
                    r"/gmail/v1/users/me/messages/([^?]+)", sub.url.path
                ).group(1)  # type: ignore[union-attr]
                message = self.messages.get(message_id)
                if message is None:
                    status = 404
                    body = google_error(404, "notFound").content.decode()
                else:
                    status, body = 200, json.dumps(message.resource())
            chunks.append(
                f"--{out}\r\nContent-Type: application/http\r\n"
                f"Content-ID: <response-{content_id}>\r\n\r\n"
                f"HTTP/1.1 {status} {'OK' if status == 200 else 'Error'}\r\n"
                f"Content-Type: application/json; charset=UTF-8\r\n\r\n{body}\r\n"
            )
        chunks.append(f"--{out}--\r\n")
        return httpx.Response(
            200,
            content="".join(chunks).encode(),
            headers={"Content-Type": f"multipart/mixed; boundary={out}"},
        )

    def _handle_pubsub(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        fault = self._fault(request)
        if fault is not None:
            return fault
        body = json.loads(request.content or b"{}")
        if request.url.path.endswith(":pull"):
            batch = self.pubsub_messages[: body.get("maxMessages", 10)]
            del self.pubsub_messages[: len(batch)]
            return httpx.Response(200, json={"receivedMessages": batch} if batch else {})
        if request.url.path.endswith(":acknowledge"):
            self.acked += body["ackIds"]
            return httpx.Response(200, json={})
        if request.url.path.endswith(":modifyAckDeadline"):
            self.nacked += body["ackIds"]
            return httpx.Response(200, json={})
        return google_error(404, "notFound")


class StaticTokens:
    """Token source for tests: counts refreshes."""

    def __init__(self) -> None:
        self.refreshes = 0
        self.calls = 0

    async def token(self, *, refresh: bool = False) -> str:
        self.calls += 1
        if refresh:
            self.refreshes += 1
        return f"test-access-token-{self.refreshes}"

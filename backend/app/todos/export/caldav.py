"""CalDAV (RFC 4791) sink: todos as VTODO resources in a calendar collection.

Works with Nextcloud, Radicale, Baïkal, SOGo, Apple iCloud and other CalDAV servers.
Plain ``httpx`` with Basic auth (app passwords), no CalDAV library.

* **Discovery** (``list_task_lists``): the configured URL may be a calendar, the calendar
  home, the principal or the server root; ``current-user-principal`` and
  ``calendar-home-set`` lead to the collections, ``/.well-known/caldav`` is tried for a
  bare host. Only collections that accept VTODO are offered.
* **IDs** are server-relative paths: a list is a collection path, a task the path of its
  ``<todo id>.ics`` resource. Requests only ever go to the configured server (origin);
  hrefs pointing elsewhere are ignored, redirects are only followed within that origin
  (discovery only; a list found behind a redirect gets the target's path as its ID).
* **Destination check** (#189): every connection goes through ``GuardedTransport``, which
  refuses internal addresses (loopback, RFC 1918, link-local, ULA, ...) unless
  ``OLLAMAIL_TODOS_EXPORT_ALLOWED_INTERNAL_HOSTS`` allows them, and connects to the checked
  address (no DNS rebinding). A refused server fails like an unreachable one.
* **Writes** are conditional: ``If-None-Match: *`` on create (a second push of the same
  todo overwrites instead of duplicating), ``If-Match`` on update (412 = conflict).
* **Status sync** (``changes``): one ``PROPFIND`` for the ETags of the collection, then a
  ``calendar-multiget`` for the tasks whose ETag changed.

Errors are mapped to coarse ``SinkError`` codes (``unavailable``, ``auth_failed``,
``not_found``, ``not_caldav``, ``conflict``), so they do not reveal what answered at an
address (no status codes); response bodies are never logged or stored.
"""

import xml.etree.ElementTree as ET
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import ClassVar
from urllib.parse import quote, unquote, urljoin, urlsplit

import httpx

from app.core.http_guard import GuardedTransport
from app.todos.export.base import (
    RemoteTask,
    RemoteVersion,
    SinkAuthError,
    SinkConflictError,
    SinkError,
    SinkNotFoundError,
    SinkUnavailableError,
    TaskData,
    TaskList,
    TodoSink,
)
from app.todos.export.ical import ICalError, build_vtodo, parse_vtodo

DAV = "DAV:"
CALDAV = "urn:ietf:params:xml:ns:caldav"
CALENDAR_SERVER = "http://calendarserver.org/ns/"
# Larger responses are rejected (a collection with thousands of tasks still fits).
MAX_RESPONSE_BYTES = 10 * 1024 * 1024
MAX_REDIRECTS = 3
# Hrefs per calendar-multiget request.
MULTIGET_BATCH = 50
XML_HEADERS = {"Content-Type": "application/xml; charset=utf-8"}
ICAL_TYPE = "text/calendar; charset=utf-8"
# Any answer that is not what a CalDAV server sends (status, body, redirect).
NOT_CALDAV = "not_caldav"


def _tag(namespace: str, name: str) -> str:
    return f"{{{namespace}}}{name}"


def validate_server_url(url: str, *, allow_http: bool) -> str:
    """The server URL without query/fragment; raises ``SinkError("invalid_url")``."""
    value = url.strip()
    try:
        parts = urlsplit(value)
    except ValueError:
        raise SinkError("invalid_url") from None
    schemes = {"https", "http"} if allow_http else {"https"}
    if parts.scheme not in schemes:
        raise SinkError("insecure_url" if parts.scheme == "http" else "invalid_url")
    if not parts.hostname or parts.username or parts.password or parts.fragment:
        raise SinkError("invalid_url")
    path = parts.path or "/"
    return f"{parts.scheme}://{parts.netloc}{path}"


@dataclass(frozen=True)
class _Response:
    href: str
    status: int | None
    props: dict[str, ET.Element]


def _status_code(element: ET.Element | None) -> int | None:
    if element is None or not element.text:
        return None
    parts = element.text.split()
    return int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None


def parse_multistatus(content: bytes) -> list[_Response]:
    """Responses of a 207 Multi-Status body; only properties with status 200."""
    if b"<!DOCTYPE" in content or b"<!ENTITY" in content:
        # No DTDs: rules out entity expansion attacks from a hostile server.
        raise SinkError(NOT_CALDAV)
    try:
        root = ET.fromstring(content)
    except ET.ParseError:
        raise SinkError(NOT_CALDAV) from None
    responses = []
    for item in root.iter(_tag(DAV, "response")):
        href = item.findtext(_tag(DAV, "href"))
        if not href:
            continue
        props: dict[str, ET.Element] = {}
        for propstat in item.findall(_tag(DAV, "propstat")):
            if _status_code(propstat.find(_tag(DAV, "status"))) != 200:
                continue
            prop = propstat.find(_tag(DAV, "prop"))
            for child in prop if prop is not None else ():
                props[child.tag] = child
        status = _status_code(item.find(_tag(DAV, "status")))
        responses.append(_Response(href.strip(), status, props))
    return responses


def _propfind_body(props: Iterable[tuple[str, str]]) -> bytes:
    root = ET.Element(_tag(DAV, "propfind"))
    prop = ET.SubElement(root, _tag(DAV, "prop"))
    for namespace, name in props:
        ET.SubElement(prop, _tag(namespace, name))
    return bytes(ET.tostring(root, encoding="utf-8", xml_declaration=True))


def _multiget_body(paths: Iterable[str]) -> bytes:
    root = ET.Element(_tag(CALDAV, "calendar-multiget"))
    prop = ET.SubElement(root, _tag(DAV, "prop"))
    ET.SubElement(prop, _tag(DAV, "getetag"))
    ET.SubElement(prop, _tag(CALDAV, "calendar-data"))
    for path in paths:
        ET.SubElement(root, _tag(DAV, "href")).text = quote(path)
    return bytes(ET.tostring(root, encoding="utf-8", xml_declaration=True))


def _is_calendar(props: Mapping[str, ET.Element]) -> bool:
    resourcetype = props.get(_tag(DAV, "resourcetype"))
    return resourcetype is not None and resourcetype.find(_tag(CALDAV, "calendar")) is not None


def _supports_vtodo(props: Mapping[str, ET.Element]) -> bool:
    components = props.get(_tag(CALDAV, "supported-calendar-component-set"))
    if components is None:
        # Not announced: the server accepts every component.
        return True
    return any(
        (comp.get("name") or "").upper() == "VTODO"
        for comp in components.findall(_tag(CALDAV, "comp"))
    )


def _inner_href(props: Mapping[str, ET.Element], namespace: str, name: str) -> str | None:
    element = props.get(_tag(namespace, name))
    if element is None:
        return None
    text = element.findtext(_tag(DAV, "href"))
    return text.strip() if text else None


def _etag(props: Mapping[str, ET.Element]) -> str | None:
    element = props.get(_tag(DAV, "getetag"))
    return element.text.strip() if element is not None and element.text else None


class CalDAVSink(TodoSink):
    kind: ClassVar[str] = "caldav"

    def __init__(
        self,
        url: str,
        username: str,
        password: str,
        *,
        timeout: float = 20.0,
        allow_http: bool = False,
        allowed_internal_hosts: Sequence[str] = (),
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._url = validate_server_url(url, allow_http=allow_http)
        parts = urlsplit(self._url)
        self._origin = f"{parts.scheme}://{parts.netloc}"
        self._client = httpx.AsyncClient(
            auth=httpx.BasicAuth(username, password) if username else None,
            timeout=timeout,
            follow_redirects=False,
            # ``transport`` only in tests; it skips the destination check.
            transport=transport
            or GuardedTransport(
                allowed_internal_hosts, log_event="todo_export_destination_refused"
            ),
            headers={"User-Agent": "ollamail"},
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    # -- addressing ------------------------------------------------------------------------

    def _path(self, href: str, base: str | None = None) -> str | None:
        """Server-relative, unquoted path of an href; ``None`` if it points elsewhere."""
        absolute = urljoin(base or self._url, href)
        parts = urlsplit(absolute)
        if f"{parts.scheme}://{parts.netloc}" != self._origin:
            return None
        return unquote(parts.path) or "/"

    def _absolute(self, path: str) -> str:
        if not path.startswith("/"):
            raise SinkError("invalid_id")
        return f"{self._origin}{quote(path)}"

    @staticmethod
    def _collection(list_id: str) -> str:
        return list_id if list_id.endswith("/") else f"{list_id}/"

    # -- HTTP ------------------------------------------------------------------------------

    async def _request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        content: bytes | None = None,
        follow: bool = False,
    ) -> httpx.Response:
        for _ in range(MAX_REDIRECTS + 1):
            try:
                response = await self._send(
                    method, url, headers=dict(headers or {}), content=content
                )
            except httpx.HTTPError:
                # Timeouts included: no hint whether something listens at the address.
                raise SinkUnavailableError() from None
            if follow and response.status_code in {301, 302, 307, 308}:
                location = response.headers.get("location")
                target = self._path(location, url) if location else None
                if target is None:
                    raise SinkError(NOT_CALDAV)
                url = self._absolute(target)
                continue
            break
        else:
            raise SinkError(NOT_CALDAV)
        status = response.status_code
        if status in {401, 403}:
            raise SinkAuthError()
        if status == 429 or status >= 500:
            raise SinkUnavailableError()
        return response

    async def _send(
        self, method: str, url: str, *, headers: dict[str, str], content: bytes | None
    ) -> httpx.Response:
        """The response with its body read, up to ``MAX_RESPONSE_BYTES``: the body is
        streamed and the connection dropped once it grows larger, so a server cannot make
        the worker buffer an arbitrarily large answer."""
        request = self._client.build_request(method, url, headers=headers, content=content)
        response = await self._client.send(request, stream=True)
        try:
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > MAX_RESPONSE_BYTES:
                    raise SinkError(NOT_CALDAV)
        finally:
            await response.aclose()
        # ``aiter_bytes`` already decoded the body; without the header it is not decoded again.
        response_headers = [
            (name, value)
            for name, value in response.headers.multi_items()
            if name.lower() != "content-encoding"
        ]
        return httpx.Response(
            response.status_code,
            headers=response_headers,
            content=bytes(body),
            request=response.request,
        )

    async def _propfind_at(
        self, url: str, depth: int, props: Iterable[tuple[str, str]], *, follow: bool
    ) -> tuple[str, list[_Response] | None]:
        """The URL that answered (the redirect target, if any) and its multi-status
        responses, or ``None`` if the resource does not exist."""
        response = await self._request(
            "PROPFIND",
            url,
            headers={**XML_HEADERS, "Depth": str(depth)},
            content=_propfind_body(props),
            follow=follow,
        )
        # ``_request`` only follows redirects within the origin, so this stays on the server.
        final_url = str(response.request.url)
        if response.status_code in {404, 405, 410}:
            return final_url, None
        if response.status_code != 207:
            raise SinkError(NOT_CALDAV)
        return final_url, parse_multistatus(response.content)

    async def _propfind(
        self, url: str, depth: int, props: Iterable[tuple[str, str]]
    ) -> list[_Response] | None:
        """Multi-status responses, or ``None`` if the resource does not exist."""
        _, responses = await self._propfind_at(url, depth, props, follow=False)
        return responses

    async def _props(
        self, url: str, props: Iterable[tuple[str, str]]
    ) -> dict[str, ET.Element] | None:
        responses = await self._propfind(url, 0, props)
        if not responses:
            return None
        return responses[0].props

    async def _located_props(
        self, url: str, props: Iterable[tuple[str, str]]
    ) -> tuple[str, dict[str, ET.Element] | None]:
        """Like ``_props``, but follows redirects and returns the URL that answered."""
        final_url, responses = await self._propfind_at(url, 0, props, follow=True)
        return final_url, responses[0].props if responses else None

    # -- discovery -------------------------------------------------------------------------

    async def _home_from(self, url: str, props: Mapping[str, ET.Element]) -> str | None:
        home = _inner_href(props, CALDAV, "calendar-home-set")
        if home:
            return self._path(home, url)
        principal = _inner_href(props, DAV, "current-user-principal")
        principal_path = self._path(principal, url) if principal else None
        if principal_path is None:
            return None
        principal_props = await self._props(
            self._absolute(principal_path), [(CALDAV, "calendar-home-set")]
        )
        home = _inner_href(principal_props or {}, CALDAV, "calendar-home-set")
        return self._path(home, self._absolute(principal_path)) if home else None

    async def list_task_lists(self) -> list[TaskList]:
        # After a redirect, IDs and relative hrefs refer to the target, not the configured URL
        # (PUT, DELETE and REPORT do not follow redirects).
        url, props = await self._located_props(
            self._url,
            [
                (DAV, "resourcetype"),
                (DAV, "displayname"),
                (DAV, "current-user-principal"),
                (CALDAV, "calendar-home-set"),
                (CALDAV, "supported-calendar-component-set"),
            ],
        )
        if props is not None and _is_calendar(props):
            path = self._path(url) or "/"
            if not _supports_vtodo(props):
                return []
            return [TaskList(self._collection(path), _display_name(props, path))]
        home = await self._home_from(url, props) if props is not None else None
        if home is None and urlsplit(self._url).path in {"", "/"}:
            well_known_url, well_known = await self._located_props(
                f"{self._origin}/.well-known/caldav", [(DAV, "current-user-principal")]
            )
            if well_known is not None:
                home = await self._home_from(well_known_url, well_known)
        if home is None:
            if props is None:
                raise SinkNotFoundError()
            home = self._path(url) or "/"
        home_url = self._absolute(self._collection(home))
        children = await self._propfind(
            home_url,
            1,
            [
                (DAV, "resourcetype"),
                (DAV, "displayname"),
                (CALDAV, "supported-calendar-component-set"),
            ],
        )
        if children is None:
            raise SinkNotFoundError()
        lists = []
        for child in children:
            child_path = self._path(child.href, home_url)
            if child_path is None or not _is_calendar(child.props):
                continue
            if not _supports_vtodo(child.props):
                continue
            collection = self._collection(child_path)
            lists.append(TaskList(collection, _display_name(child.props, collection)))
        return sorted({item.id: item for item in lists}.values(), key=lambda i: i.name.lower())

    # -- tasks -----------------------------------------------------------------------------

    async def _resource_etag(self, path: str) -> str | None:
        props = await self._props(self._absolute(path), [(DAV, "getetag")])
        return _etag(props or {})

    async def _put(self, path: str, task: TaskData, headers: Mapping[str, str]) -> httpx.Response:
        body = build_vtodo(task, now=datetime.now(UTC)).encode("utf-8")
        return await self._request(
            "PUT",
            self._absolute(path),
            headers={"Content-Type": ICAL_TYPE, **headers},
            content=body,
        )

    async def _version(self, path: str, response: httpx.Response) -> RemoteVersion:
        etag = response.headers.get("etag") or await self._resource_etag(path)
        return RemoteVersion(path, etag)

    async def push(self, list_id: str, task: TaskData) -> RemoteVersion:
        path = f"{self._collection(list_id)}{task.uid}.ics"
        response = await self._put(path, task, {"If-None-Match": "*"})
        if response.status_code == 412:
            # Written by an earlier attempt whose answer got lost: overwrite it.
            return await self.update(list_id, path, None, task)
        return await self._written(path, response)

    async def update(
        self, list_id: str, remote_id: str, etag: str | None, task: TaskData
    ) -> RemoteVersion:
        headers = {"If-Match": etag} if etag else {}
        response = await self._put(remote_id, task, headers)
        if response.status_code == 412:
            raise SinkConflictError()
        return await self._written(remote_id, response)

    async def _written(self, path: str, response: httpx.Response) -> RemoteVersion:
        status = response.status_code
        if status in {200, 201, 204}:
            return await self._version(path, response)
        if status in {404, 409, 410}:
            # 409: the collection is missing.
            raise SinkNotFoundError()
        raise SinkError(NOT_CALDAV)

    async def delete(self, list_id: str, remote_id: str) -> None:
        response = await self._request("DELETE", self._absolute(remote_id))
        if response.status_code not in {200, 202, 204, 404, 410}:
            raise SinkError(NOT_CALDAV)

    async def changes(
        self, list_id: str, known: Mapping[str, str | None]
    ) -> dict[str, RemoteTask | None]:
        if not known:
            return {}
        collection_url = self._absolute(self._collection(list_id))
        listing = await self._propfind(collection_url, 1, [(DAV, "getetag")])
        if listing is None:
            raise SinkNotFoundError()
        current: dict[str, str | None] = {}
        for item in listing:
            path = self._path(item.href, collection_url)
            if path is not None and not path.endswith("/"):
                current[path] = _etag(item.props)
        result: dict[str, RemoteTask | None] = {}
        changed = []
        for remote_id, etag in known.items():
            if remote_id not in current:
                result[remote_id] = None
            elif current[remote_id] != etag or etag is None:
                changed.append(remote_id)
        for start in range(0, len(changed), MULTIGET_BATCH):
            batch = changed[start : start + MULTIGET_BATCH]
            result.update(await self._multiget(collection_url, batch))
        return result

    async def _multiget(
        self, collection_url: str, paths: list[str]
    ) -> dict[str, RemoteTask | None]:
        response = await self._request(
            "REPORT",
            collection_url,
            headers={**XML_HEADERS, "Depth": "1"},
            content=_multiget_body(paths),
        )
        if response.status_code != 207:
            raise SinkError(NOT_CALDAV)
        wanted = set(paths)
        found: dict[str, RemoteTask | None] = {}
        skipped: set[str] = set()
        for item in parse_multistatus(response.content):
            path = self._path(item.href, collection_url)
            if path not in wanted:
                continue
            assert path is not None
            data = item.props.get(_tag(CALDAV, "calendar-data"))
            if item.status in {404, 410} or data is None or not data.text:
                found[path] = None
                continue
            try:
                parsed = parse_vtodo(data.text)
            except ICalError:
                # Not a task any more (replaced by something else): leave it alone.
                skipped.add(path)
                continue
            found[path] = RemoteTask(path, _etag(item.props), parsed.status, parsed.modified_at)
        # Not in the answer: deleted in the meantime.
        for path in wanted - found.keys() - skipped:
            found[path] = None
        return found


def _display_name(props: Mapping[str, ET.Element], path: str) -> str:
    element = props.get(_tag(DAV, "displayname"))
    if element is not None and element.text and element.text.strip():
        return element.text.strip()[:255]
    return path.rstrip("/").rsplit("/", 1)[-1] or path

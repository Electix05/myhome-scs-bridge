"""A small web interface, served through Home Assistant's ingress.

The add-on already runs an event loop, so the interface is a plain asyncio HTTP
server rather than a framework: no extra dependency, nothing to compile on the
target architecture.

Ingress puts Home Assistant's authentication in front of this and rewrites the
path prefix, which is why every URL in the page is relative and routes are
recognised by their tail.  The Home Assistant developer documentation demands
that only the ingress proxy (172.30.32.2) may connect; everybody else is turned
away.

Nothing in here blocks the event loop: files are read and written in worker
threads, requests have a size limit and a time limit.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Collection

from ..own import messages as msg
from ..own.gateway import RepeatLimiter
from .schema import SCHEMA

_LOGGER = logging.getLogger(__name__)

INGRESS_PORT = 8099
# The Supervisor's ingress proxy - the only client allowed in the add-on.
INGRESS_PROXY = "172.30.32.2"
DEFAULT_ALLOWED_PEERS = (INGRESS_PROXY, "127.0.0.1")
PAGE_FILE = Path(__file__).with_name("static") / "index.html"

REQUEST_TIMEOUT = 15.0
# How long stop() waits for open requests.  After SIGTERM the add-on may be
# killed within seconds (s6-overlay's default grace time is 3 s); the bridge
# therefore waits for the page only after it said goodbye to MQTT.
STOP_WAIT = 2.0
MAX_HEAD = 16 * 1024
MAX_BODY = 1024 * 1024
MAX_FRAME_LENGTH = 64

SUPERVISOR_API = "http://supervisor"
SUPERVISOR_TIMEOUT = 10.0

ROUTES = ("/api/devices", "/api/status", "/api/frames", "/api/send", "/api/scan",
          "/api/restart", "/api/schema", "/api/purge", "/api/check", "/api/inventory")

JSON_TYPE = "application/json; charset=utf-8"
HTML_TYPE = "text/html; charset=utf-8"

Response = tuple[str, str, bytes]


class WebError(Exception):
    """A request that cannot be served; the text goes back to the page."""

    def __init__(self, status: str, text: str) -> None:
        super().__init__(text)
        self.status = status
        self.text = text


@dataclass
class WebHandlers:
    """What the interface can ask the bridge for."""

    status: Callable[[], dict[str, Any]]
    devices: Callable[[], dict[str, list[dict[str, Any]]]]
    # save_devices(sections) or save_devices(sections, revision): the revision
    # is the state of the list the page had loaded.  A save from a list that is
    # out of date is refused with WebError("409 Conflict", ...).
    save_devices: Callable[..., Awaitable[None]]
    frames: Callable[[], list[dict[str, Any]]]
    # Returns why the frame was not taken, or "" when the gateway acknowledged it.
    send_frame: Callable[[str], Awaitable[str]]
    scan: Callable[[bool], None]
    restart: Callable[[], None]
    # Run the clean-up that waits for a confirmation; the number of removed
    # topics, or None when nothing could be removed.
    purge: Callable[[], Awaitable[int | None]] | None = None
    # What is wrong with a device list, row by row (bridge/check.py); raises
    # ValueError when it is no device list at all.  Asked while somebody types
    # and once more before anything is written.
    check: Callable[[object], list[dict[str, Any]]] | None = None
    # The device list together with the revision it belongs to and the
    # devices that are not in the device file yet: {revision, devices, new}.
    inventory: Callable[[], dict[str, Any]] | None = None


# --------------------------------------------------------------------------
# Input checks - pure functions
# --------------------------------------------------------------------------

def frame_problem(frame: str) -> str:
    """Why a hand-written frame must not go on the bus; "" when it may.

    Exactly one frame of digits, ``*`` and ``#`` - two frames glued together or
    a stray character behind the ``##`` would put more on the bus than the
    person typed in the box.
    """
    if not frame.startswith("*") or not frame.endswith("##"):
        return "Ein Rahmen beginnt mit * und endet mit ##."
    frames, rest = msg.split_frames(frame)
    if (len(frame) > MAX_FRAME_LENGTH or frames != [frame] or rest
            or any(char not in "0123456789*#" for char in frame)):
        return "Das sieht nicht nach einem Rahmen aus."
    return ""


def split_revision(data: dict[str, Any]) -> tuple[object, int | None]:
    """What the page posts to ``/api/devices``: ``(sections, revision)``.

    The page sends ``{"revision": 7, "devices": {...}}`` - the list together
    with the state it was loaded at.  The plain list of sections, as earlier
    versions of the page sent it, is still taken; then there is no revision
    and nothing to compare.

    Raises:
        WebError: 400 when the revision is no whole number.
    """
    # No section is called "devices", and a section is a list - so an object
    # under that key can only be the new form.
    if not isinstance(data.get("devices"), dict):
        return data, None
    revision = data.get("revision")
    # bool is an int in Python; true is no revision.
    if revision is not None and (not isinstance(revision, int) or isinstance(revision, bool)):
        raise WebError("400 Bad Request", "Die Revision muss eine ganze Zahl sein.")
    return data["devices"], revision


def clean_devices(data: object) -> dict[str, list[dict[str, Any]]]:
    """Check what the page wants written to the device file.

    Raises:
        ValueError: with a message for the page when the data cannot be right.
    """
    if not isinstance(data, dict):
        raise ValueError("Die Geräteliste muss ein Objekt sein.")
    sections: dict[str, list[dict[str, Any]]] = {}
    for section, items in data.items():
        if not isinstance(items, list):
            raise ValueError(f"Abschnitt {section}: erwartet wird eine Liste.")
        entries = []
        for index, item in enumerate(items, 1):
            if not isinstance(item, dict):
                raise ValueError(f"Abschnitt {section}, Eintrag {index}: kein Objekt.")
            where = item.get("where")
            # Addresses stay text: "04" is not the number 4.
            if not isinstance(where, str) or not where.strip():
                raise ValueError(f"Abschnitt {section}, Eintrag {index}: "
                                 "die Adresse fehlt oder ist kein Text.")
            if "name" in item and not isinstance(item["name"], str):
                raise ValueError(f"Abschnitt {section}, Eintrag {index}: "
                                 "der Name ist kein Text.")
            entries.append(dict(item, where=where.strip()))
        sections[str(section)] = entries
    return sections


def request_restart(token: str | None = None,
                    opener: Callable[..., Any] = urllib.request.urlopen) -> bool:
    """Ask the Supervisor to restart this add-on.  Blocking - use a thread.

    Returns False when there is no Supervisor to ask or it did not answer.
    """
    token = os.environ.get("SUPERVISOR_TOKEN", "") if token is None else token
    if not token:
        _LOGGER.warning("No Supervisor token - restart the add-on by hand")
        return False
    request = urllib.request.Request(
        SUPERVISOR_API + "/addons/self/restart", method="POST",
        headers={"Authorization": "Bearer " + token})
    try:
        with opener(request, timeout=SUPERVISOR_TIMEOUT):
            pass
    except urllib.error.HTTPError as err:
        # A real answer: the Supervisor refuses (e.g. missing permission).
        _LOGGER.warning("The Supervisor refused the restart: %s", err)
        err.close()                 # the error carries the open response
        return False
    except (urllib.error.URLError, OSError) as err:
        # Often the Supervisor stops us before it answers - that is the restart.
        _LOGGER.debug("restart request ended with: %s", err)
        return False
    return True


# --------------------------------------------------------------------------
# Server
# --------------------------------------------------------------------------

class WebServer:
    """Serves the page and the ``/api`` routes on the ingress port."""

    def __init__(self, handlers: WebHandlers, port: int = INGRESS_PORT, host: str = "0.0.0.0",
                 allowed_peers: Collection[str] | None = DEFAULT_ALLOWED_PEERS) -> None:
        """``allowed_peers=None`` lets everybody in - for tests only."""
        self._handlers = handlers
        self._host = host
        self._port = port
        self._allowed = None if allowed_peers is None else frozenset(allowed_peers)
        self._server: asyncio.Server | None = None
        self._page = b""
        self._log = RepeatLimiter(_LOGGER)

    @property
    def port(self) -> int:
        """The port actually listened on (useful when 0 was asked for)."""
        if self._server is not None and self._server.sockets:
            return int(self._server.sockets[0].getsockname()[1])
        return self._port

    async def start(self) -> None:
        """Read the page and start listening.

        Raises:
            OSError: when the page cannot be read or the port is taken.
        """
        self._page = await asyncio.to_thread(PAGE_FILE.read_bytes)
        self._server = await asyncio.start_server(self._handle, self._host, self._port,
                                                  limit=MAX_HEAD)
        _LOGGER.info("Web interface listening on port %d", self.port)

    def close(self) -> None:
        """Take no new requests; open ones may finish until :meth:`stop`."""
        if self._server is not None:
            self._server.close()

    async def stop(self) -> None:
        """Take no new requests and give open ones up to ``STOP_WAIT`` seconds."""
        server, self._server = self._server, None
        if server is None:
            return
        server.close()
        try:
            async with asyncio.timeout(STOP_WAIT):
                await server.wait_closed()
        except TimeoutError:
            _LOGGER.debug("web connections did not close within %.0fs", STOP_WAIT)

    # ------------------------------------------------------------------ HTTP

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            try:
                # Read first, even from a stranger: closing a socket with an
                # unread request makes the client see a reset, not the answer.
                async with asyncio.timeout(REQUEST_TIMEOUT):
                    method, path, body = await self._read_request(reader)
            except WebError as err:
                await self._respond(writer, err.status, JSON_TYPE,
                                    self._json_bytes({"error": err.text}))
                return
            peer = writer.get_extra_info("peername")
            address = peer[0] if isinstance(peer, tuple) and peer else ""
            if self._allowed is not None and address not in self._allowed:
                self._log.log(f"peer {address}", logging.WARNING,
                              "web interface: refused a request from %s - only Home "
                              "Assistant's ingress may connect", address or "unknown")
                await self._respond(writer, "403 Forbidden", JSON_TYPE,
                                    self._json_bytes({"error": "forbidden"}))
                return
            try:
                status, content_type, payload = await self._route(method, path, body)
            except WebError as err:
                status, content_type = err.status, JSON_TYPE
                payload = self._json_bytes({"ok": False, "error": err.text})
            await self._respond(writer, status, content_type, payload)
        except (TimeoutError, ConnectionError, asyncio.IncompleteReadError,
                asyncio.LimitOverrunError) as err:
            _LOGGER.debug("web request dropped: %s", err or type(err).__name__)
        except Exception as err:  # noqa: BLE001 - outermost handler: the page must get an answer
            # The page asks every three seconds: a lasting bug would otherwise
            # write a traceback per request for hours.
            self._log.log("request failed", logging.ERROR, "web request failed: %s", err,
                          exc_info=True)
            try:
                await self._respond(writer, "500 Internal Server Error", JSON_TYPE,
                                    self._json_bytes({"error": "internal error"}))
            except (ConnectionError, RuntimeError):
                pass
        finally:
            writer.close()
            try:
                async with asyncio.timeout(REQUEST_TIMEOUT):
                    await writer.wait_closed()
            except (TimeoutError, ConnectionError):
                pass

    async def _read_request(self, reader: asyncio.StreamReader) -> tuple[str, str, bytes]:
        try:
            head = await reader.readuntil(b"\r\n\r\n")
        except asyncio.LimitOverrunError as err:
            raise WebError("431 Request Header Fields Too Large", "request too large") from err
        lines = head.decode("latin-1").split("\r\n")
        parts = lines[0].split(" ")
        if len(parts) < 3:
            raise WebError("400 Bad Request", "malformed request")
        method, target = parts[0].upper(), parts[1]
        length = 0
        for line in lines[1:]:
            name, _, value = line.partition(":")
            name = name.strip().lower()
            if name == "transfer-encoding":
                raise WebError("411 Length Required", "chunked requests are not supported")
            if name == "content-length":
                try:
                    length = int(value.strip())
                except ValueError as err:
                    raise WebError("400 Bad Request", "bad Content-Length") from err
        if length < 0:
            raise WebError("400 Bad Request", "bad Content-Length")
        if length > MAX_BODY:
            raise WebError("413 Payload Too Large", "request too large")
        body = await reader.readexactly(length) if length else b""
        return method, self._route_of(target), body

    @staticmethod
    def _route_of(target: str) -> str:
        path = target.split("?", 1)[0].rstrip("/")
        # Ingress prefixes the path; only the tail is ours.
        for route in ROUTES:
            if path.endswith(route):
                return route
        # Any other path is the page itself - but a mistyped API path has to
        # stay an error instead of quietly returning HTML.
        return "/unknown" if "/api/" in path + "/" else "/"

    async def _route(self, method: str, path: str, body: bytes) -> Response:
        handlers = self._handlers
        if path == "/":
            self._expect(method, "GET")
            return "200 OK", HTML_TYPE, self._page
        if path == "/api/schema":
            self._expect(method, "GET")
            return self._json(SCHEMA)
        if path == "/api/status":
            self._expect(method, "GET")
            return self._json(handlers.status())
        if path == "/api/frames":
            self._expect(method, "GET")
            return self._json({"frames": handlers.frames()})
        if path == "/api/devices" and method == "GET":
            return self._json(handlers.devices())
        if path == "/api/devices":
            self._expect(method, "POST")
            # An empty body must never become an empty device file.
            data, revision = split_revision(self._body(body, required=True))
            try:
                sections = clean_devices(data)
            except ValueError as err:
                raise WebError("400 Bad Request", str(err)) from err
            # Checked once more right here, whatever the page did before: an
            # entry the add-on cannot read at its next start must not get
            # into the device file in the first place.
            problems = handlers.check(sections) if handlers.check is not None else []
            if problems:
                return ("422 Unprocessable Content", JSON_TYPE, self._json_bytes({
                    "ok": False, "problems": problems,
                    "error": "Die Geräteliste hat Fehler - nichts gespeichert."}))
            try:
                if revision is None:
                    await handlers.save_devices(sections)
                else:
                    await handlers.save_devices(sections, revision)
            except OSError as err:
                _LOGGER.error("Cannot write the device file: %s", err)
                raise WebError("500 Internal Server Error",
                               "Die Gerätedatei konnte nicht geschrieben werden.") from err
            return self._json({"ok": True})
        if path == "/api/send":
            self._expect(method, "POST")
            frame = self._body(body, required=True).get("frame", "")
            if not isinstance(frame, str):
                raise WebError("400 Bad Request", "Ein Rahmen ist Text.")
            error = await handlers.send_frame(frame)
            return self._json({"ok": not error, "error": error})
        if path == "/api/scan":
            self._expect(method, "POST")
            handlers.scan(self._body(body, required=False).get("deep") is True)
            return self._json({"ok": True})
        if path == "/api/restart":
            self._expect(method, "POST")
            handlers.restart()
            return self._json({"ok": True})
        if path == "/api/inventory" and handlers.inventory is not None:
            self._expect(method, "GET")
            return self._json(handlers.inventory())
        if path == "/api/check" and handlers.check is not None:
            self._expect(method, "POST")
            data = self._body(body, required=True)
            devices = data["devices"] if isinstance(data.get("devices"), dict) else data
            try:
                return self._json({"problems": handlers.check(devices)})
            except ValueError as err:
                raise WebError("400 Bad Request", str(err)) from err
        if path == "/api/purge" and handlers.purge is not None:
            self._expect(method, "POST")
            removed = await handlers.purge()
            if removed is None:
                return self._json({"ok": False, "error":
                                   "Der Broker ließ sich nicht fragen - nichts entfernt."})
            return self._json({"ok": True, "removed": removed})
        raise WebError("404 Not Found", "not found")

    @staticmethod
    def _expect(method: str, allowed: str) -> None:
        if method != allowed:
            raise WebError("405 Method Not Allowed", f"use {allowed}")

    @staticmethod
    def _body(body: bytes, required: bool) -> dict[str, Any]:
        if not body.strip():
            if required:
                raise WebError("400 Bad Request", "Die Anfrage ist leer.")
            return {}
        try:
            data = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as err:
            raise WebError("400 Bad Request", "Die Anfrage ist kein gültiges JSON.") from err
        if not isinstance(data, dict):
            raise WebError("400 Bad Request", "Die Anfrage muss ein JSON-Objekt sein.")
        return data

    @staticmethod
    def _json_bytes(data: object) -> bytes:
        return json.dumps(data, ensure_ascii=False).encode("utf-8")

    def _json(self, data: object) -> Response:
        return "200 OK", JSON_TYPE, self._json_bytes(data)

    @staticmethod
    async def _respond(writer: asyncio.StreamWriter, status: str, content_type: str,
                       payload: bytes) -> None:
        head = (f"HTTP/1.1 {status}\r\n"
                f"Content-Type: {content_type}\r\n"
                f"Content-Length: {len(payload)}\r\n"
                "Cache-Control: no-store\r\n"
                "X-Content-Type-Options: nosniff\r\n"
                "Connection: close\r\n\r\n")
        writer.write(head.encode("latin-1") + payload)
        async with asyncio.timeout(REQUEST_TIMEOUT):
            await writer.drain()

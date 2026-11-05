"""A single TCP session towards the OpenWebNet gateway.

A session is either the *event* session, which hears everything on the bus, or
the *command* session, which asks and switches.  This module knows how to log
in, how to read frames and how to wait for the acknowledgement of a command;
keeping two sessions alive is the job of :mod:`.gateway`.

Remember when measuring: a session that is only ``connect()``-ed delivers
nothing.  Frames are read by :meth:`OwnSession.run` alone.
"""

from __future__ import annotations

import asyncio
import logging
import socket
from collections import OrderedDict
from typing import Callable

from . import auth, messages as msg

_LOGGER = logging.getLogger(__name__)

# Custom log level below DEBUG: every single frame in and out.
TRACE = 5

MessageHandler = Callable[[msg.OwnMessage], None]

AUTH_MODES = ("auto", "none", "open", "hmac")

CONNECT_TIMEOUT = 10.0
HANDSHAKE_TIMEOUT = 10.0
COMMAND_TIMEOUT = 6.0
WRITE_TIMEOUT = 10.0
# The gateway acknowledges frames in the order they arrived.  A request that
# goes out while posted frames still wait for their ACK/NACK would take one of
# those for its own - so it waits this long for them first.
POST_REPLY_WAIT = 1.0
READ_CHUNK = 1024
# A frame is a few dozen characters.  This much data without a "##" is not
# OpenWebNet any more, and keeping it would only grow the buffer for ever.
MAX_BUFFER = 4096

# TCP keepalive for both sessions.  The event session never sends anything, so
# after a power cut of the gateway it would otherwise wait for data that never
# comes - for more than two hours with the operating system defaults.  With
# these values a dead link is noticed after about 90 seconds.  The probes are
# plain TCP between this host and the gateway; nothing of it reaches the bus.
TCP_KEEPALIVE_IDLE = 60
TCP_KEEPALIVE_INTERVAL = 10
TCP_KEEPALIVE_COUNT = 3

# How many distinct frames the "warn only once" memory holds.
_REPEAT_MEMORY = 256


class OwnError(Exception):
    """Protocol level failure: bad handshake, broken or closed connection."""


class OwnAuthError(OwnError):
    """The gateway rejected the session or our credentials."""


class OwnClosedError(OwnError):
    """The gateway closed the connection."""


class _RepeatFilter:
    """Tells whether a frame is reported for the first time.

    An address that does not exist is refused on every refresh.  A warning each
    time would fill the log within days; the first one says everything.
    """

    def __init__(self, size: int = _REPEAT_MEMORY) -> None:
        self._size = size
        self._seen: OrderedDict[str, None] = OrderedDict()

    def first_time(self, key: str) -> bool:
        if key in self._seen:
            self._seen.move_to_end(key)
            return False
        self._seen[key] = None
        if len(self._seen) > self._size:
            self._seen.popitem(last=False)
        return True


def _enable_tcp_keepalive(sock: socket.socket) -> None:
    """Switch on TCP keepalive with short, platform dependent timings."""
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        # Linux calls the idle time TCP_KEEPIDLE, macOS TCP_KEEPALIVE.
        idle = getattr(socket, "TCP_KEEPIDLE", None)
        if idle is None:
            idle = getattr(socket, "TCP_KEEPALIVE", None)
        if idle is not None:
            sock.setsockopt(socket.IPPROTO_TCP, idle, TCP_KEEPALIVE_IDLE)
        interval = getattr(socket, "TCP_KEEPINTVL", None)
        if interval is not None:
            sock.setsockopt(socket.IPPROTO_TCP, interval, TCP_KEEPALIVE_INTERVAL)
        count = getattr(socket, "TCP_KEEPCNT", None)
        if count is not None:
            sock.setsockopt(socket.IPPROTO_TCP, count, TCP_KEEPALIVE_COUNT)
    except OSError as err:  # pragma: no cover - depends on the platform
        _LOGGER.debug("cannot tune TCP keepalive: %s", err)


class OwnSession:
    """One command or event session, including the authentication handshake."""

    def __init__(
        self,
        host: str,
        port: int,
        password: str,
        session_type: str,
        on_message: MessageHandler,
        auth_mode: str = "auto",
        name: str = "session",
    ) -> None:
        if auth_mode not in AUTH_MODES:
            raise ValueError(f"unknown auth mode: {auth_mode}")
        self.host = host
        self.port = port
        self.password = password or ""
        self.session_type = session_type
        self.auth_mode = auth_mode
        self.name = name
        self._on_message = on_message
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._buffer = ""
        self._pending: asyncio.Future[msg.OwnMessage] | None = None
        self._send_lock = asyncio.Lock()
        # Posted frames whose acknowledgement has not arrived yet.
        self._posts_outstanding = 0
        self._posts_drained = asyncio.Event()
        self._posts_drained.set()
        self._ready = False
        self._refused = _RepeatFilter()
        self._unanswered = _RepeatFilter()

    # ----------------------------------------------------------------- setup

    @property
    def connected(self) -> bool:
        """True once the socket is up *and* the handshake succeeded."""
        return (
            self._ready
            and self._writer is not None
            and not self._writer.is_closing()
        )

    async def connect(self) -> None:
        """Open the socket and log in.  Cleans up after itself on failure."""
        await self.close()
        _LOGGER.debug("[%s] connecting to %s:%s", self.name, self.host, self.port)
        self._buffer = ""
        async with asyncio.timeout(CONNECT_TIMEOUT):
            self._reader, self._writer = await asyncio.open_connection(
                self.host, self.port)
        sock = self._writer.get_extra_info("socket")
        if sock is not None:
            _enable_tcp_keepalive(sock)
        try:
            await self._handshake()
        except OwnClosedError as err:
            await self.close()
            # Measured on an MH201: the fifth session is accepted and closed at
            # once, which looks exactly like a break in the middle of the login.
            raise OwnClosedError(
                "gateway closed the connection during login - an MH201 allows "
                "only four sessions at a time; are other OpenWebNet clients "
                "(MyHOME_Suite, a second bridge) connected?"
            ) from err
        except BaseException:
            await self.close()
            raise
        self._ready = True
        # debug only: the gateway reports "link is up" once both sessions stand.
        _LOGGER.debug("[%s] session established with %s:%s",
                      self.name, self.host, self.port)

    async def close(self) -> None:
        """Close the socket.  Safe to call at any time, also twice."""
        self._ready = False
        self._reset_posts()
        writer, self._writer, self._reader = self._writer, None, None
        if writer is None:
            return
        if not writer.is_closing():
            writer.close()
        try:
            async with asyncio.timeout(WRITE_TIMEOUT):
                await writer.wait_closed()
        except (OSError, TimeoutError):
            # The socket is gone either way; nothing left to clean up.
            pass

    # ------------------------------------------------------------- handshake

    async def _handshake(self) -> None:
        first = await self._expect_frame()
        if not first.is_ack:
            raise OwnError(f"unexpected greeting from gateway: {first.raw}")

        await self._write(self.session_type)
        answer = await self._expect_frame()

        if answer.is_ack:
            _LOGGER.debug("[%s] gateway accepted us without a password", self.name)
            return
        if answer.is_nack:
            raise OwnAuthError(
                "gateway refused the session - is the add-on's IP allowed in "
                "the gateway's IP range / OPEN parameters?"
            )

        raw = answer.raw
        if raw in (msg.HMAC_SHA1_CHALLENGE, msg.HMAC_SHA256_CHALLENGE):
            if self.auth_mode in ("open", "none"):
                raise OwnAuthError(
                    f"gateway wants HMAC auth but '{self.auth_mode}' was forced")
            algorithm = "sha1" if raw == msg.HMAC_SHA1_CHALLENGE else "sha256"
            await self._hmac_handshake(algorithm)
            return

        nonce = raw[2:-2]
        if raw.startswith("*#") and nonce.isascii() and nonce.isdigit():
            if self.auth_mode in ("hmac", "none"):
                raise OwnAuthError(
                    f"gateway wants OPEN password auth but '{self.auth_mode}' was forced")
            await self._open_password_handshake(nonce)
            return

        raise OwnError(f"unsupported authentication challenge: {raw}")

    async def _open_password_handshake(self, nonce: str) -> None:
        if not self.password:
            raise OwnAuthError("gateway asks for an OPEN password but none is configured")
        try:
            answer = auth.open_password(self.password, nonce)
        except ValueError as err:
            raise OwnAuthError(str(err)) from err
        _LOGGER.debug("[%s] answering OPEN password challenge", self.name)
        await self._write(f"*#{answer}##")
        result = await self._expect_frame()
        if not result.is_ack:
            raise OwnAuthError("gateway rejected the OPEN password")

    async def _hmac_handshake(self, algorithm: str) -> None:
        if not self.password:
            raise OwnAuthError("gateway asks for HMAC authentication but no password is set")
        _LOGGER.debug("[%s] starting %s HMAC handshake", self.name, algorithm)
        handshake = auth.HmacAuth(self.password, algorithm)

        await self._write(msg.ACK)
        challenge = await self._expect_frame()
        if challenge.is_reply or not challenge.raw.startswith("*#"):
            raise OwnAuthError(f"expected the gateway nonce, got {challenge.raw}")

        try:
            response = handshake.client_response(challenge.raw[2:-2])
        except ValueError as err:
            raise OwnAuthError(f"malformed gateway nonce: {err}") from err
        await self._write(response)

        server = await self._expect_frame()
        if server.is_nack:
            raise OwnAuthError("gateway rejected the password (HMAC)")
        if not handshake.verify_server(server.raw):
            raise OwnAuthError("the gateway could not prove it knows the password")
        await self._write(msg.ACK)

    # ------------------------------------------------------------------- I/O

    async def _write(self, frame: str) -> None:
        writer = self._writer
        if writer is None or writer.is_closing():
            raise OwnError("session is not connected")
        _LOGGER.log(TRACE, "[%s] >>> %s", self.name, frame)
        writer.write(frame.encode("ascii"))
        try:
            async with asyncio.timeout(WRITE_TIMEOUT):
                await writer.drain()
        except TimeoutError as err:
            raise OwnError("the gateway does not take any data") from err

    async def _read_frame(self, timeout: float | None) -> msg.OwnMessage:
        """Read the next complete frame, refilling the buffer when needed."""
        while True:
            frames, self._buffer = msg.split_frames(self._buffer)
            if frames:
                # Keep the leftovers for the next call.
                self._buffer = "".join(frames[1:]) + self._buffer
                _LOGGER.log(TRACE, "[%s] <<< %s", self.name, frames[0])
                return msg.parse(frames[0])
            if len(self._buffer) > MAX_BUFFER:
                self._buffer = ""
                raise OwnError("gateway sends data that is not OpenWebNet")
            reader = self._reader
            if reader is None:
                raise OwnError("session is not connected")
            async with asyncio.timeout(timeout):
                chunk = await reader.read(READ_CHUNK)
            if not chunk:
                if self._reader is not reader:
                    # close() ran meanwhile (keepalive timeout, failed send):
                    # blaming the gateway would send the reader of the log astray.
                    raise OwnError("connection closed locally")
                raise OwnClosedError("gateway closed the connection")
            self._buffer += chunk.decode("ascii", errors="ignore")

    async def _expect_frame(self) -> msg.OwnMessage:
        return await self._read_frame(HANDSHAKE_TIMEOUT)

    # -------------------------------------------------------------- run loop

    async def run(self) -> None:
        """Read frames until the connection breaks; dispatch them to the handler.

        Always ends with an exception - :class:`OwnError` or :class:`OSError`
        when the link breaks, ``CancelledError`` when it is stopped.
        """
        try:
            while True:
                message = await self._read_frame(timeout=None)
                if message.is_reply:
                    if self._posts_outstanding:
                        # The acknowledgement of a posted frame, not ours.
                        self._posts_outstanding -= 1
                        if not self._posts_outstanding:
                            self._posts_drained.set()
                    else:
                        pending = self._pending
                        if pending is not None and not pending.done():
                            pending.set_result(message)
                            continue
                try:
                    self._on_message(message)
                except Exception:  # noqa: BLE001 - a broken handler must not kill the link
                    _LOGGER.exception("[%s] error while handling %s", self.name, message.raw)
        finally:
            # `connected` turns False the moment the read loop dies, not only
            # once the supervisor gets around to calling close().
            self._ready = False
            # Whoever waits for an acknowledgement learns about the break now
            # instead of after the full command timeout.
            pending = self._pending
            if pending is not None and not pending.done():
                pending.set_exception(OwnClosedError("the session ended"))

    async def request(
        self, frame: str, timeout: float = COMMAND_TIMEOUT
    ) -> msg.OwnMessage | None:
        """Send a frame and return the ACK/NACK that closes the exchange.

        ``None`` means the gateway did not answer at all.  A NACK is returned
        as a message: the command failed but the link is healthy.  A broken
        link raises :class:`OwnError` or :class:`OSError`.
        """
        async with self._send_lock:
            await self._wait_for_posted_replies()
            pending: asyncio.Future[msg.OwnMessage] = (
                asyncio.get_running_loop().create_future())
            self._pending = pending
            try:
                await self._write(frame)
                try:
                    async with asyncio.timeout(timeout):
                        return await pending
                except TimeoutError:
                    level = logging.WARNING if self._unanswered.first_time(frame) \
                        else logging.DEBUG
                    _LOGGER.log(level, "[%s] no answer for %s", self.name, frame)
                    return None
            finally:
                self._pending = None
                if pending.done() and not pending.cancelled():
                    # Mark a failure set by run() as retrieved, it was handled.
                    pending.exception()

    async def post(self, frame: str) -> None:
        """Send a frame without waiting for its acknowledgement.

        A scan asks dozens of addresses that may not exist.  Waiting for each
        answer would stall two seconds per silent address, so the probes go out
        at a fixed pace instead and the replies are picked up by the normal
        message handler.
        """
        async with self._send_lock:
            # Counted before writing: the acknowledgement may arrive while
            # drain() still waits.
            self._posts_outstanding += 1
            self._posts_drained.clear()
            try:
                await self._write(frame)
            except BaseException:
                self._posts_outstanding = max(0, self._posts_outstanding - 1)
                if not self._posts_outstanding:
                    self._posts_drained.set()
                raise

    async def _wait_for_posted_replies(self) -> None:
        """Let the acknowledgements of posted frames arrive first.

        Called with the send lock held, so no further post can slip in.  A post
        the gateway never answers must not block commands for ever: after
        POST_REPLY_WAIT the bookkeeping starts afresh.

        Residual risk, accepted on purpose: OpenWebNet has no frame id, so an
        acknowledgement that arrives just *after* this reset (slow rather than
        never) can still be taken for the reply of the next request.  Not
        resetting would be worse - a real reply would be booked as a post's
        and the command would run into its full timeout.  Keep POST_REPLY_WAIT
        well below COMMAND_TIMEOUT.
        """
        if not self._posts_outstanding:
            return
        try:
            async with asyncio.timeout(POST_REPLY_WAIT):
                await self._posts_drained.wait()
        except TimeoutError:
            _LOGGER.debug("[%s] %d posted frames stayed unanswered",
                          self.name, self._posts_outstanding)
            self._reset_posts()

    def _reset_posts(self) -> None:
        self._posts_outstanding = 0
        self._posts_drained.set()

    async def send(self, frame: str, timeout: float = COMMAND_TIMEOUT,
                   quiet: bool = False) -> bool | None:
        """Send a frame.

        ``True`` when the gateway acknowledged it, ``False`` when it refused
        it, and ``None`` when it did not answer at all.  The difference
        matters: a refusal means the gateway is listening and had an opinion,
        silence means it is not keeping up.

        ``quiet`` is for frames whose refusal is an answer, not a problem:
        asking a zone whether it has a fan is allowed to fail.
        """
        reply = await self.request(frame, timeout)
        if reply is None:
            return None
        if reply.is_nack:
            if not quiet and self._refused.first_time(frame):
                _LOGGER.warning("[%s] gateway refused %s", self.name, frame)
            else:
                _LOGGER.debug("[%s] gateway refused %s", self.name, frame)
            return False
        return True

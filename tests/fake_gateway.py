"""A minimal OpenWebNet gateway for the tests, listening on 127.0.0.1 by default.

It speaks all three handshakes a real BTicino gateway can offer, lets a test
push frames into the event sessions and reproduces the measured quirks from
WISSEN.md that the bridge has to survive:

* ``max_sessions``: the fifth session of an MH201 is accepted and closed at once
* ``answers_on_event_session``: an MH201 answers a request on the *event*
  session and only acknowledges it on the command session
* ``idle_timeout``: a command session without traffic is closed after a while
  (about 30 s on real hardware)
"""

from __future__ import annotations

import asyncio
import hashlib

from myhome_scs_mqtt.app.own import auth, messages as msg


class _Client:
    """Buffered reader for one connection."""

    def __init__(self, reader: asyncio.StreamReader) -> None:
        self.reader = reader
        self.buffer = ""

    async def read_frame(self) -> str:
        while True:
            frames, self.buffer = msg.split_frames(self.buffer)
            if frames:
                self.buffer = "".join(frames[1:]) + self.buffer
                return frames[0]
            chunk = await self.reader.read(256)
            if not chunk:
                raise ConnectionError("client went away")
            self.buffer += chunk.decode()


class FakeGateway:
    def __init__(self, password: str = "12345", mode: str = "none") -> None:
        self.password = password
        self.mode = mode                      # none | open | hmac
        self.server: asyncio.AbstractServer | None = None
        self.port = 0
        # Frames received on command sessions, in order.
        self.received: list[str] = []
        # The same with the loop time of arrival - to measure the real pace.
        self.timeline: list[tuple[float, str]] = []
        self.event_writers: list[asyncio.StreamWriter] = []
        self.command_writers: list[asyncio.StreamWriter] = []
        self.sessions: list[str] = []         # session type of every login
        self.logins = 0                       # completed logins
        self.nack_frames: set[str] = set()
        # Frames that get no reply at all - the gateway stays silent.
        self.silent_frames: set[str] = set()
        # frame -> frames the gateway answers with before the ACK
        self.responses: dict[str, list[str]] = {}
        self.max_sessions: int | None = None
        self.refuse_sessions = False
        self.answers_on_event_session = False
        self.idle_timeout: float | None = None
        self._writers: set[asyncio.StreamWriter] = set()

    async def start(self, host: str = "127.0.0.1", port: int = 0) -> int:
        """Listen, by default on a free port of 127.0.0.1; tests/e2e/gateway.py picks both."""
        self.server = await asyncio.start_server(self._handle, host, port)
        self.port = self.server.sockets[0].getsockname()[1]
        return self.port

    async def stop(self) -> None:
        if self.server is not None:
            self.server.close()
            self.drop_all()
            await self.server.wait_closed()
            self.server = None

    @property
    def open_connections(self) -> int:
        return len(self._writers)

    async def emit(self, frame: str) -> None:
        """Push an event frame to every connected event session."""
        for writer in list(self.event_writers):
            writer.write(frame.encode())
            try:
                await writer.drain()
            except ConnectionError:
                pass                          # that session is just going away

    def drop_all(self) -> None:
        """Close every connection - what a gateway reboot looks like."""
        for writer in list(self._writers):
            writer.close()

    # ------------------------------------------------------------- internals

    async def _handle(self, reader: asyncio.StreamReader,
                      writer: asyncio.StreamWriter) -> None:
        if self.max_sessions is not None and len(self._writers) >= self.max_sessions:
            # MH201: accepted and closed at once, no greeting, no refusal.
            writer.close()
            return
        self._writers.add(writer)
        # One buffer per connection: a client may pack several frames into a
        # single TCP segment, and a real gateway does not lose the extra ones.
        client = _Client(reader)
        try:
            writer.write(msg.ACK.encode())
            await writer.drain()
            session = (await client.read_frame()).strip()
            self.sessions.append(session)

            if self.refuse_sessions:
                writer.write(msg.NACK.encode())
                await writer.drain()
                return
            if not await self._authenticate(client, writer):
                return
            self.logins += 1

            if session == msg.SESSION_EVENT:
                self.event_writers.append(writer)
                while await reader.read(256):
                    pass
                return

            self.command_writers.append(writer)
            await self._command_loop(client, writer)
        except (ConnectionError, asyncio.IncompleteReadError, TimeoutError):
            pass
        finally:
            for registry in (self.event_writers, self.command_writers):
                if writer in registry:
                    registry.remove(writer)
            self._writers.discard(writer)
            if not writer.is_closing():
                writer.close()

    async def _command_loop(self, client: _Client, writer: asyncio.StreamWriter) -> None:
        while True:
            if self.idle_timeout is None:
                frame = await client.read_frame()
            else:
                async with asyncio.timeout(self.idle_timeout):
                    frame = await client.read_frame()
            self.received.append(frame)
            self.timeline.append((asyncio.get_running_loop().time(), frame))
            if frame in self.silent_frames:
                continue
            for answer in self.responses.get(frame, ()):
                if self.answers_on_event_session:
                    await self.emit(answer)
                else:
                    writer.write(answer.encode())
            reply = msg.NACK if frame in self.nack_frames else msg.ACK
            writer.write(reply.encode())
            await writer.drain()

    async def _authenticate(self, client: _Client, writer: asyncio.StreamWriter) -> bool:
        if self.mode == "none":
            writer.write(msg.ACK.encode())
            await writer.drain()
            return True

        if self.mode == "open":
            nonce = "603346802"
            writer.write(("*#%s##" % nonce).encode())
            await writer.drain()
            answer = (await client.read_frame()).strip()
            expected = "*#%s##" % auth.open_password(self.password, nonce)
            writer.write((msg.ACK if answer == expected else msg.NACK).encode())
            await writer.drain()
            return answer == expected

        # HMAC (SHA-256), computed here independently of HmacAuth.
        writer.write(msg.HMAC_SHA256_CHALLENGE.encode())
        await writer.drain()
        await client.read_frame()  # the client's ACK

        ra = hashlib.sha256(b"server-nonce").hexdigest()
        writer.write(("*#%s##" % auth.hex_to_digits(ra)).encode())
        await writer.drain()

        answer = (await client.read_frame()).strip().strip("*#").split("*")
        rb = auth.digits_to_hex(answer[0])
        client_digest = auth.digits_to_hex(answer[1])
        kab = hashlib.sha256(self.password.encode()).hexdigest()
        expected = hashlib.sha256(
            (ra + rb + "736F70653E" + "636F70653E" + kab).encode()
        ).hexdigest()
        if client_digest != expected:
            writer.write(msg.NACK.encode())
            await writer.drain()
            return False

        server_digest = hashlib.sha256((ra + rb + kab).encode()).hexdigest()
        writer.write(("*#%s##" % auth.hex_to_digits(server_digest)).encode())
        await writer.drain()
        await client.read_frame()  # the client's closing ACK
        return True

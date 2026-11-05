"""One OpenWebNet session against the fake gateway: login, requests, reading."""

from __future__ import annotations

import asyncio
import socket
import unittest

from myhome_scs_mqtt.app.own import messages as msg, session as session_module
from myhome_scs_mqtt.app.own.session import (
    OwnAuthError, OwnClosedError, OwnError, OwnSession,
)

from .fake_gateway import FakeGateway
from .helpers import wait_until


class SessionTestCase(unittest.IsolatedAsyncioTestCase):
    """Starts a fake gateway; every session created is closed afterwards."""

    gateway_mode = "none"

    async def asyncSetUp(self) -> None:
        self.gateway = FakeGateway(password="12345", mode=self.gateway_mode)
        self.port = await self.gateway.start()
        self.sessions: list[OwnSession] = []
        self.tasks: list[asyncio.Task] = []
        self.events: list[msg.OwnMessage] = []

    async def asyncTearDown(self) -> None:
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        for session in self.sessions:
            await session.close()
        await self.gateway.stop()

    def make(self, password: str = "12345", auth_mode: str = "auto",
             session_type: str = msg.SESSION_COMMAND, handler=None) -> OwnSession:
        session = OwnSession("127.0.0.1", self.port, password, session_type,
                             handler or self.events.append, auth_mode, name="test")
        self.sessions.append(session)
        return session

    def run_loop(self, session: OwnSession) -> asyncio.Task:
        task = asyncio.create_task(session.run())
        self.tasks.append(task)
        return task


class TestHandshakes(SessionTestCase):
    async def test_gateway_without_password(self):
        session = self.make()
        await session.connect()
        self.assertTrue(session.connected)
        self.assertEqual(self.gateway.sessions, [msg.SESSION_COMMAND])

    async def test_open_password(self):
        self.gateway.mode = "open"
        session = self.make()
        await session.connect()
        self.assertTrue(session.connected)

    async def test_hmac(self):
        self.gateway.mode = "hmac"
        session = self.make()
        await session.connect()
        self.assertTrue(session.connected)

    async def test_hmac_accepts_an_alphanumeric_password(self):
        self.gateway.mode = "hmac"
        self.gateway.password = "abc123"
        session = self.make(password="abc123")
        await session.connect()
        self.assertTrue(session.connected)

    async def test_wrong_passwords_are_reported(self):
        for mode in ("open", "hmac"):
            with self.subTest(mode=mode):
                self.gateway.mode = mode
                session = self.make(password="99999")
                with self.assertRaises(OwnAuthError):
                    await session.connect()
                self.assertFalse(session.connected)

    async def test_missing_password_is_an_auth_error(self):
        for mode in ("open", "hmac"):
            with self.subTest(mode=mode):
                self.gateway.mode = mode
                with self.assertRaises(OwnAuthError):
                    await self.make(password="").connect()

    async def test_non_numeric_open_password_is_an_auth_error(self):
        # Not a ValueError: the supervisor must treat it like any other
        # credential problem and retry slowly.
        self.gateway.mode = "open"
        with self.assertRaisesRegex(OwnAuthError, "numeric"):
            await self.make(password="secret").connect()

    async def test_refused_session_explains_the_ip_range(self):
        self.gateway.refuse_sessions = True
        with self.assertRaisesRegex(OwnAuthError, "IP"):
            await self.make().connect()

    async def test_forced_modes_must_match_the_gateway(self):
        cases = [("hmac", "open"), ("hmac", "none"), ("open", "hmac"), ("open", "none")]
        for gateway_mode, forced in cases:
            with self.subTest(gateway=gateway_mode, forced=forced):
                self.gateway.mode = gateway_mode
                with self.assertRaises(OwnAuthError):
                    await self.make(auth_mode=forced).connect()

    async def test_a_forced_mode_does_not_matter_without_password_request(self):
        session = self.make(auth_mode="hmac")
        await session.connect()
        self.assertTrue(session.connected)

    def test_unknown_auth_mode_is_refused(self):
        with self.assertRaises(ValueError):
            OwnSession("127.0.0.1", 1, "", msg.SESSION_COMMAND, print, auth_mode="magic")


class TestSessionLimit(SessionTestCase):
    async def test_fifth_session_of_an_mh201(self):
        # WISSEN.md: the fifth session is accepted and closed at once.
        self.gateway.max_sessions = 4
        for _ in range(4):
            await self.make().connect()
        fifth = self.make()
        with self.assertRaises(OwnClosedError) as caught:
            await fifth.connect()
        self.assertIn("four sessions", str(caught.exception))
        self.assertFalse(fifth.connected)

        # Once a place is free again, the same session gets in.
        await self.sessions[0].close()
        self.assertTrue(await wait_until(lambda: self.gateway.open_connections == 3))
        await fifth.connect()
        self.assertTrue(fifth.connected)


class TestRequests(SessionTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.session = self.make()
        await self.session.connect()
        self.runner = self.run_loop(self.session)

    async def test_ack_nack_and_silence(self):
        self.gateway.nack_frames.add("*1*1*99##")
        self.gateway.silent_frames.add("*1*1*98##")
        self.assertTrue(await self.session.send("*1*1*12##"))
        self.assertIs(await self.session.send("*1*1*99##"), False)
        self.assertIsNone(await self.session.send("*1*1*98##", timeout=0.2))
        self.assertEqual(self.gateway.received, ["*1*1*12##", "*1*1*99##", "*1*1*98##"])

    async def test_request_returns_the_closing_message(self):
        reply = await self.session.request("*1*1*12##")
        self.assertTrue(reply.is_ack)
        self.gateway.nack_frames.add("*1*1*13##")
        reply = await self.session.request("*1*1*13##")
        self.assertTrue(reply.is_nack)

    async def test_answers_before_the_ack_reach_the_handler(self):
        self.gateway.responses["*#4*1*0##"] = ["*#4*1*0*0243##"]
        self.assertTrue(await self.session.send("*#4*1*0##"))
        self.assertEqual([m.raw for m in self.events], ["*#4*1*0*0243##"])

    async def test_a_broken_handler_does_not_kill_the_session(self):
        calls = []

        def handler(message):
            calls.append(message.raw)
            if len(calls) == 1:
                raise RuntimeError("boom")

        session = self.make(handler=handler)
        await session.connect()
        self.run_loop(session)
        self.gateway.responses["*#1*12##"] = ["*1*1*12##", "*1*0*12##"]
        with self.assertLogs("myhome_scs_mqtt.app.own.session", "ERROR"):
            self.assertTrue(await session.send("*#1*12##"))
        self.assertEqual(calls, ["*1*1*12##", "*1*0*12##"])
        self.assertTrue(await session.send("*1*1*12##"))

    async def test_post_does_not_wait(self):
        self.gateway.silent_frames.add("*#1*33##")
        loop = asyncio.get_running_loop()
        started = loop.time()
        await self.session.post("*#1*33##")
        self.assertLess(loop.time() - started, 0.1)
        self.assertTrue(await wait_until(lambda: "*#1*33##" in self.gateway.received))

    async def test_a_command_does_not_take_the_ack_of_a_posted_probe(self):
        # A scan posts probes without waiting.  The refusal of a probe that
        # arrives late must not be read as the answer to the next command.
        self.gateway.nack_frames.add("*#25*399##")
        await self.session.post("*#25*399##")
        self.assertTrue(await self.session.send("*1*1*12##"))
        # ... and the refusal itself still reaches the handler.
        self.assertTrue(await wait_until(lambda: any(m.is_nack for m in self.events)))

    async def test_an_unanswered_post_blocks_commands_only_briefly(self):
        self.gateway.silent_frames.add("*#25*398##")
        await self.session.post("*#25*398##")
        loop = asyncio.get_running_loop()
        started = loop.time()
        self.assertTrue(await self.session.send("*1*1*12##"))
        elapsed = loop.time() - started
        self.assertGreater(elapsed, session_module.POST_REPLY_WAIT * 0.8)
        self.assertLess(elapsed, session_module.POST_REPLY_WAIT + 1.0)

    async def test_several_frames_in_one_segment(self):
        self.gateway.responses["*#1*0##"] = ["*1*1*11##*1*0*12##", "*1*1*13##"]
        self.assertTrue(await self.session.send("*#1*0##"))
        self.assertEqual([m.raw for m in self.events], ["*1*1*11##", "*1*0*12##", "*1*1*13##"])

    async def test_a_closed_link_ends_run_and_fails_a_waiting_request(self):
        self.gateway.silent_frames.add("*1*1*12##")
        request = asyncio.create_task(self.session.send("*1*1*12##", timeout=5.0))
        self.assertTrue(await wait_until(lambda: "*1*1*12##" in self.gateway.received))
        loop = asyncio.get_running_loop()
        started = loop.time()
        self.gateway.drop_all()
        with self.assertRaises(OwnClosedError):
            await self.runner
        with self.assertRaises(OwnError):
            await request
        self.assertLess(loop.time() - started, 1.0, "the request waited for its timeout")

    async def test_connected_becomes_false_once_the_read_loop_dies(self):
        self.gateway.drop_all()
        with self.assertRaises(OwnClosedError):
            await self.runner
        self.assertFalse(self.session.connected)

    async def test_writing_to_a_closed_session_raises(self):
        await self.session.close()
        self.assertFalse(self.session.connected)
        with self.assertRaises(OwnError):
            await self.session.send("*1*1*12##")

    async def test_refusals_warn_once_per_frame(self):
        self.gateway.nack_frames.add("*#1*77##")
        with self.assertLogs("myhome_scs_mqtt.app.own.session", "DEBUG") as logs:
            for _ in range(3):
                self.assertIs(await self.session.send("*#1*77##"), False)
        warnings = [r for r in logs.records if r.levelname == "WARNING"]
        self.assertEqual(len(warnings), 1)

    async def test_quiet_refusals_never_warn(self):
        self.gateway.nack_frames.add("*#4*1*60##")
        with self.assertLogs("myhome_scs_mqtt.app.own.session", "DEBUG") as logs:
            self.assertIs(await self.session.send("*#4*1*60##", quiet=True), False)
        self.assertFalse([r for r in logs.records if r.levelname == "WARNING"])


class TestEventSession(SessionTestCase):
    async def test_events_are_dispatched(self):
        session = self.make(session_type=msg.SESSION_EVENT)
        await session.connect()
        self.run_loop(session)
        self.assertTrue(await wait_until(lambda: self.gateway.event_writers))
        await self.gateway.emit("*1*1*12##")
        await self.gateway.emit("*#4*1*0*0243##")
        self.assertTrue(await wait_until(lambda: len(self.events) == 2))
        self.assertEqual(self.events[1].values, ("0243",))


class TestRobustness(SessionTestCase):
    async def test_tcp_keepalive_is_switched_on(self):
        session = self.make()
        await session.connect()
        sock = session._writer.get_extra_info("socket")
        self.assertTrue(sock.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE))
        interval = getattr(socket, "TCP_KEEPINTVL", None)
        if interval is not None:
            self.assertEqual(sock.getsockopt(socket.IPPROTO_TCP, interval),
                             session_module.TCP_KEEPALIVE_INTERVAL)

    async def test_garbage_without_frame_end_is_not_buffered_for_ever(self):
        session = self.make(session_type=msg.SESSION_EVENT)
        await session.connect()
        runner = self.run_loop(session)
        self.assertTrue(await wait_until(lambda: self.gateway.event_writers))
        writer = self.gateway.event_writers[0]
        writer.write(b"x" * (session_module.MAX_BUFFER + 10))
        await writer.drain()
        with self.assertRaisesRegex(OwnError, "not OpenWebNet"):
            await runner

    async def test_reconnect_after_close(self):
        session = self.make()
        await session.connect()
        await session.close()
        await session.close()                 # twice is fine
        await session.connect()
        self.assertTrue(session.connected)
        self.assertEqual(self.gateway.logins, 2)

    async def test_closing_locally_is_reported_distinctly(self):
        # A close of our own (keepalive timeout, failed send) must not be
        # logged as a gateway problem - that would send the search astray.
        session = self.make()
        await session.connect()
        runner = self.run_loop(session)
        # An acknowledged command proves the read loop is really reading.
        self.assertTrue(await session.send("*1*1*12##"))
        await session.close()
        with self.assertRaisesRegex(OwnError, "connection closed locally") as caught:
            await runner
        self.assertNotIsInstance(caught.exception, OwnClosedError)

    async def test_connection_refused_is_an_oserror(self):
        await self.gateway.stop()
        with self.assertRaises(OSError):
            await self.make().connect()


if __name__ == "__main__":
    unittest.main()

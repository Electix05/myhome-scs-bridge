"""The diagnostic tool - what you point at a gateway before anything else."""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest
from unittest import mock

import yaml

from myhome_scs_mqtt.app import probe as probe_module
from myhome_scs_mqtt.app.probe import Probe
from myhome_scs_mqtt.app.scan import ScanOptions

from .fake_gateway import FakeGateway

INSTALLATION = {
    "*#13**16##": ["*#13**16*2*15*0*4##"],
    "*#13**15##": ["*#13**15*200##"],
    "*#1*0##": ["*1*1*12##", "*1*0*13##", "*1*8*14##"],
    "*#1*2##": ["*1*0*21##"],
    "*#2*0##": ["*2*0*31##"],
    "*#2*31*10##": ["*#2*31*10*10*50*001*0##"],
    "*#4*1*0##": ["*#4*1*0*0213##"],
    "*#18*51*113##": ["*#18*51*113*0000850##"],
    "*#25*32##": ["*25*31*32##"],
}


class ProbeTestCase(unittest.IsolatedAsyncioTestCase):
    mode = "hmac"

    async def asyncSetUp(self) -> None:
        self.gateway = FakeGateway(password="12345", mode=self.mode)
        self.port = await self.gateway.start()
        self.gateway.responses = dict(INSTALLATION)
        self._tmp = tempfile.TemporaryDirectory()
        self.lines: list[str] = []

    async def asyncTearDown(self) -> None:
        await self.gateway.stop()
        self._tmp.cleanup()

    def probe(self, password: str = "12345") -> Probe:
        return Probe("127.0.0.1", self.port, password, "auto", pace=0.01, settle=0.3,
                     printer=self.lines.append)

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


class TestCheck(ProbeTestCase):
    async def test_check_reports_the_gateway_identity(self):
        self.assertTrue(await self.probe().check())
        self.assertIn("Anmeldung erfolgreich", self.text)
        self.assertIn("Firmware: 2.15.0.4", self.text)
        self.assertIn("Kennung:  200", self.text)
        self.assertTrue(await self._wait_closed())

    async def test_check_explains_a_wrong_password(self):
        self.assertFalse(await self.probe(password="99999").check())
        self.assertIn("FEHLER bei der Anmeldung", self.text)

    async def test_no_gateway_no_crash(self):
        await self.gateway.stop()
        self.assertFalse(await self.probe().check())
        self.assertIn("Keine Verbindung", self.text)

    async def test_both_sessions_are_opened(self):
        self.assertTrue(await self.probe().check())
        self.assertEqual(sorted(self.gateway.sessions), ["*99*0##", "*99*1##"])

    async def _wait_closed(self) -> bool:
        for _ in range(100):
            if self.gateway.open_connections == 0:
                return True
            await asyncio.sleep(0.02)
        return False


class TestCheckOnAnMh201(ProbeTestCase):
    mode = "open"

    async def test_answers_on_the_event_session_are_seen(self):
        # WISSEN.md: an MH201 acknowledges on the command session and answers
        # on the event session - a probe with one session would see nothing.
        self.gateway.answers_on_event_session = True
        self.assertTrue(await self.probe().check())
        self.assertIn("Firmware: 2.15.0.4", self.text)


class TestScan(ProbeTestCase):
    async def test_scan_finds_the_installation_and_writes_a_template(self):
        report = os.path.join(self._tmp.name, "scan_result.yaml")
        self.assertTrue(await self.probe().scan(ScanOptions(zones=2, meters=1, contacts=2),
                                                report))
        self.assertIn("Gefunden: 8 Geräte", self.text)
        with open(report, encoding="utf-8") as handle:
            parsed = yaml.safe_load(handle)
        self.assertEqual([item["where"] for item in parsed["lights"]], ["12", "13", "14", "21"])
        self.assertTrue(parsed["lights"][2]["dimmer"])          # answered with WHAT 8
        self.assertEqual(parsed["covers"][0]["where"], "31")
        self.assertEqual(parsed["climate"][0]["where"], "1")
        self.assertEqual(parsed["energy"][0]["where"], "51")
        self.assertEqual(parsed["binary_sensors"][0]["where"], "2")     # bus address 32
        # The second pass asks shutters for their position and lights for a level.
        self.assertIn("*#2*31*10##", self.gateway.received)
        self.assertIn("*#1*12*1##", self.gateway.received)

    async def test_the_scan_only_reads(self):
        await self.probe().scan(ScanOptions(zones=2, meters=1, contacts=2),
                                os.path.join(self._tmp.name, "r.yaml"))
        self.assertTrue(self.gateway.received)
        self.assertEqual([f for f in self.gateway.received if not f.startswith("*#")], [])

    async def test_a_report_that_cannot_be_written_is_printed(self):
        missing = os.path.join(self._tmp.name, "no", "such", "dir", "r.yaml")
        self.assertTrue(await self.probe().scan(ScanOptions(zones=1, meters=0, contacts=0),
                                                missing))
        self.assertIn("Konnte", self.text)
        self.assertIn("lights:", self.text)


class TestMonitor(ProbeTestCase):
    async def test_monitor_shows_frames_with_a_hint(self):
        probe = self.probe()
        watcher = asyncio.create_task(probe.monitor(1.5))
        for _ in range(50):
            if self.gateway.event_writers:
                break
            await asyncio.sleep(0.02)
        await self.gateway.emit("*25*21#3*22##")
        await self.gateway.emit("*1*1*77##")
        self.assertTrue(await watcher)
        self.assertIn("*25*21#3*22##", self.text)
        self.assertIn("Taster (CEN / CEN+), WHERE=22", self.text)
        self.assertIn("Licht und Dimmer, WHERE=77", self.text)


class TestCommandLine(ProbeTestCase):
    async def test_the_password_comes_from_the_environment_only(self):
        parser = probe_module.build_parser()
        with self.assertRaises(SystemExit), mock.patch.object(sys, "stderr"):
            parser.parse_args(["--host", "192.0.2.10", "--password", "12345"])
        with mock.patch.dict(os.environ, {"OWN_PASSWORD": "12345"}):
            code = await probe_module.main(["--host", "127.0.0.1", "--port", str(self.port)],
                                           printer=self.lines.append)
        self.assertEqual(code, 0)
        self.assertNotIn("OWN_PASSWORD ist nicht gesetzt", self.text)

    async def test_a_wrong_password_ends_with_exit_code_1(self):
        with mock.patch.dict(os.environ, {"OWN_PASSWORD": "99999"}):
            code = await probe_module.main(["--host", "127.0.0.1", "--port", str(self.port)],
                                           printer=self.lines.append)
        self.assertEqual(code, 1)

    def test_the_tool_needs_no_extra_packages(self):
        # Runs on any computer with Python: no paho-mqtt, no PyYAML.
        import ast
        with open(probe_module.__file__, encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        imported = {alias.name.split(".")[0] for node in ast.walk(tree)
                    if isinstance(node, ast.Import) for alias in node.names}
        imported |= {node.module.split(".")[0] for node in ast.walk(tree)
                     if isinstance(node, ast.ImportFrom) and node.module and node.level == 0}
        self.assertFalse(imported & {"paho", "yaml"}, imported)
        for module in ("own/session.py", "own/messages.py", "own/auth.py", "scan.py"):
            path = os.path.join(os.path.dirname(probe_module.__file__), module)
            with open(path, encoding="utf-8") as handle:
                source = handle.read()
            self.assertNotIn("import yaml", source, module)
            self.assertNotIn("paho", source, module)


if __name__ == "__main__":
    unittest.main()

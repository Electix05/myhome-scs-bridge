"""Diagnostic tool: checks the gateway without Home Assistant and without MQTT.

    python3 -m app.probe --host 192.0.2.10               # check the login
    python3 -m app.probe --host 192.0.2.10 --scan        # ask the installation
    python3 -m app.probe --host 192.0.2.10 --monitor 60  # record the bus

The password is never taken as an argument but read from the environment
variable OWN_PASSWORD - so it shows neither in the process list nor in the
shell history.  The scan only sends status requests and switches nothing.

Like the add-on, the tool opens both sessions: an MH201 answers a request on
the *event* session and only acknowledges it on the command session
(WISSEN.md) - with a command session alone it would see no data at all.  Mind
the session limit: an MH201 takes four sessions, and a running add-on already
holds two of them.

Needs nothing but Python: neither paho-mqtt nor PyYAML is imported.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from typing import Any, Callable

from .own import messages as msg
from .own.session import TRACE, OwnAuthError, OwnError, OwnSession
from .scan import (
    DIM_LIGHT_LEVEL, DIM_SHUTTER_STATUS, SECTION_COVERS, SECTION_LIGHTS, SECTION_TITLES,
    ScanOptions, build_plan, classify, plan_size, render_devices_yaml,
)

PASSWORD_VARIABLE = "OWN_PASSWORD"
DEFAULT_PORT = 20000
# The pace of the add-on's scan (CHANGELOG 1.1.2): a busy gateway refuses
# requests in rows when they come faster.
PROBE_PACE = 0.3
# Slow actuators answer late; the scan listens this long after the last probe.
SETTLE = 3.0
ANSWER_TIMEOUT = 2.0
ANSWER_WAIT = 0.3

# WHO=13 dimensions of the gateway.  Dimension 15 answers "200" on an F461 and
# on an MH201 alike - an identifier, not a model; the firmware tells them apart.
DIM_GATEWAY_IP = "10"
DIM_GATEWAY_MAC = "12"
GATEWAY_DIMENSIONS = (
    (msg.DIM_GATEWAY_FIRMWARE, "Firmware"),
    (msg.DIM_GATEWAY_TYPE, "Kennung"),
    (DIM_GATEWAY_IP, "IP"),
    (DIM_GATEWAY_MAC, "MAC"),
)

Printer = Callable[[str], None]


def _print(text: str = "") -> None:
    print(text, flush=True)


class Probe:
    """Login check, scan and bus monitor against one gateway."""

    def __init__(self, host: str, port: int, password: str, auth: str = "auto", *,
                 pace: float = PROBE_PACE, settle: float = SETTLE,
                 printer: Printer = _print) -> None:
        self.host = host
        self.port = port
        self.password = password
        self.auth = auth
        self.pace = pace
        self.settle = settle
        self._print = printer
        # Everything the gateway told us except acknowledgements, in order.
        self.messages: list[msg.OwnMessage] = []
        self._sessions: list[OwnSession] = []
        self._runners: list[asyncio.Task[None]] = []

    # ------------------------------------------------------------- sessions

    def _collect(self, message: msg.OwnMessage) -> None:
        if not message.is_reply:
            self.messages.append(message)

    async def _connect(self, session_type: str, name: str) -> OwnSession | None:
        session = OwnSession(self.host, self.port, self.password, session_type,
                             self._collect, self.auth, name=name)
        try:
            await session.connect()
        except OwnAuthError as err:
            self._print(f"  FEHLER bei der Anmeldung: {err}")
            return None
        except (OwnError, OSError, TimeoutError) as err:
            self._print(f"  Keine Verbindung ({name}): {err or type(err).__name__}")
            self._print("  Prüfe IP-Adresse, Port 20000 und ob der IP-Bereich im Gateway "
                        "freigegeben ist.")
            return None
        self._sessions.append(session)
        self._runners.append(asyncio.create_task(self._read(session), name=f"probe-{name}"))
        return session

    async def _read(self, session: OwnSession) -> None:
        try:
            await session.run()
        except (OwnError, OSError) as err:
            self._print(f"  [{session.name}] Verbindung beendet: {err}")

    async def _open_both(self) -> OwnSession | None:
        """Command session (returned) plus event session; None without login."""
        command = await self._connect(msg.SESSION_COMMAND, "command")
        if command is None:
            return None
        if await self._connect(msg.SESSION_EVENT, "event") is None:
            self._print("  Hinweis: Ohne Ereignis-Sitzung bleibt ein MH201 stumm – er "
                        "antwortet nur dort. Sind schon vier Sitzungen belegt?")
        return command

    async def close(self) -> None:
        runners, self._runners = self._runners, []
        for runner in runners:
            runner.cancel()
        await asyncio.gather(*runners, return_exceptions=True)
        sessions, self._sessions = self._sessions, []
        for session in sessions:
            await session.close()

    # ---------------------------------------------------------------- check

    async def check(self) -> bool:
        """Log in and show what the gateway says about itself."""
        self._print(f"Verbinde mit {self.host}:{self.port} ...")
        command = await self._open_both()
        if command is None:
            await self.close()
            return False
        self._print("  Anmeldung erfolgreich.")
        try:
            for dimension, label in GATEWAY_DIMENSIONS:
                before = len(self.messages)
                frame = msg.dimension_request(msg.WHO_GATEWAY, "", dimension)
                # A gateway without this dimension refuses it - an answer.
                await command.send(frame, timeout=ANSWER_TIMEOUT, quiet=True)
                await asyncio.sleep(ANSWER_WAIT)
                for message in self.messages[before:]:
                    if message.who == msg.WHO_GATEWAY and message.dimension == dimension:
                        self._print(f"  {label + ':':<9} {'.'.join(message.values)}")
        except (OwnError, OSError) as err:
            self._print(f"  Verbindung abgebrochen: {err}")
        finally:
            await self.close()
        return True

    # ----------------------------------------------------------------- scan

    async def scan(self, options: ScanOptions, report_path: str) -> bool:
        """Ask the installation and write a devices.yaml template."""
        command = await self._open_both()
        if command is None:
            await self.close()
            return False
        phases = build_plan(options)
        self._print(f"Scan läuft: {plan_size(phases)} Abfragen "
                    "(nur Statusabfragen, es wird nichts geschaltet)")
        try:
            for phase in phases:
                await self._probe(command, phase.label, phase.frames)
            followup = self._followup_frames()
            if followup:
                await self._probe(command, "Nachprüfung", followup)
            await asyncio.sleep(self.settle)
        except (OwnError, OSError) as err:
            self._print(f"Scan abgebrochen – Verbindung verloren: {err}")
            return False
        finally:
            await self.close()
        await self._report(report_path)
        return True

    async def _probe(self, command: OwnSession, label: str, frames: list[str]) -> None:
        before = len(self.messages)
        for frame in frames:
            await command.post(frame)
            await asyncio.sleep(self.pace)
        self._print(f"  {label:<15} {len(frames):3d} Abfragen, "
                    f"{len(self.messages) - before} Antworten")

    def _followup_frames(self) -> list[str]:
        """Ask every shutter for its position and every light for its level."""
        frames: list[str] = []
        for classified in filter(None, map(classify, self.messages)):
            if classified.section == SECTION_COVERS:
                frame = msg.dimension_request(msg.WHO_AUTOMATION, classified.where,
                                              DIM_SHUTTER_STATUS)
            elif classified.section == SECTION_LIGHTS:
                frame = msg.dimension_request(msg.WHO_LIGHTING, classified.where,
                                              DIM_LIGHT_LEVEL[0])
            else:
                continue
            if frame not in frames:
                frames.append(frame)
        return frames

    def found_devices(self) -> tuple[dict[str, list[dict[str, Any]]], list[str]]:
        """What the answers describe: (sections for devices.yaml, unknown frames)."""
        sections: dict[str, dict[str, dict[str, Any]]] = {}
        unknown: list[str] = []
        for message in self.messages:
            classified = classify(message)
            if classified is None:
                if message.who not in (msg.WHO_GATEWAY, ""):
                    unknown.append(message.raw)
                continue
            title = SECTION_TITLES.get(classified.section, classified.section)
            entry = sections.setdefault(classified.section, {}).setdefault(
                classified.where, {"where": classified.where,
                                   "name": f"{title} {classified.where}"})
            for key, value in classified.extra.items():
                # A device that ever answered as a dimmer stays a dimmer.
                if value or key not in entry:
                    entry[key] = entry.get(key) or value
        # Shorter addresses first, then alphabetically: "5" before "12", while
        # composite values such as "8#1" stay put (int() would raise on them).
        plain = {section: [items[where] for where in sorted(items, key=lambda w: (len(w), w))]
                 for section, items in sections.items()}
        return plain, unknown

    async def _report(self, report_path: str) -> None:
        sections, unknown = self.found_devices()
        found = sum(len(items) for items in sections.values())
        self._print("")
        self._print(f"Gefunden: {found} Geräte")
        for section, items in sections.items():
            title = SECTION_TITLES.get(section, section)
            self._print(f"  {title + ':':<28} {', '.join(item['where'] for item in items)}")
        if unknown:
            self._print("")
            self._print(f"Nicht zugeordnete Frames ({len(unknown)}), die ersten 10:")
            for frame in unknown[:10]:
                self._print(f"  {frame}")
        if not found:
            return
        text = render_devices_yaml(sections, found)
        try:
            await asyncio.to_thread(_write_text, report_path, text)
        except OSError as err:
            self._print(f"Konnte {report_path} nicht schreiben: {err}")
            self._print(text)
            return
        self._print("")
        self._print(f"Vorlage geschrieben: {report_path}")

    # -------------------------------------------------------------- monitor

    async def monitor(self, seconds: float) -> bool:
        """Show what happens on the bus - press a button, read its address."""
        event = await self._connect(msg.SESSION_EVENT, "event")
        if event is None:
            await self.close()
            return False
        self._print(f"Mitschnitt für {seconds:.0f} Sekunden – jetzt Taster drücken, "
                    "Licht schalten ...")
        shown = len(self.messages)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + seconds
        try:
            while loop.time() < deadline:
                await asyncio.sleep(0.2)
                while shown < len(self.messages):
                    message = self.messages[shown]
                    shown += 1
                    hint = ""
                    classified = classify(message)
                    if classified is not None:
                        title = SECTION_TITLES.get(classified.section, classified.section)
                        hint = f"  -> {title}, WHERE={classified.where}"
                    self._print(f"  {message.raw:<28}{hint}")
        finally:
            await self.close()
        return True


def _write_text(path: str, text: str) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python3 -m app.probe",
        description="Prüft ein BTicino/Legrand-OpenWebNet-Gateway. Das Passwort kommt aus "
                    f"der Umgebungsvariable {PASSWORD_VARIABLE}.")
    parser.add_argument("--host", required=True, help="IP-Adresse des Gateways")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--auth", default="auto", choices=["auto", "none", "open", "hmac"])
    parser.add_argument("--scan", action="store_true", help="Anlage abfragen")
    parser.add_argument("--deep", action="store_true", help="jede A/PL-Adresse abfragen")
    parser.add_argument("--zones", type=int, default=20)
    parser.add_argument("--meters", type=int, default=8)
    parser.add_argument("--contacts", type=int, default=20)
    parser.add_argument("--monitor", type=float, default=0, metavar="SEKUNDEN",
                        help="Bus-Verkehr mitschneiden")
    parser.add_argument("--report", default="scan_result.yaml")
    parser.add_argument("--verbose", action="store_true",
                        help="jeden gesendeten und empfangenen Frame anzeigen")
    return parser


async def main(argv: list[str], printer: Printer = _print) -> int:
    """The exit code: 0 when every step worked, 1 otherwise."""
    args = build_parser().parse_args(argv)
    logging.addLevelName(TRACE, "TRACE")
    logging.basicConfig(level=TRACE if args.verbose else logging.ERROR, format="  %(message)s")

    password = os.environ.get(PASSWORD_VARIABLE, "")
    if not password:
        printer(f"Hinweis: {PASSWORD_VARIABLE} ist nicht gesetzt – das klappt nur, wenn das")
        printer(f"Gateway ohne Passwort auskommt. Sonst: export {PASSWORD_VARIABLE}=...")
        printer("")

    probe = Probe(args.host, args.port, password, args.auth, printer=printer)
    if not await probe.check():
        return 1
    ok = True
    if args.scan:
        printer("")
        ok = await probe.scan(ScanOptions(zones=args.zones, meters=args.meters,
                                          contacts=args.contacts, deep=args.deep),
                              args.report) and ok
    if args.monitor:
        printer("")
        ok = await probe.monitor(args.monitor) and ok
    return 0 if ok else 1


def run() -> None:
    try:
        sys.exit(asyncio.run(main(sys.argv[1:])))
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    run()

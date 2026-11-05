"""Wires gateway, registry, discovery, background tasks, MQTT and the web interface.

The bridge owns the lifecycle (:meth:`Bridge.run` / :meth:`Bridge.stop`), the
decision to create an entity for an unknown address, the bus scan and what the
web interface may ask for.  Reconnecting the gateway is left to
:mod:`..own.gateway` alone.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from typing import Any, Callable

from .. import __version__
from ..devices.base import Context, Entity
from ..devices.sensors import BridgeStatus
from ..mqtt import MqttClient, collect_retained
from ..own import messages as msg
from ..own.gateway import BusPacing, Gateway, RepeatLimiter
from ..scan import ScanOptions, build_plan, classify, plan_size, render_devices_yaml
from ..settings import (
    DEFAULT_INSTANCE_ID, Settings, atomic_write_text, load_devices, write_device_file,
)
from ..web.monitor import BusMonitor
from ..web.server import WebHandlers, WebServer, frame_problem, request_restart
from .background import BackgroundTasks
from .discovery import Collector, Discovery
from .registry import FACTORIES, Registry, load_discovered, save_discovered

_LOGGER = logging.getLogger(__name__)

AVAILABLE = "online"
UNAVAILABLE = "offline"
# The name of the add-on's own device in Home Assistant - distinct from the
# old add-on's "MyHome SCS gateway".
GATEWAY_NAME = "MyHome SCS Bridge"
# "Save & restart": the page gets its answer before the Supervisor stops us.
RESTART_DELAY = 0.5
_ANSWERS = {True: "acknowledged", False: "refused", None: "no answer"}
# discovered.json is written once changes have settled - a scan creates
# dozens of entities, and every write would block on the disk.
SAVE_DELAY = 2.0
# A scan must not wait the full WAIT_READY for every probe of a lost link.
SCAN_WAIT_READY = 5.0
SCAN_REPORT = "scan_result.yaml"
# How long after a scan the refresh starts again.  Not right away: the scan has
# just asked every device, and the answers are still coming in.
POST_SCAN_REFRESH = 30.0


@dataclass(frozen=True)
class ScanTiming:
    """Pauses of a bus scan.

    Probes go out without waiting for their acknowledgement (most addresses do
    not exist); the pace keeps the gateway comfortable, the pause between the
    phases gives it a breather, and the settle time lets slow actuators answer.
    """

    probe_pace: float = 0.3
    phase_pause: float = 1.5
    settle: float = 5.0


DEFAULT_SCAN_TIMING = ScanTiming()


class Bridge:
    def __init__(
        self,
        settings: Settings,
        *,
        mqtt_factory: Callable[..., MqttClient] = MqttClient,
        collect: Collector = collect_retained,
        scan_timing: ScanTiming = DEFAULT_SCAN_TIMING,
        gateway_options: dict[str, Any] | None = None,
        web_options: dict[str, Any] | None = None,
        restart: Callable[[], bool] = request_restart,
    ) -> None:
        """``web_options`` go to :class:`WebServer` (tests: port 0, all peers);
        ``restart`` asks the Supervisor for a restart and blocks - it runs in a
        worker thread."""
        self.settings = settings
        base = settings.mqtt.base_topic
        self.availability_topic = f"{base}/bridge/availability"
        self.bridge_topic = f"{base}/bridge/lwt"
        self.scan_timing = scan_timing

        self.context = Context(
            base_topic=base,
            discovery_prefix=settings.mqtt.discovery_prefix,
            availability_topic=self.availability_topic,
            publish=self._publish,
            send=self._send_frame,
            bridge_id=settings.instance_id,
            name_prefix=settings.name_prefix,
            rediscover=self._rediscover,
            queue_depth=self._queue_depth,
            call_later=self._call_later,
            bridge_availability=self.bridge_topic,
            dead_after=settings.dead_after,
        )
        self.registry = Registry(self.context)
        self.status = BridgeStatus(self.context, {"where": settings.gateway.host,
                                                  "name": GATEWAY_NAME})
        self.status.on_scan = self.trigger_scan

        self.mqtt = mqtt_factory(
            settings.mqtt,
            self.bridge_topic,
            on_command=self._handle_command,
            on_homeassistant_online=self._on_homeassistant_online,
        )
        options: dict[str, Any] = {
            # One pacing for every sender: commands, refresh, scan, keepalive.
            "pacing": BusPacing(threshold=settings.brake.threshold,
                                window=settings.brake.window,
                                reserve=settings.command_reserve),
        }
        options.update(gateway_options or {})
        self.gateway = Gateway(
            settings.gateway.host,
            settings.gateway.port,
            settings.gateway.password,
            settings.gateway.auth,
            on_message=self._handle_own,
            on_connected=self._handle_link_state,
            **options,
        )
        self.status.read_counters = lambda: {"refused": self.gateway.refused,
                                             "unanswered": self.gateway.unanswered}
        self.background = BackgroundTasks(
            self.gateway,
            self.registry,
            poll_interval=settings.poll_interval,
            auto_discover=lambda: settings.auto_discover,
            is_scanning=lambda: self.scanning,
        )
        self.discovery = Discovery(self.mqtt, self.registry, settings.mqtt, collect=collect)
        self.monitor = BusMonitor(self.registry.known)
        self.web = WebServer(
            WebHandlers(
                status=self.web_status,
                devices=self.web_devices,
                save_devices=self.web_save_devices,
                frames=self.monitor.frames,
                send_frame=self.web_send_frame,
                scan=self.trigger_scan,
                restart=self.schedule_restart,
            ),
            **(web_options or {}),
        )

        self.inventory_complete = True
        self._log = RepeatLimiter(_LOGGER)
        self._request_restart = restart
        self._restart_task: asyncio.Task[None] | None = None
        self._scan_task: asyncio.Task[None] | None = None
        self._post_scan_task: asyncio.Task[None] | None = None
        self._scanning = False
        self._scanned = False
        self._save_task: asyncio.Task[None] | None = None
        self._save_pending = False
        self._started = False
        self._stopped = False

    # ------------------------------------------------------------- lifecycle

    async def run(self) -> None:
        """Start everything and run until cancelled."""
        self._started = True
        definitions = await asyncio.to_thread(load_devices, self.settings.devices_file)
        discovered: list[dict[str, Any]] = []
        discovered_complete = True
        if self.settings.auto_discover:
            discovered, discovered_complete = await asyncio.to_thread(
                load_discovered, self.settings.discovered_file)
        self.inventory_complete = definitions.complete and discovered_complete

        self.registry.register(self.status)
        self.registry.build(definitions.sections, discovered)
        for topic in self.registry.topics:
            self.mqtt.subscribe(topic)
        # Up before MQTT: while a broker is still missing after a power cut,
        # the page already shows what the add-on waits for.
        await self._start_web()

        await self.mqtt.start()
        self.discovery.publish_all()
        if self.inventory_complete:
            await self.discovery.purge_stale()
        else:
            _LOGGER.warning("Not every device could be read - nothing is removed from "
                            "the broker until the device file is fixed")
        self.mqtt.publish(self.availability_topic, UNAVAILABLE, retain=True, qos=1)
        self.status.publish_state()

        # Only now, on purpose (not before MQTT): the "offline" just above
        # would otherwise overwrite the "online" of a link that came up while
        # the broker was still missing, and the states a first scan collects
        # (QoS 0) would be lost on a client that is not connected yet.
        await self.gateway.start()
        await self.background.run()

    async def stop(self) -> None:
        """Shut down: tasks, offline, gateway sessions, MQTT."""
        if self._stopped:
            return
        self._stopped = True
        await self._cancel(self._scan_task)
        await self._cancel(self._post_scan_task)
        await self._cancel(self._restart_task)
        await self.background.stop_refresh()
        await self._flush_discovered()
        # Offline first: the Supervisor kills the add-on after a few seconds,
        # and a gateway that hangs must not keep Home Assistant believing.
        self.mqtt.publish(self.availability_topic, UNAVAILABLE, retain=True, qos=1)
        # No new web requests; a request that hangs gets its moment only at
        # the very end, after the goodbye to MQTT.
        self.web.close()
        await self.gateway.stop()
        await self.mqtt.stop()
        await self.web.stop()

    @staticmethod
    async def _cancel(task: asyncio.Task[None] | None) -> None:
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _start_web(self) -> None:
        try:
            await self.web.start()
        except OSError as err:
            # The bridge itself works without the page; say why it is missing.
            _LOGGER.error("The web interface cannot start: %s", err)

    # ------------------------------------------------------------ callbacks

    def _publish(self, topic: str, payload: object, retain: bool = True, qos: int = 0) -> None:
        self.mqtt.publish(topic, str(payload), retain=retain, qos=qos)

    def _send_frame(self, frame: str) -> None:
        self.background.queue_frame(frame)

    def _queue_depth(self) -> int:
        return self.background.queue_depth()

    @staticmethod
    def _call_later(delay: float, callback: Callable[[], None]) -> asyncio.TimerHandle:
        return asyncio.get_running_loop().call_later(delay, callback)

    def _handle_command(self, topic: str, payload: str) -> None:
        entity = self.registry.by_topic(topic)
        if entity is None:
            _LOGGER.debug("No entity for command topic %s", topic)
            return
        try:
            entity.on_command(topic, payload)
        except Exception:  # noqa: BLE001 - a broken entity must not stop the bridge
            _LOGGER.exception("Error while handling %s -> %s", topic, payload)

    def _handle_own(self, message: msg.OwnMessage) -> None:
        if message.is_reply:
            return
        self.status.set_last_frame(message.raw)
        self.monitor.record(message)
        targets = self.registry.route(message)
        if not targets and (self.settings.auto_discover or self._scanning):
            entity = self._auto_create(message)
            if entity is not None:
                targets = [entity]
        if not targets:
            _LOGGER.debug("Unhandled frame %s (WHO=%s WHERE=%s)",
                          message.raw, message.who, message.full_where)
            return
        for entity in targets:
            try:
                # A frame from the bus is the best proof that the address is
                # there - a silenced one comes back to life right here.
                entity.note_alive()
                entity.on_own(message)
            except Exception:  # noqa: BLE001 - one broken entity must not stop the rest
                self._log.log(f"own {entity.object_id}", logging.ERROR,
                              "Error while %s handled %s", entity.name, message.raw,
                              exc_info=True)

    def _handle_link_state(self, connected: bool) -> None:
        self.status.set_connected(connected)
        self.mqtt.publish(self.availability_topic, AVAILABLE if connected else UNAVAILABLE,
                          retain=True, qos=1)
        if not connected or self._stopped:
            return
        # Scan only when there would otherwise be nothing to show: a fresh
        # installation configures itself, a known one starts up quickly.
        if (self.settings.scan.on_start and not self._scanned and self.inventory_complete
                and not self.registry.device_entities()):
            _LOGGER.info("No devices known yet - starting an initial bus scan")
            self.trigger_scan()
        else:
            self.background.start_refresh()

    def _on_homeassistant_online(self) -> None:
        """Home Assistant restarted: announce everything again."""
        _LOGGER.info("Home Assistant came back online - re-publishing discovery")
        self.discovery.publish_all()
        self.mqtt.announce_online()
        self.mqtt.publish(self.availability_topic,
                          AVAILABLE if self.gateway.connected else UNAVAILABLE,
                          retain=True, qos=1)
        for entity in self.registry.entities:
            try:
                entity.republish()
            except Exception:  # noqa: BLE001 - keep announcing the others
                _LOGGER.exception("Cannot republish %s", entity.name)

    def _rediscover(self, entity: Entity) -> None:
        """Re-announce an entity that learned something about itself."""
        self.discovery.publish_entity(entity)
        for topic in self.registry.add_topics(entity):
            self.mqtt.subscribe(topic)
        if self.registry.remember(entity):
            self._schedule_save()
        entity.republish()

    # ------------------------------------------------------- auto discovery

    def _auto_create(self, message: msg.OwnMessage) -> Entity | None:
        classified = classify(message)
        if classified is None:
            return None
        entity = self.registry.auto_create(classified)
        if entity is None:
            return None
        # The registry already routes these topics; the broker has to learn
        # them here (subscribe() ignores topics it already has).
        for topic in entity.subscriptions():
            self.mqtt.subscribe(topic)
        self.discovery.publish_entity(entity)
        self._schedule_save()
        _LOGGER.info("Auto-discovered %s at WHERE=%s (%s)",
                     classified.section, classified.where, message.raw)
        return entity

    def _schedule_save(self) -> None:
        if self._stopped:
            # discovered.json was flushed at the start of stop(); a device that
            # shows up now is found again on the next start.
            return
        self._save_pending = True
        if self._save_task is None or self._save_task.done():
            self._save_task = asyncio.create_task(self._save_soon(), name="bridge-save")

    async def _save_soon(self) -> None:
        await asyncio.sleep(SAVE_DELAY)
        await self._write_discovered()

    async def _write_discovered(self) -> None:
        while self._save_pending:
            self._save_pending = False
            items = [dict(item) for item in self.registry.discovered]
            await asyncio.to_thread(save_discovered, self.settings.discovered_file, items)
            self.discovery.publish_directory()

    async def _flush_discovered(self) -> None:
        await self._cancel(self._save_task)
        if self._save_pending:
            await self._write_discovered()

    # -------------------------------------------------------- web interface

    def silenced_devices(self) -> list[str]:
        """Addresses the bridge stopped asking for, as "section address"."""
        return [f"{entity.section} {entity.where}"
                for entity in self.registry.device_entities() if entity.silenced]

    def instance_label(self) -> str:
        """How this instance calls itself - empty when it is the only one.

        With two instances two browser tabs look exactly alike, and a frame
        typed by hand would go to whichever installation happens to be in that
        tab.  The name prefix says it best ("Haus 2"); without one the instance
        id has to do.  Both at their default means a single instance, and then
        the status line stays exactly as it was.
        """
        if self.settings.name_prefix:
            return self.settings.name_prefix
        instance = self.settings.instance_id
        return "" if instance == DEFAULT_INSTANCE_ID else instance

    def web_status(self) -> dict[str, Any]:
        """What the status line of the page shows, and ``/api/status``."""
        return {
            "connected": self.gateway.connected,
            "host": f"{self.settings.gateway.host}:{self.settings.gateway.port}",
            "instance": self.instance_label(),
            "devices": len(self.registry.device_entities()),
            "silenced": self.silenced_devices(),
            "scanning": self.scanning,
            "scan": self.status.scan_state,
            "version": __version__,
            "mqtt_broker": f"{self.settings.mqtt.host}:{self.settings.mqtt.port}",
            "mqtt_connected": self.mqtt.connected,
            "subscriptions": len(self.mqtt.subscriptions),
            **self.background.bus_load(),
        }

    def _device_sections(self) -> dict[str, list[dict[str, Any]]]:
        """Every device as devices.yaml entries, sorted by address per section."""
        sections: dict[str, list[dict[str, Any]]] = {}
        for entity in self.registry.device_entities():
            sections.setdefault(entity.section, []).append(entity.to_config())
        for items in sections.values():
            items.sort(key=lambda item: str(item.get("where", "")))
        return sections

    def web_devices(self) -> dict[str, list[dict[str, Any]]]:
        """Everything the bridge knows - configured and discovered alike."""
        return self._device_sections()

    async def web_save_devices(self, sections: dict[str, list[dict[str, Any]]]) -> None:
        """Write the device file; from then on every device is an explicit entry.

        Raises:
            OSError: when the file cannot be written - the old one stays.
        """
        # Rendering and writing both in a worker thread: the old add-on did it
        # on the event loop, and a slow disk froze the bus meanwhile.
        await asyncio.to_thread(write_device_file, self.settings.devices_file, sections,
                                tuple(FACTORIES))
        # Everything is in the file now; the discovery memory would only
        # produce duplicates on the next start.
        self.registry.forget_discovered()
        await asyncio.to_thread(save_discovered, self.settings.discovered_file, [])
        _LOGGER.info("Device file written from the web interface: %d sections",
                     len([items for items in sections.values() if items]))

    async def web_send_frame(self, frame: str) -> str:
        """Put a hand-written frame on the bus; returns why not, or "".

        Unlike everything else it waits for the gateway: whoever types a frame
        wants to know whether it was taken, not merely that it was queued.
        """
        frame = frame.strip()
        problem = frame_problem(frame)
        if problem:
            return problem
        if not self.gateway.connected:
            return "Das Gateway ist nicht verbunden."
        # quiet: a refusal of an experiment is an answer, not overload.
        answer = await self.gateway.send(frame, quiet=True)
        _LOGGER.info("Frame sent by hand: %s - %s", frame, _ANSWERS.get(answer, answer))
        if answer is None:
            return "Das Gateway hat nicht geantwortet."
        if not answer:
            return "Das Gateway hat den Rahmen abgelehnt."
        return ""

    def schedule_restart(self) -> None:
        """Ask the Supervisor for a restart, a moment after the page got its answer.

        Returns at once.  The request itself blocks for up to ten seconds and
        runs in a worker thread - in the old add-on it ran on the event loop and
        froze the bus for as long as the Supervisor took to answer.
        """
        if self._stopped:
            return
        if self._restart_task is not None and not self._restart_task.done():
            return
        self._restart_task = asyncio.create_task(self._restart_soon(), name="bridge-restart")

    async def _restart_soon(self) -> None:
        await asyncio.sleep(RESTART_DELAY)
        _LOGGER.info("Restart requested from the web interface")
        if not await asyncio.to_thread(self._request_restart):
            # Often simply the restart cutting the answer short - no warning.
            _LOGGER.info("The Supervisor did not confirm the restart - if the add-on "
                         "keeps running, restart it by hand")

    # ------------------------------------------------------------- bus scan

    @property
    def scanning(self) -> bool:
        return self._scan_task is not None and not self._scan_task.done()

    def trigger_scan(self, deep: bool = False) -> None:
        if self._stopped:
            # MQTT still takes commands while the bridge shuts down; a scan
            # started now would never be cancelled.
            return
        if self.scanning:
            _LOGGER.info("A bus scan is already running")
            return
        self._scan_task = asyncio.create_task(self.scan_bus(deep), name="bridge-scan")
        self._scan_task.add_done_callback(self._scan_finished)

    def _scan_finished(self, task: asyncio.Task[None]) -> None:
        if not task.cancelled() and task.exception() is not None:
            self.status.set_scan_state("abgebrochen: Fehler")
            _LOGGER.error("The bus scan failed", exc_info=task.exception())

    async def scan_bus(self, deep: bool = False) -> None:
        """Walk the bus and let every device announce itself.

        Only status and dimension requests are sent - a scan never switches
        anything.  The replies travel the normal routing path, so the auto
        discovery turns them into entities as they arrive.
        """
        self._scanned = True
        if not self.gateway.connected:
            self.status.set_scan_state("kein Gateway verbunden")
            return
        options = ScanOptions(zones=self.settings.scan.zones, meters=self.settings.scan.meters,
                              contacts=self.settings.scan.contacts, deep=deep)
        phases = build_plan(options)
        total = plan_size(phases)
        known_before = {(e.section, e.where) for e in self.registry.device_entities()}
        timing = self.scan_timing

        _LOGGER.info("Bus scan started (%d probes%s)", total, ", deep" if deep else "")
        self._scanning = True
        sent = 0
        try:
            for phase in phases:
                for frame in phase.frames:
                    if not await self._probe(frame):
                        return
                    sent += 1
                    if sent % 5 == 0 or frame is phase.frames[-1]:
                        self.status.set_scan_state(f"läuft: {phase.label} ({sent}/{total})")
                    await asyncio.sleep(timing.probe_pace)
                await asyncio.sleep(timing.phase_pause)
            # Second pass: ask what the devices found so far can do - only a
            # shutter that knows its position answers dimension 10, only a
            # dimmer reports a level.  The entities upgrade themselves.
            followup = list(dict.fromkeys(
                frame for entity in self.registry.device_entities()
                for frame in entity.followup_frames()))
            for index, frame in enumerate(followup, 1):
                if not await self._probe(frame):
                    return
                if index % 5 == 0 or index == len(followup):
                    self.status.set_scan_state(f"läuft: Nachprüfung ({index}/{len(followup)})")
                await asyncio.sleep(timing.probe_pace)
            self.status.set_scan_state("läuft: Auswertung")
            await asyncio.sleep(timing.settle)
        except asyncio.CancelledError:
            self.status.set_scan_state("abgebrochen")
            raise
        finally:
            self._scanning = False
            # Whatever the refresh queued while the scan was running is minutes
            # old by now, and the scan has just asked everything itself.  Left
            # in the queue it ran on for minutes (measured 24.09.2026).
            self.background.drop_queued_requests()
            self._schedule_post_scan_refresh()

        devices = self.registry.device_entities()
        new = [e for e in devices if (e.section, e.where) not in known_before]
        report = await self._write_scan_report(len(new))
        self.status.set_scan_state(f"fertig: {len(new)} neu, {len(devices)} gesamt")
        _LOGGER.info("Bus scan finished: %d new devices, %d in total%s", len(new), len(devices),
                     f" - report in {report}" if report else "")
        # No refresh right here: the scan has just asked every device for its
        # state.  The next round starts delayed, see _schedule_post_scan_refresh
        # (called in the finally above, so an aborted scan is covered too).

    def _schedule_post_scan_refresh(self) -> None:
        """Let the refresh start again a while after the scan, once.

        Not right away: the scan has just asked every device and the answers
        are still arriving.  Starting the round on its heels put several
        hundred more frames on a bus that was still busy.

        A second scan that finishes while the timer still runs takes its place,
        so the quiet period always counts from the *last* scan.  The old timer
        is only cancelled, not awaited: this runs in the ``finally`` of
        :meth:`scan_bus`, and awaiting there could swallow a cancellation that
        is on its way through.
        """
        if self._stopped:
            return
        if self._post_scan_task is not None and not self._post_scan_task.done():
            self._post_scan_task.cancel()
        self._post_scan_task = asyncio.create_task(self._refresh_after_scan(),
                                                   name="bridge-post-scan")

    async def _refresh_after_scan(self) -> None:
        await asyncio.sleep(POST_SCAN_REFRESH)
        if self.gateway.connected and not self.scanning and not self._stopped:
            _LOGGER.debug("Refreshing again after the bus scan")
            self.background.start_refresh()

    async def _probe(self, frame: str) -> bool:
        """Put one scan probe on the bus; False when the link is gone.

        The probe counts as background traffic: it respects the brake and the
        command reserve, so a key press gets through while the scan runs
        (measured 24.09.2026: during a scan thirteen switch attempts were all
        refused).  Its refusal - most scanned addresses do not exist - can
        never move the brake, because ``post`` does not wait for the answer
        and ``OwnSession.run`` books the reply of a posted frame away.
        """
        if await self.gateway.post(frame, wait_ready=SCAN_WAIT_READY, background=True):
            return True
        self.status.set_scan_state("abgebrochen: keine Verbindung zum Gateway")
        _LOGGER.warning("Bus scan aborted - the gateway link is gone")
        return False

    async def _write_scan_report(self, found: int) -> str:
        """Everything known as a ready to use devices.yaml; the path or ''."""
        sections = self._device_sections()
        directory = os.path.dirname(self.settings.devices_file) or "/config"
        path = os.path.join(directory, SCAN_REPORT)
        try:
            await asyncio.to_thread(atomic_write_text, path, render_devices_yaml(sections, found))
        except OSError as err:
            _LOGGER.warning("Cannot write the scan report to %s: %s", path, err)
            return ""
        return path

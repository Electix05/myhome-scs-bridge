"""Thin asyncio wrapper around paho-mqtt.

paho runs its network loop in a thread of its own and reconnects by itself.
Whatever arrives there is handed to the event loop with
``call_soon_threadsafe``; nothing in here blocks the event loop.

Rules this module keeps:

* The add-on announces itself with a retained ``online`` and registers a last
  will ``offline`` on the same topic.
* Retained messages (states, names, discovery) go out only when they changed.
  Events are never suppressed - a second press is a second press.
* Home Assistant's birth message on ``<discovery_prefix>/status`` triggers a
  full re-announcement.
* A *retained* message on a command topic is ignored: the broker would replay
  it after every reconnect and switch the light again.
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import threading
import time
from typing import Callable, Iterable

from paho.mqtt import client as paho
from paho.mqtt.enums import MQTTErrorCode

from .settings import MqttConfig

_LOGGER = logging.getLogger(__name__)

TRACE = 5

CommandHandler = Callable[[str, str], None]

KEEPALIVE = 60
RECONNECT_MIN_DELAY = 1
RECONNECT_MAX_DELAY = 30
# While the broker cannot be reached at start-up, remind of it this often.
FIRST_WARNING_AFTER = 30.0
WARNING_INTERVAL = 300.0
OFFLINE_PUBLISH_TIMEOUT = 2.0
STOP_TIMEOUT = 5.0
SUBSCRIBE_BATCH = 100
# Taking stock of retained topics: done once no message came for this long.
INVENTORY_QUIET = 0.5


def mqtt_client_id(instance_id: str, role: str = "", hostname: str | None = None,
                   pid: int | None = None) -> str:
    """A client id that no second client of ours shares.

    A broker throws the first client out the moment a second one logs in with
    the same id - the two then kick each other off in an endless loop.  Two
    instances of this add-on (one per MyHome installation) can run on the same
    machine, so the host name alone is not enough to tell them apart; the
    instance id is.  Underscores become hyphens, which keeps the id of a
    default instance exactly what earlier versions used.
    """
    parts = [instance_id.replace("_", "-")]
    if role:
        parts.append(role)
    parts.append(socket.gethostname() if hostname is None else hostname)
    if pid is not None:
        parts.append(str(pid))
    return "-".join(parts)


def collect_retained(config: MqttConfig, patterns: Iterable[str],
                     seconds: float = 3.0) -> list[str] | None:
    """List the retained topics under the given patterns.

    Blocking - run it in a worker thread.  Uses a connection of its own:
    subscribing to a wildcard on the main client would make the broker deliver
    every command twice.  Returns None when the broker could not be asked.
    """
    patterns = list(patterns)
    found: set[str] = set()
    lock = threading.Lock()
    subscribed = threading.Event()
    failed = threading.Event()
    last_activity = time.monotonic()

    client = paho.Client(
        paho.CallbackAPIVersion.VERSION2,
        client_id=mqtt_client_id(config.instance_id, "inventory", pid=os.getpid()),
    )
    if config.username:
        client.username_pw_set(config.username, config.password)
    if config.ssl:
        client.tls_set()

    def on_connect(cl, _userdata, _flags, reason_code, _properties=None):
        if reason_code.is_failure:
            _LOGGER.warning("Cannot take stock of the broker: %s", reason_code)
            failed.set()
            return
        cl.subscribe([(pattern, 0) for pattern in patterns])

    def on_subscribe(_cl, _userdata, _mid, _reason_codes, _properties=None):
        nonlocal last_activity
        with lock:
            last_activity = time.monotonic()
        subscribed.set()

    def on_message(_cl, _userdata, message):
        nonlocal last_activity
        with lock:
            last_activity = time.monotonic()
            if message.retain and message.payload:
                found.add(message.topic)

    client.on_connect = on_connect
    client.on_subscribe = on_subscribe
    client.on_message = on_message
    # The TCP connect is part of the time budget, too (paho's default is 5 s).
    client.connect_timeout = max(seconds, 0.1)
    try:
        client.connect(config.host, config.port, keepalive=30)
    except OSError as err:
        _LOGGER.warning("Cannot take stock of the broker: %s", err)
        return None

    client.loop_start()
    try:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline and not failed.is_set():
            with lock:
                quiet_long_enough = (subscribed.is_set()
                                     and time.monotonic() - last_activity >= INVENTORY_QUIET)
            if quiet_long_enough:
                break
            time.sleep(0.05)
    finally:
        client.disconnect()
        client.loop_stop()
    if failed.is_set() or not subscribed.is_set():
        if not failed.is_set():
            _LOGGER.warning("Cannot take stock of the broker: no answer within %.0fs", seconds)
        return None
    with lock:
        return sorted(found)


class MqttClient:
    """Publishes states and forwards incoming commands into the event loop."""

    def __init__(
        self,
        config: MqttConfig,
        availability_topic: str,
        on_command: CommandHandler,
        on_homeassistant_online: Callable[[], None] | None = None,
        client_id: str | None = None,
    ) -> None:
        self.config = config
        self.availability_topic = availability_topic
        self.status_topic = f"{config.discovery_prefix}/status"
        self._on_command = on_command
        self._on_ha_online = on_homeassistant_online or (lambda: None)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._connected = asyncio.Event()
        self._lock = threading.Lock()
        self._subscriptions: set[str] = set()
        # Last retained payload per topic; touched in the event loop only.
        self._retained: dict[str, str] = {}
        self._refusal_logged = False
        self._started = False
        self._stopped = False

        # Not "myhome-scs-bridge-...": that is the old add-on's client id, and a
        # broker drops the first client when a second one logs in with its id.
        # The instance id keeps two instances of *this* add-on apart as well.
        self._client = paho.Client(
            paho.CallbackAPIVersion.VERSION2,
            client_id=client_id or mqtt_client_id(config.instance_id),
        )
        if config.username:
            self._client.username_pw_set(config.username, config.password)
        if config.ssl:
            self._client.tls_set()
        self._client.will_set(availability_topic, "offline", qos=1, retain=True)
        self._client.reconnect_delay_set(RECONNECT_MIN_DELAY, RECONNECT_MAX_DELAY)
        self._client.on_connect = self._handle_connect
        self._client.on_disconnect = self._handle_disconnect
        self._client.on_message = self._handle_message

    # ------------------------------------------------------------- lifecycle

    async def start(self, first_warning: float = FIRST_WARNING_AFTER,
                    warning_interval: float = WARNING_INTERVAL) -> None:
        """Connect and wait for the broker - however long that takes.

        After a power cut the broker may start later than this add-on; paho
        keeps trying, and a warning now and then says what we are waiting for.
        """
        self._loop = asyncio.get_running_loop()
        _LOGGER.info("Connecting to MQTT broker %s:%s", self.config.host, self.config.port)
        self._client.connect_async(self.config.host, self.config.port, keepalive=KEEPALIVE)
        self._client.loop_start()
        self._started = True
        loop = asyncio.get_running_loop()
        started = loop.time()
        wait = first_warning
        while not self._connected.is_set():
            try:
                async with asyncio.timeout(wait):
                    await self._connected.wait()
            except TimeoutError:
                _LOGGER.warning(
                    "Still no connection to the MQTT broker at %s:%s after %.0fs - "
                    "check the broker and the credentials",
                    self.config.host, self.config.port, loop.time() - started)
                wait = warning_interval

    async def stop(self) -> None:
        """Say goodbye: ``offline`` first, then disconnect and end the thread."""
        if self._stopped:
            return
        self._stopped = True
        if not self._started:
            return
        info = self._client.publish(self.availability_topic, "offline", qos=1, retain=True)
        try:
            await asyncio.to_thread(info.wait_for_publish, OFFLINE_PUBLISH_TIMEOUT)
        except (ValueError, RuntimeError) as err:
            _LOGGER.debug("offline message not confirmed: %s", err)
        # DISCONNECT first: loop_stop() waits for the network thread, and
        # without a disconnect that thread only wakes up on the keepalive timer.
        self._client.disconnect()
        try:
            async with asyncio.timeout(STOP_TIMEOUT):
                await asyncio.to_thread(self._client.loop_stop)
        except TimeoutError:
            _LOGGER.warning("MQTT network thread did not end in time")

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    @property
    def subscriptions(self) -> list[str]:
        """The command topics this client listens on."""
        with self._lock:
            return sorted(self._subscriptions)

    # ---------------------------------------------------------------- paho glue
    # These run in paho's network thread.

    def _call_in_loop(self, callback: Callable[..., None], *args: object) -> None:
        loop = self._loop
        if loop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(callback, *args)

    def _handle_connect(self, client: paho.Client, _userdata, _flags,
                        reason_code, _properties=None) -> None:
        if reason_code.is_failure:
            if not self._refusal_logged:
                _LOGGER.error("MQTT connection refused: %s - check user and password",
                              reason_code)
                self._refusal_logged = True
            else:
                _LOGGER.debug("MQTT connection refused: %s", reason_code)
            return
        self._refusal_logged = False
        _LOGGER.info("MQTT connected to %s:%s", self.config.host, self.config.port)
        with self._lock:
            topics = sorted(self._subscriptions)
        client.subscribe(self.status_topic, qos=1)
        for start in range(0, len(topics), SUBSCRIBE_BATCH):
            client.subscribe([(topic, 1) for topic in topics[start:start + SUBSCRIBE_BATCH]])
        client.publish(self.availability_topic, "online", qos=1, retain=True)
        self._call_in_loop(self._connected_in_loop)

    def _handle_disconnect(self, _client: paho.Client, _userdata, _flags,
                           reason_code, _properties=None) -> None:
        if self._stopped:
            _LOGGER.debug("MQTT connection closed (%s)", reason_code)
        elif self.connected:
            _LOGGER.warning("MQTT disconnected (%s) - reconnecting", reason_code)
        self._call_in_loop(self._connected.clear)

    def _handle_message(self, _client: paho.Client, _userdata, message) -> None:
        topic = message.topic
        payload = message.payload.decode("utf-8", errors="replace")
        _LOGGER.log(TRACE, "MQTT <<< %s %s", topic, payload)
        if topic == self.status_topic:
            if payload.strip().lower() == "online":
                self._call_in_loop(self._homeassistant_online_in_loop)
            return
        if message.retain:
            # Delivered because we subscribed, not because somebody pressed
            # something right now - replaying it would switch things by itself.
            _LOGGER.warning("Ignoring the retained command %r on %s - commands "
                            "must be published without retain", payload, topic)
            return
        self._call_in_loop(self._on_command, topic, payload)

    # ------------------------------------------------------ event loop side

    def _connected_in_loop(self) -> None:
        # A restarted broker may have lost what it held; the next change of
        # every state has to go out again.
        self._retained.clear()
        self._connected.set()

    def _homeassistant_online_in_loop(self) -> None:
        self._retained.clear()
        self._on_ha_online()

    # ------------------------------------------------------------------ api

    def publish(self, topic: str, payload: str, retain: bool = False, qos: int = 0) -> None:
        """Publish; a retained payload that did not change is not sent again."""
        if retain:
            if self._retained.get(topic) == payload:
                return
            self._retained[topic] = payload
        _LOGGER.log(TRACE, "MQTT >>> %s %s", topic, payload)
        info = self._client.publish(topic, payload, qos=qos, retain=retain)
        if retain and qos == 0 and info.rc == MQTTErrorCode.MQTT_ERR_NO_CONN:
            # QoS 0 is not queued by paho: forget it, so it goes out next time.
            self._retained.pop(topic, None)

    def forget_retained(self) -> None:
        """Send every retained payload again on its next publish."""
        self._retained.clear()

    def subscribe(self, topic: str) -> None:
        with self._lock:
            if topic in self._subscriptions:
                return
            self._subscriptions.add(topic)
        # Without a connection paho refuses; the connect handler subscribes
        # everything in the set anyway.
        self._client.subscribe(topic, qos=1)

    def announce_online(self) -> None:
        """Publish ``online`` again, e.g. after Home Assistant restarted."""
        self._retained.pop(self.availability_topic, None)
        self.publish(self.availability_topic, "online", retain=True, qos=1)

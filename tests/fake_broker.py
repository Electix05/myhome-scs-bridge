"""A very small MQTT 3.1.1 broker on 127.0.0.1, just enough for the paho glue.

It understands CONNECT (including the last will), SUBSCRIBE, PUBLISH (QoS 0/1),
PINGREQ and DISCONNECT, keeps retained messages, hands them out after a
SUBACK with the retain flag set, and publishes the last will of a client that
goes away without saying goodbye.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

CONNECT, CONNACK, PUBLISH, PUBACK = 1, 2, 3, 4
SUBSCRIBE, SUBACK, PINGREQ, PINGRESP, DISCONNECT = 8, 9, 12, 13, 14

CONNACK_ACCEPTED = 0
CONNACK_NOT_AUTHORIZED = 5


def _encode_length(length: int) -> bytes:
    out = bytearray()
    while True:
        byte = length % 128
        length //= 128
        if length:
            byte |= 0x80
        out.append(byte)
        if not length:
            return bytes(out)


def _encode_string(value: str) -> bytes:
    raw = value.encode("utf-8")
    return len(raw).to_bytes(2, "big") + raw


def _read_string(body: bytes, cursor: int) -> tuple[str, int]:
    size = int.from_bytes(body[cursor:cursor + 2], "big")
    cursor += 2
    return body[cursor:cursor + size].decode("utf-8", errors="replace"), cursor + size


def topic_matches(filter_: str, topic: str) -> bool:
    if filter_ == topic:
        return True
    filter_parts = filter_.split("/")
    topic_parts = topic.split("/")
    for index, part in enumerate(filter_parts):
        if part == "#":
            return True
        if index >= len(topic_parts):
            return False
        if part != "+" and part != topic_parts[index]:
            return False
    return len(filter_parts) == len(topic_parts)


@dataclass
class ClientInfo:
    client_id: str = ""
    username: str = ""
    will: tuple[str, str, bool] | None = None      # topic, payload, retain
    subscriptions: list[str] = field(default_factory=list)


class FakeBroker:
    def __init__(self) -> None:
        self.server: asyncio.AbstractServer | None = None
        self.port = 0
        self.messages: list[tuple[str, str, bool]] = []    # (topic, payload, retain)
        self.retained: dict[str, str] = {}
        self.connects: list[ClientInfo] = []
        self.refuse_connect = False
        self._clients: dict[asyncio.StreamWriter, ClientInfo] = {}

    async def start(self, port: int = 0) -> int:
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", port)
        self.port = self.server.sockets[0].getsockname()[1]
        return self.port

    async def stop(self) -> None:
        if self.server is not None:
            self.server.close()
            for writer in list(self._clients):
                writer.close()
            await self.server.wait_closed()
            self.server = None

    def last(self, topic: str) -> str | None:
        for published_topic, payload, _retain in reversed(self.messages):
            if published_topic == topic:
                return payload
        return None

    def count(self, topic: str, payload: str | None = None) -> int:
        return sum(1 for t, p, _r in self.messages
                   if t == topic and (payload is None or p == payload))

    @property
    def subscriptions(self) -> list[str]:
        return [s for info in self._clients.values() for s in info.subscriptions]

    async def publish(self, topic: str, payload: str, retain: bool = False) -> None:
        """Publish from the broker side, as another client would."""
        self._store(topic, payload, retain)
        await self._forward(topic, payload)

    def drop_clients(self) -> None:
        """Cut every connection without DISCONNECT - a broker restart."""
        for writer in list(self._clients):
            writer.close()

    # ------------------------------------------------------------- internals

    def _store(self, topic: str, payload: str, retain: bool) -> None:
        self.messages.append((topic, payload, retain))
        if retain:
            if payload:
                self.retained[topic] = payload
            else:
                self.retained.pop(topic, None)     # an empty retained message deletes

    async def _forward(self, topic: str, payload: str) -> None:
        body = _encode_string(topic) + payload.encode()
        packet = bytes([PUBLISH << 4]) + _encode_length(len(body)) + body
        for writer, info in list(self._clients.items()):
            if any(topic_matches(f, topic) for f in info.subscriptions):
                writer.write(packet)
                try:
                    await writer.drain()
                except ConnectionError:
                    pass

    async def _read_length(self, reader: asyncio.StreamReader) -> int:
        multiplier = 1
        value = 0
        while True:
            byte = (await reader.readexactly(1))[0]
            value += (byte & 127) * multiplier
            if not byte & 0x80:
                return value
            multiplier *= 128

    def _parse_connect(self, body: bytes) -> ClientInfo:
        _protocol, cursor = _read_string(body, 0)
        flags = body[cursor + 1]
        cursor += 4                                   # level, flags, keepalive
        info = ClientInfo()
        info.client_id, cursor = _read_string(body, cursor)
        if flags & 0x04:
            will_topic, cursor = _read_string(body, cursor)
            will_payload, cursor = _read_string(body, cursor)
            info.will = (will_topic, will_payload, bool(flags & 0x20))
        if flags & 0x80:
            info.username, cursor = _read_string(body, cursor)
        return info

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        info = ClientInfo()
        self._clients[writer] = info
        clean_goodbye = False
        try:
            while True:
                header = (await reader.readexactly(1))[0]
                packet_type = header >> 4
                length = await self._read_length(reader)
                body = await reader.readexactly(length) if length else b""

                if packet_type == CONNECT:
                    parsed = self._parse_connect(body)
                    info.client_id, info.username, info.will = (
                        parsed.client_id, parsed.username, parsed.will)
                    self.connects.append(parsed)
                    code = CONNACK_NOT_AUTHORIZED if self.refuse_connect else CONNACK_ACCEPTED
                    writer.write(bytes([CONNACK << 4, 2, 0, code]))
                    if self.refuse_connect:
                        await writer.drain()
                        clean_goodbye = True
                        break
                elif packet_type == SUBSCRIBE:
                    packet_id = body[:2]
                    cursor = 2
                    codes = bytearray()
                    new_filters = []
                    while cursor < len(body):
                        topic_filter, cursor = _read_string(body, cursor)
                        cursor += 1                   # requested QoS
                        info.subscriptions.append(topic_filter)
                        new_filters.append(topic_filter)
                        codes.append(0)
                    payload = packet_id + bytes(codes)
                    writer.write(bytes([SUBACK << 4]) + _encode_length(len(payload)) + payload)
                    # A real broker hands out the retained messages right after
                    # the SUBACK - with the retain flag set.
                    for topic, retained in list(self.retained.items()):
                        if any(topic_matches(f, topic) for f in new_filters):
                            retained_body = _encode_string(topic) + retained.encode()
                            writer.write(bytes([(PUBLISH << 4) | 1])
                                         + _encode_length(len(retained_body)) + retained_body)
                elif packet_type == PUBLISH:
                    qos = (header >> 1) & 3
                    retain = bool(header & 1)
                    topic, cursor = _read_string(body, 0)
                    packet_id = b""
                    if qos:
                        packet_id = body[cursor:cursor + 2]
                        cursor += 2
                    payload = body[cursor:].decode("utf-8", errors="replace")
                    self._store(topic, payload, retain)
                    if qos == 1:
                        writer.write(bytes([PUBACK << 4, 2]) + packet_id)
                    await self._forward(topic, payload)
                elif packet_type == PINGREQ:
                    writer.write(bytes([PINGRESP << 4, 0]))
                elif packet_type == DISCONNECT:
                    clean_goodbye = True
                    break
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            self._clients.pop(writer, None)
            if not writer.is_closing():
                writer.close()
            if not clean_goodbye and info.will is not None:
                topic, payload, retain = info.will
                self._store(topic, payload, retain)
                await self._forward(topic, payload)

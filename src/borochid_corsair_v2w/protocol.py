"""V2W wire format.

Output: 65-byte hidraw writes, ``[0x00 report id, 0x02 marker, endpoint,
command, *payload]`` zero padded. Input reports carry no report id and come
in three shapes, told apart by ``classify``:

* button events   ``03 01 02 <down>``      (only while in software mode)
* status notices  ``03 01 01 <op> 00 <lo> <hi>``  pushed on value change
* replies         ``01 <source> <command> <status> <lo> <hi>``: source is 00
                  for the dongle and 01 for the headset, command echoes the
                  request, status 00 means success

A dongle reply proves nothing about the headset, which is why liveness
checks must look at the source byte.

Protocol knowledge comes from HeadsetControl's corsair_void_v2w and captures
documented in VirtuosoControl.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum

REPORT_SIZE = 65
MARKER = 0x02


class Endpoint(IntEnum):
    RECEIVER = 0x08  # the USB dongle
    HEADSET = 0x09  # the headset, relayed over the radio link


class Command(IntEnum):
    GET = 0x01
    SET = 0x02
    RGB = 0x06
    LED_INIT = 0x0D


class Op(IntEnum):
    SOFTWARE_MODE = 0x03
    BATTERY = 0x0F
    CHARGE = 0x10
    HEARTBEAT = 0x12
    FIRMWARE = 0x13
    SIDETONE_ENABLE = 0x46
    SIDETONE_LEVEL = 0x47
    ANC = 0xD1


class Mode(IntEnum):
    HARDWARE = 0x01  # firmware handles buttons and LEDs itself
    SOFTWARE = 0x02  # host takes over (mic button is forwarded, LEDs settable)


def frame(endpoint: Endpoint, command: Command, *payload: int) -> bytes:
    return bytes([0x00, MARKER, endpoint, command, *payload]).ljust(REPORT_SIZE, b"\0")


def request(op: Op, endpoint: Endpoint = Endpoint.HEADSET) -> bytes:
    """Single-opcode request (queries and heartbeats use SET with just the op)."""
    return frame(endpoint, Command.SET, op)


def set_mode(endpoint: Endpoint, mode: Mode) -> bytes:
    return frame(endpoint, Command.GET, Op.SOFTWARE_MODE, 0x00, mode)


def rgb(zones: list[tuple[int, int, int]]) -> bytes:
    """All zones are written in one frame, grouped by channel: R of every zone,
    then G, then B."""
    data = [zone[ch] for ch in range(3) for zone in zones]
    return frame(Endpoint.HEADSET, Command.RGB, 0x00, len(data), 0x00, 0x00, 0x00, *data)


def led_init() -> bytes:
    return frame(Endpoint.HEADSET, Command.LED_INIT, 0x00, 0x01)


def sidetone(level: int) -> list[bytes]:
    """Sidetone needs ANC off; level 0 turns sidetone off and ANC back on.
    The device scale is 0-1000 in steps of 10."""
    off = level == 0
    raw = level * 10
    return [
        frame(Endpoint.HEADSET, Command.GET, Op.ANC, *((0x00, 0x01) if off else ())),
        frame(Endpoint.HEADSET, Command.GET, Op.SIDETONE_ENABLE, *((0x00, 0x01) if off else ())),
        frame(Endpoint.HEADSET, Command.GET, Op.SIDETONE_LEVEL, 0x00, raw & 0xFF, raw >> 8),
    ]


# -- input -------------------------------------------------------------------

_BUTTON = b"\x03\x01\x02"
_NOTICE = b"\x03\x01\x01"


@dataclass(frozen=True)
class Button:
    down: bool  # the button reports key down and key up; it carries no state


@dataclass(frozen=True)
class Notice:
    op: int
    value: int


REPLY = 0x01


@dataclass(frozen=True)
class Reply:
    data: bytes

    @property
    def source(self) -> Endpoint | None:
        """Who answered: the dongle or the headset (None if not a reply)."""
        if len(self.data) < 4 or self.data[0] != REPLY:
            return None
        return {0x00: Endpoint.RECEIVER, 0x01: Endpoint.HEADSET}.get(self.data[1])

    @property
    def command(self) -> int | None:
        return self.data[2] if len(self.data) > 2 else None

    @property
    def ok(self) -> bool:
        return len(self.data) > 3 and self.data[3] == 0x00

    def answers(self, command: Command, source: Endpoint = Endpoint.HEADSET) -> bool:
        return self.source == source and self.command == command and self.ok

    def word(self, offset: int = 4) -> int:
        """Little-endian 16-bit value; replies carry theirs at offset 4."""
        return (self.data[offset + 1] << 8) | self.data[offset] if len(self.data) > offset + 1 else 0


def classify(report: bytes) -> Button | Notice | Reply:
    if len(report) > 3 and report[:3] == _BUTTON:
        return Button(report[3] != 0)
    if len(report) >= 7 and report[:3] == _NOTICE:
        return Notice(report[3], (report[6] << 8) | report[5])
    return Reply(bytes(report))


def battery_percent(raw: int) -> int | None:
    """0 is an empty reading, and values above 1000 (100.0%) are the stale
    packet the headset sends first after idling. Both are rejected, not
    clamped: clamping is what produces phantom 100% readings."""
    return raw // 10 if 0 < raw <= 1000 else None


def charge_state(raw: int) -> bool | None:
    return {1: True, 2: False}.get(raw)

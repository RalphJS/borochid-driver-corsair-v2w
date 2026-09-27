from __future__ import annotations

import asyncio
import copy
from pathlib import Path

import pytest

from borochid.common.manifest import Manifest
from borochid.common.models import Bus, DeviceIdentity
from borochid.service.channels import Channel
from borochid.service.settings import MemoryStore

from borochid_corsair_v2w.driver import HeadsetDriver

WIRELESS_PID, WIRED_PID, STANDBY_PID = 0x0A3E, 0x0A3D, 0x0A46

MANIFEST = {
    "id": "test.headset",
    "version": "1.0.0",
    "match": [
        {"bus": "usb", "vid": "0x1b1c", "pid": "0x0a3e"},
        {"bus": "usb", "vid": "0x1b1c", "pid": "0x0a3d"},
        {"bus": "usb", "vid": "0x1b1c", "pid": "0x0a46", "channel": None},
    ],
    "channel": {"type": "hid", "interface": [4, 3]},
    "driver": {"type": "corsair-v2w"},
    "v2w": {
        "links": {"wireless": ["0x0a3e"], "wired": ["0x0a3d"], "standby": ["0x0a46"]},
        "keepalive_s": 0.05,
        "probe_s": 0.05,
        "probe_max_s": 0.05,
        "reply_timeout_s": 0.05,
        "first_contact_s": 0.3,
        "write_gap_s": 0,
        "features": {
            "lighting": {
                "zones": [
                    {"id": "logo", "kind": "color"},
                    {"id": "battery", "kind": "battery"},
                    {"id": "mic", "kind": "mic", "default": "#00ff00", "muted_default": "#ff0000"},
                ]
            },
            "battery": {"poll_s": 0.05, "low_percent": 15},
            "sidetone": {},
            "mic_button": {},
        },
    },
}


class FakeHeadset(Channel):
    """A V2W dongle + headset pair, replying in the format captured from a
    Virtuoso SE: ``01 <source> <command> <status> <lo> <hi>``."""

    RECEIVER_PID, HEADSET_PID = 0x0A3E, 0x0A3D

    def __init__(self, pid: int = WIRELESS_PID):
        super().__init__(DeviceIdentity(Bus.USB, "usb:1-2.4", vid=0x1B1C, pid=pid, serial="T1"), {"type": "hid"})
        self.powered = True
        self.mode = {0x08: 0x01, 0x09: 0x01}  # hardware mode on both ends
        self.battery_raw = 850
        self.charging = 2
        # After power-on / re-enumeration the radio link takes a while: every
        # headset reply waits until it is up (captured: ~600 ms).
        self.first_contact_delay = 0.0
        self._link_up_at: float | None = None
        # USB jitter on the dongle's own replies.
        self.dongle_delay = 0.0
        # Opcodes this model rejects (captured: a Virtuoso SE answers the
        # Void-family sidetone frames with status 05).
        self.rejected_ops: set[int] = set()
        self.frames: list[tuple[int, int, bytes]] = []
        self.painted: list[list[tuple[int, int, int]]] = []

    async def open(self): ...

    async def close(self): ...

    def count(self, endpoint: int, command: int, op: int) -> int:
        return sum(1 for ep, cmd, p in self.frames if (ep, cmd) == (endpoint, command) and p[:1] == bytes([op]))

    async def write(self, data: bytes) -> None:
        assert len(data) == 65 and data[:2] == b"\x00\x02"
        ep, cmd, payload = data[2], data[3], data[4:]
        self.frames.append((ep, cmd, payload.rstrip(b"\0")))
        if ep == 0x08:
            # The dongle always answers its own commands, headset or not.
            value = {0x13: 0x5010, 0x12: self.RECEIVER_PID}.get(payload[0], 0) if cmd == 0x02 else 0
            if cmd == 0x01 and payload[0] == 0x03:
                self.mode[ep] = payload[2]
            self._reply(0x00, cmd, value)
            return
        if not self.powered:
            return
        if cmd == 0x01 and payload[0] == 0x03:
            self.mode[ep] = payload[2]
        if cmd == 0x02 and payload[0] == 0x0F:
            self._reply(0x01, cmd, self.battery_raw)
        elif cmd == 0x02 and payload[0] == 0x10:
            self._reply(0x01, cmd, self.charging)
        elif cmd == 0x02 and payload[0] == 0x12:
            # Same shape as a battery reply; its value (the headset's own PID,
            # 2621) is what reads as a "stale >100%" battery level.
            self._reply(0x01, cmd, self.HEADSET_PID)
        else:
            if cmd == 0x06:
                n = payload[1] // 3
                d = payload[5 : 5 + 3 * n]
                self.painted.append([(d[i], d[n + i], d[2 * n + i]) for i in range(n)])
            self._reply(0x01, cmd, 0, status=0x05 if payload[0] in self.rejected_ops else 0x00)

    def _reply(self, source: int, cmd: int, value: int, status: int = 0x00) -> None:
        data = bytes([0x01, source, cmd, status, value & 0xFF, value >> 8]).ljust(64, b"\0")
        loop = asyncio.get_running_loop()
        if source == 0x00:
            loop.call_later(self.dongle_delay, self._deliver, data)
            return
        if self._link_up_at is None:
            self._link_up_at = loop.time() + self.first_contact_delay
        # Replies queue until the link is up, then arrive in order.
        loop.call_at(max(loop.time(), self._link_up_at), self._deliver, data)

    def power_cycle(self, on: bool) -> None:
        self.powered = on
        if on:
            self.mode[0x09] = 0x01  # a power-cycled headset forgets software mode
            self._link_up_at = None

    def announce(self) -> None:
        """Hypothetical dongle status notice when the headset reconnects."""
        self._deliver(bytes([0x03, 0x01, 0x01, 0x36, 0x00, 0x02, 0x00]).ljust(64, b"\0"))

    def press_mic(self) -> None:
        assert self.mode[0x09] == 0x02, "the button is only forwarded in software mode"
        for down in (1, 0):
            self._deliver(bytes([0x03, 0x01, 0x02, down]).ljust(64, b"\0"))

    def notify_battery(self, percent: int) -> None:
        raw = percent * 10
        self._deliver(bytes([0x03, 0x01, 0x01, 0x0F, 0x00, raw & 0xFF, raw >> 8]).ljust(64, b"\0"))


class FakeAudio:
    def __init__(self):
        self.mic_muted = False
        self._listeners = []

    def on_mic_muted(self, fn):
        self._listeners.append(fn)

    async def toggle_mic_mute(self):
        self.mic_muted = not self.mic_muted
        for fn in self._listeners:
            if asyncio.iscoroutine(result := fn(self.mic_muted)):
                await result
        return self.mic_muted


class FakeHost:
    def __init__(self):
        self.audio = FakeAudio()


def make_driver(pid=WIRELESS_PID, settings=None, manifest=None, **v2w):
    headset = FakeHeadset(pid)
    events: list[dict] = []
    store = MemoryStore(settings)
    m = copy.deepcopy(manifest or MANIFEST)
    m["v2w"].update(v2w)
    driver = HeadsetDriver(Manifest.from_json(m), Path("."), headset, events.append, store, FakeHost())
    headset.on_data = driver.on_data
    return driver, headset, events, store


@pytest.fixture
def run():
    def runner(coro):
        return asyncio.run(asyncio.wait_for(coro, 5))

    return runner

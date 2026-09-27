"""Record what a V2W dongle does, read-only, to learn its events.

    python -m borochid_corsair_v2w.capture MANIFEST.json [--seconds N]

Prints, with wall-clock and relative timestamps:

* every HID input report from the device, with its classification;
* USB add/remove events for the vendor, with product IDs, since some
  dongles re-enumerate (e.g. briefly as another product) instead of sending
  a report;
* your own notes: type a word (``off``, ``on``) and press Enter to mark
  what you just did.

It never writes to the device, so it cannot change the headset's state, and
it can run next to the Borochid service (hidraw gives each reader its own
copy of every report). When the device disappears it keeps listening and
reattaches when it comes back.
"""

from __future__ import annotations

import argparse
import json
import os
import select
import sys
import time
from datetime import datetime
from pathlib import Path

import pyudev

from borochid.common.models import parse_int
from borochid.service.channels import ChannelError, find_usb_child
from borochid.service.detectors.udev import identity_from_udev

from borochid_corsair_v2w import protocol


class Capture:
    def __init__(self, manifest: dict):
        self.vid = parse_int(manifest["match"][0]["vid"])
        self.pids = {parse_int(r["pid"]) for r in manifest["match"] if "pid" in r}
        self.interface = manifest["channel"].get("interface")
        self.ctx = pyudev.Context()
        self.fd: int | None = None
        self.start = time.monotonic()

    def log(self, text: str) -> None:
        now = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        print(f"{now} {time.monotonic() - self.start:8.3f}s  {text}", flush=True)

    def attach(self) -> None:
        if self.fd is not None:
            return
        for dev in self.ctx.list_devices(subsystem="usb", DEVTYPE="usb_device"):
            ident = identity_from_udev(dev)
            if ident and ident.vid == self.vid and ident.pid in self.pids:
                try:
                    node = find_usb_child(ident, "hidraw", self.interface)
                    self.fd = os.open(node, os.O_RDONLY | os.O_NONBLOCK)
                except (ChannelError, OSError) as e:
                    self.log(f"-- found {ident.pid:04x} but cannot open its HID node yet: {e}")
                    return
                self.log(f"-- attached to {ident.name} ({ident.vid:04x}:{ident.pid:04x}) on {node}")
                return

    def detach(self, why: str) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
            self.log(f"-- detached: {why}")

    def usb_event(self, dev: pyudev.Device) -> None:
        props = dev.properties
        if props.get("ID_VENDOR_ID", "").lower() != f"{self.vid:04x}" and dev.subsystem == "usb":
            return
        if dev.subsystem == "usb" and dev.device_type == "usb_device":
            pid = props.get("ID_MODEL_ID") or (props.get("PRODUCT", "").split("/")[1:2] or ["?"])[0]
            known = "" if pid and int(pid, 16) in self.pids else "  (not in the package)"
            self.log(f"== USB {dev.action} {self.vid:04x}:{pid}{known}")
        elif dev.subsystem == "hidraw" and dev.action == "add":
            self.attach()

    def report(self, data: bytes) -> None:
        msg = protocol.classify(data)
        if isinstance(msg, protocol.Button):
            kind = f"button {'down' if msg.down else 'up'}"
        elif isinstance(msg, protocol.Notice):
            op = protocol.Op(msg.op).name if msg.op in protocol.Op._value2member_map_ else f"op 0x{msg.op:02x}"
            kind = f"notice {op} value={msg.value}"
        else:
            kind = "reply/other"
        self.log(f"   {(data.rstrip(b'\\0') or data[:1]).hex(' '):<48} {kind}")

    def run(self, seconds: float) -> None:
        monitor = pyudev.Monitor.from_netlink(self.ctx)
        monitor.filter_by("usb")
        monitor.filter_by("hidraw")
        monitor.start()
        self.log(f"read-only capture for {seconds:.0f} s. Type a note (e.g. 'off', 'on') + Enter to mark actions; Ctrl+C stops.")
        self.attach()
        if self.fd is None:
            self.log("-- device not present yet; waiting for it")
        end = time.monotonic() + seconds
        stdin_open = True
        while (left := end - time.monotonic()) > 0:
            fds = [monitor.fileno()] + ([sys.stdin.fileno()] if stdin_open else []) + ([self.fd] if self.fd is not None else [])
            ready, _, _ = select.select(fds, [], [], left)
            if monitor.fileno() in ready:
                while (dev := monitor.poll(timeout=0)) is not None:
                    self.usb_event(dev)
            if stdin_open and sys.stdin.fileno() in ready:
                line = sys.stdin.readline()
                stdin_open = line != ""  # EOF: stop watching, or select spins
                if line.strip():
                    self.log(f">> YOU: {line.strip()}")
            if self.fd is not None and self.fd in ready:
                try:
                    self.report(os.read(self.fd, 64))
                except BlockingIOError:
                    pass
                except OSError as e:
                    self.detach(f"{e.strerror} (the device went away)")


def main() -> None:
    ap = argparse.ArgumentParser(prog="python -m borochid_corsair_v2w.capture")
    ap.add_argument("manifest", type=Path)
    ap.add_argument("--seconds", type=float, default=300)
    args = ap.parse_args()
    cap = Capture(json.loads(args.manifest.read_text()))
    try:
        cap.run(args.seconds)
    except KeyboardInterrupt:
        pass
    finally:
        cap.detach("capture ended")


if __name__ == "__main__":
    main()

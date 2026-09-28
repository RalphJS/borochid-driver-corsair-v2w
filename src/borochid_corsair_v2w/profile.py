"""Model profile: the manifest's ``v2w`` section, validated.

Everything that differs between V2W headsets lives here as data, so
supporting a new model means publishing a device package, not a driver
release::

    "v2w": {
      "links": {"wireless": ["0x0a3e"], "wired": ["0x0a3d"], "standby": ["0x0a46"]},
      "receiver_software_mode": false,
      "keepalive_s": 20,
      "probe_s": 3,
      "probe_max_s": 60,
      "features": {
        "lighting": {"zones": [
          {"id": "logo", "kind": "color", "label": "Logo"},
          {"id": "battery", "kind": "battery"},
          {"id": "mic", "kind": "mic", "label": "Microphone"}
        ]},
        "battery": {"poll_s": 300, "low_percent": 15},
        "sidetone": {},
        "mic_button": {}
      }
    }

Validate a manifest with ``python -m borochid_corsair_v2w.profile manifest.json``.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any


class ProfileError(ValueError):
    pass


class Link(StrEnum):
    WIRELESS = "wireless"  # through the dongle: V2W available
    WIRED = "wired"  # USB cable: V2W on the dongle's endpoint
    # The dongle with no headset linked. A Virtuoso SE dongle re-enumerates
    # as a different product (0a46) while its headset is off; nothing to
    # talk to, and the manifest matches it with "channel": null.
    STANDBY = "standby"


ZONE_KINDS = {"color", "mic", "battery"}
FEATURES = {"lighting", "battery", "sidetone", "mic_button"}


@dataclass(frozen=True)
class Zone:
    id: str
    kind: str
    label: str
    default: str = "#ff0000"
    muted_default: str = "#ff0000"


@dataclass(frozen=True)
class Profile:
    links: dict[int, Link]
    features: dict[str, dict[str, Any]]
    zones: tuple[Zone, ...] = ()
    # HeadsetControl also puts the receiver in software mode; on the Virtuoso
    # SE that switches the dongle's status LED off for as long as the mode is
    # held, and nothing else needs it. Off unless a model proves otherwise.
    receiver_software_mode: bool = False
    keepalive_s: float = 20.0
    # While the headset is off the driver waits for the dongle to announce it;
    # the probe is only a safety net, backing off from probe_s to probe_max_s.
    probe_s: float = 3.0
    probe_max_s: float = 60.0
    reply_timeout_s: float = 0.25
    # The first headset reply after the dongle (re)enumerates or the headset
    # powers on took ~600 ms on a Virtuoso SE; routine replies take <100 ms.
    first_contact_s: float = 1.5
    write_gap_s: float = 0.003
    extra: dict[str, Any] = field(default_factory=dict)

    def link_for(self, pid: int | None) -> Link:
        try:
            return self.links[pid]  # type: ignore[index]
        except KeyError:
            raise ProfileError(f"product id {pid!r} is not listed in v2w.links") from None

    @classmethod
    def from_manifest(cls, manifest: dict[str, Any]) -> Profile:
        try:
            profile = cls._parse(manifest["v2w"])
        except KeyError as e:
            raise ProfileError(f"missing v2w field {e.args[0]!r}") from None
        except (TypeError, ValueError) as e:
            if isinstance(e, ProfileError):
                raise
            raise ProfileError(str(e)) from None
        # Standby products have nothing to open; a channel there would leave
        # the device stuck "connecting" to an interface that doesn't exist.
        channelless = {
            int(r["pid"], 0) if isinstance(r["pid"], str) else int(r["pid"])
            for r in manifest.get("match", [])
            if "pid" in r and "channel" in r and r["channel"] is None
        }
        for pid, link in profile.links.items():
            if (link is Link.STANDBY) != (pid in channelless):
                raise ProfileError(
                    f"product 0x{pid:04x}: standby products need '\"channel\": null' in their match rule, and only they may"
                )
        return profile

    @classmethod
    def _parse(cls, d: dict[str, Any]) -> Profile:
        links: dict[int, Link] = {}
        for link, pids in d["links"].items():
            for pid in pids:
                links[int(pid, 0) if isinstance(pid, str) else int(pid)] = Link(link)
        if not links:
            raise ProfileError("v2w.links lists no product ids")

        features = {name: dict(cfg or {}) for name, cfg in d.get("features", {}).items()}
        if unknown := set(features) - FEATURES:
            raise ProfileError(f"unknown v2w features {sorted(unknown)}")

        zones = tuple(
            Zone(z["id"], z["kind"], z.get("label", z["id"].title()), z.get("default", "#ff0000"), z.get("muted_default", "#ff0000"))
            for z in features.get("lighting", {}).get("zones", [])
        )
        if "lighting" in features and not zones:
            raise ProfileError("lighting needs at least one zone")
        for z in zones:
            if z.kind not in ZONE_KINDS:
                raise ProfileError(f"zone {z.id!r}: kind must be one of {sorted(ZONE_KINDS)}")
        if len({z.id for z in zones}) != len(zones):
            raise ProfileError("zone ids must be unique")
        if sum(z.kind == "battery" for z in zones) > 1 or sum(z.kind == "mic" for z in zones) > 1:
            raise ProfileError("at most one battery zone and one mic zone")
        if any(z.kind == "battery" for z in zones) and "battery" not in features:
            raise ProfileError("a battery zone needs the battery feature")

        timings = {
            k: float(d[k])
            for k in ("keepalive_s", "probe_s", "probe_max_s", "reply_timeout_s", "first_contact_s", "write_gap_s")
            if k in d
        }
        if any(v < 0 for v in timings.values()):
            raise ProfileError("timings must be non-negative")
        return cls(links, features, zones, bool(d.get("receiver_software_mode", False)), **timings)


def main() -> None:
    ok = True
    for arg in sys.argv[1:]:
        try:
            p = Profile.from_manifest(json.loads(Path(arg).read_text()))
            print(f"ok   {arg}: {len(p.links)} product ids, features {sorted(p.features)}")
        except (ProfileError, OSError, json.JSONDecodeError) as e:
            ok = False
            print(f"FAIL {arg}: {e}", file=sys.stderr)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()

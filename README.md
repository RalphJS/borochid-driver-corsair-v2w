# borochid-driver-corsair-v2w

[Borochid](../borochid) driver for Corsair wireless headsets that speak the
**V2W** HID protocol (Virtuoso SE/XT and relatives).

This repo is **code only**. Headset models are described by signed data
packages in the Borochid registry (for example
[`borochid-corsair-virtuoso`](../borochid-corsair-virtuoso)). A model on the
V2W protocol needs a new device package and a new line in the udev rule,
never new driver code.

## How it fits

```
plug in dongle ─► service matches corsair.virtuoso-receiver (signed data, downloaded)
                  └─ driver: corsair-v2w-receiver, provided_by: borochid-driver-corsair-v2w
                       ├─ installed?  ─► ReceiverDriver runs; once the headset
                       │                 answers it announces it (Driver.pair) and
                       │                 the service matches corsair.virtuoso,
                       │                 driver corsair-v2w ─► HeadsetDriver runs
                       └─ missing     ─► GUI: "Install borochid-driver-corsair-v2w"
                                         via PackageKit (signed repos only)
plug in cable  ─► corsair.virtuoso ─► HeadsetDriver (the same features, on the
                                     dongle's endpoint)
```

As in iCUE, the dongle and the headset are two devices: the dongle's card
says whether its headset is connected (and its firmware), the headset's card
has its settings, whether it's on its cable or behind the dongle. The user
can hide the dongle's card.

The driver registers itself as the `corsair-v2w` entry point in the
`borochid.drivers` group. It is installed as an RPM/deb, so the package
manager handles signing, updates and root. The service never downloads code.

## Design

| Module | Role |
|---|---|
| `protocol.py` | Frame builders and input classification (button / notice / reply). Pure, no I/O. |
| `session.py` | Request/reply over Borochid's push-based channel: write pacing, re-entrant exclusive access, reply matching. |
| `profile.py` | The device package's `v2w` section, validated. Holds every model-specific fact. |
| `features/` | `lighting`, `battery`, `sidetone`, `mic_button`: independent units with their own settings, actions and reactions. |
| `receiver.py` | `ReceiverDriver`, the dongle: link state machine (`standby` / `offline` / `online`), handshake and keep-alive; announces the headset while it answers and hands it its input. |
| `driver.py` | `HeadsetDriver`, the headset, behind its dongle or on its cable: reads its ID and composes the features the profile enables. Behind the dongle it shares the dongle's session; on the cable it takes the headset over itself. |

Features never call each other. They react to shared state: when `battery`
publishes a new level, `lighting` recolours the battery zone. Mic mute and
host audio (ALSA volume and sidetone, PipeWire mute, feedback tone) belong to
the service's audio service, so this package spawns no processes.

### Protocol facts that shaped the design

These were learned the hard way in VirtuosoControl (see NOTICE):

* **Only the headset enters software mode, not the receiver.** On the
  receiver, software mode turns the dongle's LED off. The profile flag
  `receiver_software_mode` exists in case a model needs it.
* **Software mode kills the mic button until the host handles it, and it
  survives closing the device.** `mic_button` forwards presses to the audio
  service, and the dongle's `stop()` always returns both endpoints to
  hardware mode (after the headset's, which undoes its own settings first).
* **The handshake darkens the LEDs.** Lighting is the first feature brought
  online and repaints immediately.
* **The dongle answering doesn't mean the headset does.** Replies are
  `01 <source> <command> <status> <lo> <hi>`, with source `00` for the dongle
  and `01` for the headset, and the dongle answers its own commands whether
  or not the headset is on. Liveness counts only headset replies. A
  power-cycled headset forgets software mode, so every reconnect
  re-handshakes and every feature re-applies its settings.
* **First contact is slow.** After the dongle re-enumerates or the headset
  powers on, the first headset reply took about 600 ms on a Virtuoso SE
  (routine replies take under 100 ms). The check right after a handshake
  therefore waits `first_contact_s` (1.5 s), not the routine
  `reply_timeout_s`.
* **The "stale >100% battery reading" is a heartbeat reply.** The
  heartbeat reply has the same shape as a battery reply and carries the
  headset's own PID (`0x0a3d` = 2621), so a late one reads as 262%. Values
  of 0 or above 1000 are rejected, not clamped: clamping is what showed
  phantom 100% readings. Charge state has its own query, which also sees
  wall chargers. The headset pushes a notice whenever level or charge
  changes, so battery tracking is event-driven with a slow safety poll.
  Below `low_percent` and not charging polling stops entirely, because
  waking a flat headset makes it beep.
* **The dongle reports headset power by changing identity.** When the
  headset is switched off, a Virtuoso SE dongle drops off USB within about
  a second and re-enumerates as `1b1c:0a46` (one HID interface), staying
  that way until the headset is switched back on (37 s measured), when it
  returns as `0a3e`. Device packages list such products under
  `v2w.links.standby` with `"channel": null`, so the entry stays and reads
  "Headset off". The service's re-plug grace period keeps it as one device.
  Because the dongle reports power this way, the offline probe is only a
  slow last resort.
* **Interface order matters.** Standard models speak V2W on HID interface 4
  and the Slipstream receiver only has 3, so device packages use
  `"interface": [4, 3]`.
* **On the cable the headset speaks V2W on the dongle's endpoint.** A
  cabled Virtuoso SE answers `0x08` (replies from source `00`) the way it
  answers `0x09` through the dongle: battery, charge, lighting, software
  mode (as OpenLinkHub drives it). The headset driver addresses its
  `target` endpoint, and on the cable does the dongle driver's part itself:
  software mode, the keep-alive, and hardware mode again on stop.
  Hardware sidetone is only tried through the dongle; on the cable the
  sound card's sidetone is there.
* **The headset has its own ID.** Data resource `0x05` holds 8 bytes that
  identify the headset whichever way it is connected. On the cable it is
  read on the dongle's endpoint (`0x08`, replies from source `00`), through
  the dongle on the headset's (`0x09`, source `01`). Read with open
  (`0d <handle> 05`), read (`08 <handle>`), close (`05 01 <handle>`), on
  handle 2 (lighting uses 0). The headset driver passes it to
  `identify()`, so the service shows the headset once and its settings
  follow it between cable and dongle. The dongle's own USB serial differs
  from the headset's, and none of the single-value properties
  (`02 <prop>`) carries a serial: `0x11`/`0x12` are the VID/PID, `0x13`
  the firmware (`a.b.c` from its first three bytes).
* **The dongle's status LED is the firmware's.** White while the headset
  is linked, blinking red while it searches (plugged in with no headset,
  or the link lost, e.g. out of range), dark once the headset is switched
  off: a clean power-off tells the dongle, which then re-enumerates as its
  standby product. Nothing here drives it; only receiver software mode
  would turn it off (see above).
* **One session for two devices.** The headset behind the dongle talks
  through the dongle's node, and the device answers whatever was asked
  last. Both drivers therefore use the dongle's `Session` (passed along
  with the paired channel); the dongle's driver feeds it every reply and
  hands the headset its button and status reports.

## Adding a model

1. In the device package repo, add the model's PIDs to `v2w.links` and
   `match`, and describe its zones and features. Validate with
   `python -m borochid_corsair_v2w.profile path/to/manifest.json`.
2. Here, add the PIDs to `udev/70-borochid-corsair-v2w.rules` and release a
   new package version. This is the one step that needs root on user
   machines, which is why it lives in the system package.

## Development

```sh
python3 -m venv --system-site-packages .venv
.venv/bin/pip install -e ../borochid/packages/common -e ../borochid/packages/service -e '.[test]'
.venv/bin/pytest
```

The tests drive the real `ReceiverDriver` and `HeadsetDriver` against
`FakeHeadset` (`tests/conftest.py`), which replies in the format captured
from real hardware and models the behaviour listed above. `Rig` wires them
up the way the service does: the headset appears on a `PairedChannel` when
the dongle announces it and is stopped before the dongle.

To learn how a dongle behaves, capture it read-only next to the running
service and mark your actions (type `off`/`on` + Enter):

```sh
python -m borochid_corsair_v2w.capture path/to/manifest.json
```

## Packaging

* RPM: `packaging/rpm/borochid-driver-corsair-v2w.spec` (pyproject macros;
  installs the udev rule into `%{_udevrulesdir}`).
* Debian: `debian/` (pybuild with the pyproject plugin; the rule is
  installed by `dh_installudev` from `udev/`).

Both depend on the `borochid-service` package. Fedora COPR or openSUSE OBS can
build both formats from this repo.

import asyncio
import copy
from pathlib import Path

import pytest

from borochid.common.manifest import Manifest
from borochid.service.drivers import DriverError
from borochid.service.settings import MemoryStore

from borochid_corsair_v2w.driver import HeadsetDriver
from borochid_corsair_v2w.receiver import ReceiverDriver

from conftest import HEADSET_ID, MANIFEST, RECEIVER_MANIFEST, STANDBY_PID, WIRED_PID, FakeHeadset, Rig, make_cable

RECEIVER, HEADSET = 0x08, 0x09
HW, SW = 0x01, 0x02


def test_the_dongle_pairs_the_headset_once_it_answers_and_hands_it_back_on_stop(run):
    async def go():
        rig = Rig()
        await rig.start()
        assert rig.receiver.state["link"] == "online" and rig.headset is not None
        assert rig.headset.state["link"] == "online" and rig.headset.channel.ident.uid == "usb:1-2.4/headset"
        assert rig.device.mode == {RECEIVER: HW, HEADSET: SW}, "receiver software mode darkens the dongle LED"
        await rig.stop()
        assert rig.headset is None
        assert rig.device.mode == {RECEIVER: HW, HEADSET: HW}, "the mic button must work again after exit"

    run(go())


def test_the_dongle_reports_its_firmware(run):
    async def go():
        rig = Rig()
        await rig.start()
        assert rig.receiver.state["firmware"] == "0.16.80"
        await rig.stop()

    run(go())


def test_the_headset_reads_the_same_id_over_the_radio_and_on_its_cable(run):
    async def go():
        rig = Rig()
        await rig.start()
        assert rig.identified == [HEADSET_ID]
        assert rig.device.count(HEADSET, 0x0D, 0x02) == 1, "resource opened on its own handle, not lighting's"
        assert rig.device.handles.get(0x02) is None, "and closed again"
        await rig.stop()

        cable, device, identified, _ = make_cable()
        await cable.start()
        await cable.stop()
        assert identified == [HEADSET_ID] and cable.state["link"] == "wired"
        assert {ep for ep, _, _ in device.frames} == {RECEIVER}, "on the cable, the dongle's endpoint"

    run(go())


def test_on_its_cable_the_headset_is_driven_like_through_the_dongle(run):
    """Captured: on the cable a Virtuoso SE speaks V2W on endpoint 0x08,
    replying from source 00 (battery, charge, lighting, software mode)."""

    async def go():
        cable, device, _, host = make_cable(settings={"logo_color": "#0000ff"})
        await cable.start()
        await asyncio.sleep(0.02)
        assert device.mode[RECEIVER] == SW and cable.state["link"] == "wired" and cable.state["online"] is True
        assert cable.state["battery"] == 85 and cable.state["charging"] is False
        assert device.painted[-1][0] == (0, 0, 255)
        await cable.invoke("set_logo_color", {"value": "#ff0000"})
        assert device.painted[-1][0] == (255, 0, 0)
        device.press_mic()
        await asyncio.sleep(0.02)
        assert host.audio.mic_muted is True
        heartbeats = device.count(RECEIVER, 0x02, 0x12)
        await asyncio.sleep(0.12)
        assert device.count(RECEIVER, 0x02, 0x12) > heartbeats, "kept alive"
        await cable.stop()
        assert device.mode[RECEIVER] == HW, "the mic button must work again after exit"

    run(go())


def test_a_headset_without_an_id_still_works(run):
    async def go():
        rig = Rig()
        rig.device.headset_id = bytes(8)  # all zeros: none
        await rig.start()
        assert rig.identified == [] and rig.headset.state["link"] == "online"
        await rig.stop()

    run(go())


def test_leds_are_repainted_right_after_pairing(run):
    async def go():
        rig = Rig(settings={"logo_color": "#ff8000", "logo_brightness": 50})
        await rig.start()
        # Painted before the battery is read, so the battery zone starts dark,
        # then turns green (85%) once the reading arrives. Logo at 50%.
        assert rig.device.painted[0] == [(127, 64, 0), (0, 0, 0), (0, 255, 0)]
        assert rig.device.painted[1] == [(127, 64, 0), (0, 255, 0), (0, 255, 0)]
        await rig.stop()

    run(go())


def test_heartbeat_reply_is_never_read_as_battery(run):
    async def go():
        rig = Rig()
        await rig.start()
        await rig.settle(0.02)
        assert rig.headset.state["battery"] == 85 and rig.headset.state["charging"] is False
        await rig.stop()

    run(go())


def test_mic_button_mutes_through_host_and_recolours_mic_zone(run):
    async def go():
        rig = Rig()
        await rig.start()
        rig.device.press_mic()
        await asyncio.sleep(0.02)
        assert rig.host.audio.mic_muted is True
        assert rig.device.painted[-1][2] == (255, 0, 0), "muted colour on the mic zone"
        rig.device.press_mic()
        await asyncio.sleep(0.02)
        assert rig.host.audio.mic_muted is False and rig.device.painted[-1][2] == (0, 255, 0)
        await rig.stop()

    run(go())


def test_power_cycle_unpairs_then_repairs_and_the_headset_reapplies_its_settings(run):
    async def go():
        rig = Rig(settings={"sidetone": 40})
        await rig.start()
        rig.device.power_cycle(False)
        await rig.settle(0.15)
        assert rig.receiver.state["link"] == "offline" and rig.headset is None
        paints = len(rig.device.painted)
        rig.device.power_cycle(True)
        await rig.settle(0.15)
        assert rig.receiver.state["link"] == "online" and rig.pairings == 2
        assert rig.device.mode[HEADSET] == SW and len(rig.device.painted) > paints
        assert rig.device.count(HEADSET, 0x01, 0x47) >= 2, "sidetone re-applied after reconnect"
        await rig.stop()

    run(go())


def test_low_battery_stops_polling_to_avoid_beeps(run):
    async def go():
        rig = Rig()
        rig.device.battery_raw = 100  # 10%
        await rig.start()
        before = rig.device.count(HEADSET, 0x02, 0x0F)
        await asyncio.sleep(0.2)
        # Only keep-alive checks (one query each) remain, no 3-sample refreshes.
        keepalives = rig.device.count(HEADSET, 0x02, 0x12) - 1
        assert rig.device.count(HEADSET, 0x02, 0x0F) - before <= keepalives + 1
        rig.device.charging = 1
        rig.device.notify_battery(11)
        await asyncio.sleep(0.02)
        await rig.stop()

    run(go())


def test_battery_notice_reaches_the_headset_through_the_dongle(run):
    async def go():
        rig = Rig()
        await rig.start()
        rig.device.notify_battery(12)
        await asyncio.sleep(0.02)
        assert rig.headset.state["battery"] == 12 and rig.device.painted[-1][1] == (255, 0, 0)
        await rig.stop()

    run(go())


def test_settings_are_validated_persisted_and_applied_when_paired_again(run):
    async def go():
        rig = Rig()
        await rig.start()
        await rig.headset.invoke("set_logo_color", {"value": "#0000FF"})
        assert rig.store.load()["logo_color"] == "#0000ff" and rig.device.painted[-1][0] == (0, 0, 255)
        with pytest.raises(DriverError, match="colour"):
            await rig.headset.invoke("set_logo_color", {"value": "blue"})
        with pytest.raises(DriverError, match="unknown action"):
            await rig.headset.invoke("set_link", {"value": "online"})
        rig.device.power_cycle(False)
        await rig.settle(0.15)
        rig.device.painted.clear()
        rig.device.power_cycle(True)
        await rig.settle(0.15)
        assert rig.device.painted[0][0] == (0, 0, 255)
        await rig.stop()

    run(go())


def test_settings_kept_by_the_other_connection_are_shown_and_pushed(run):
    async def go():
        rig = Rig()
        await rig.start()
        rig.store.save({"logo_color": "#00ff00"})  # changed while it was on its cable
        await rig.headset.reload_settings()
        assert rig.headset.state["logo_color"] == "#00ff00" and rig.device.painted[-1][0] == (0, 255, 0)
        await rig.stop()

    run(go())


def test_sidetone_is_left_alone_until_set_and_off_on_exit(run):
    async def go():
        rig = Rig()
        await rig.start()
        assert rig.device.count(HEADSET, 0x01, 0x47) == 0
        await rig.headset.invoke("set_sidetone", {"value": 30})
        await rig.stop()
        levels = [p for ep, cmd, p in rig.device.frames if (ep, cmd) == (HEADSET, 0x01) and p[:1] == b"\x47"]
        assert levels[-1] == b"\x47", "level 0 on exit (trailing zero bytes stripped)"
        last_sidetone = max(i for i, (ep, cmd, p) in enumerate(rig.device.frames) if p[:1] == b"\x47")
        hand_back = max(i for i, (ep, cmd, p) in enumerate(rig.device.frames) if (ep, cmd, p) == (HEADSET, 0x01, b"\x03\x00\x01"))
        assert last_sidetone < hand_back, "the headset's settings are undone before the dongle hands it back"

    run(go())


def _driver(cls, base, pid, shared=None):
    device = FakeHeadset(pid)
    device.shared = shared
    return cls(Manifest.from_json(copy.deepcopy(base)), Path("."), device, [].append, MemoryStore(), None)


def test_each_driver_refuses_what_is_not_its_own():
    with pytest.raises(DriverError, match="not listed"):
        _driver(ReceiverDriver, RECEIVER_MANIFEST, 0x9999)
    with pytest.raises(DriverError, match="not listed"):
        _driver(HeadsetDriver, MANIFEST, 0x9999)
    wired = copy.deepcopy(RECEIVER_MANIFEST)
    wired["v2w"]["links"]["wired"] = ["0x0a3d"]
    with pytest.raises(DriverError, match="cable is the headset's"):
        _driver(ReceiverDriver, wired, WIRED_PID)
    with pytest.raises(DriverError, match="through its dongle's driver"):
        _driver(HeadsetDriver, MANIFEST, 0x0A3E)  # the dongle's PID, but not announced by it


def test_reconnects_as_soon_as_the_dongle_announces_the_headset(run):
    """With a slow safety probe, only the dongle's notice can explain a fast reconnect."""

    async def go():
        rig = Rig(probe_s=30, probe_max_s=30)
        await rig.start()
        rig.device.power_cycle(False)
        await rig.settle(0.12)
        assert rig.receiver.state["link"] == "offline"
        rig.device.power_cycle(True)
        await rig.settle(0.1)
        assert rig.receiver.state["link"] == "offline", "no probe yet: the safety net is 30 s away"
        rig.device.announce()
        await rig.settle(0.1)
        assert rig.receiver.state["link"] == "online" and rig.device.mode[HEADSET] == SW and rig.headset is not None
        await rig.stop()

    run(go())


def test_safety_probe_backs_off_while_the_headset_stays_off(run):
    async def go():
        rig = Rig(probe_s=0.02, probe_max_s=0.16)
        rig.device.power_cycle(False)
        await rig.start()
        await asyncio.sleep(0.6)
        handshakes = rig.device.count(RECEIVER, 0x02, 0x13) - 1  # (the first is the firmware read)
        await rig.stop()
        # Fixed 20 ms probing would be ~30 handshakes; backoff (20, 40, 80, 160, 160...) gives ~6.
        assert 3 <= handshakes <= 9 and rig.pairings == 0

    run(go())


def test_replies_to_our_own_probe_do_not_trigger_another(run):
    async def go():
        rig = Rig(probe_s=30, probe_max_s=30)
        rig.device.power_cycle(False)
        await rig.start()
        before = rig.device.count(RECEIVER, 0x02, 0x13)
        await asyncio.sleep(0.2)
        assert rig.device.count(RECEIVER, 0x02, 0x13) == before, "idle while offline"
        await rig.stop()

    run(go())


def test_dongle_replies_do_not_make_a_switched_off_headset_look_alive(run):
    """The dongle answers its own commands while the headset is off."""

    async def go():
        rig = Rig(probe_s=30, probe_max_s=30)
        rig.device.power_cycle(False)
        rig.device.dongle_delay = 0.04  # its replies land during the liveness check
        await rig.start()
        assert rig.receiver.state["link"] == "offline" and rig.pairings == 0
        assert rig.device.count(RECEIVER, 0x02, 0x13) == 2, "the dongle did reply to the handshake"
        await rig.stop()

    run(go())


def test_slow_first_contact_does_not_cost_a_probe_cycle(run):
    """Captured: the first headset reply after (re)connecting took ~600 ms."""

    async def go():
        rig = Rig(probe_s=30, probe_max_s=30)
        rig.device.first_contact_delay = 0.2  # > reply_timeout_s (0.05), < first_contact_s (0.3)
        await rig.start()
        assert rig.receiver.state["link"] == "online"
        assert rig.device.count(RECEIVER, 0x02, 0x13) == 2, "connected on the first handshake"
        await rig.stop()

    run(go())


def test_rejected_sidetone_is_reported_not_silently_ignored(run):
    async def go():
        rig = Rig()
        rig.device.rejected_ops = {0xD1, 0x46, 0x47}
        await rig.start()
        with pytest.raises(DriverError, match="rejected hardware sidetone"):
            await rig.headset.invoke("set_sidetone", {"value": 40})
        assert rig.headset.state["sidetone_supported"] is False
        sent = rig.device.count(HEADSET, 0x01, 0xD1)
        with pytest.raises(DriverError, match="does not support"):
            await rig.headset.invoke("set_sidetone", {"value": 50})
        assert rig.device.count(HEADSET, 0x01, 0xD1) == sent, "no more frames once known unsupported"
        await rig.stop()

    run(go())


def test_led_init_is_sent_once_per_pairing(run):
    async def go():
        rig = Rig()
        await rig.start()
        await rig.headset.invoke("set_logo_color", {"value": "#00ff00"})
        await rig.headset.invoke("set_logo_color", {"value": "#0000ff"})
        assert rig.device.count(HEADSET, 0x0D, 0x00) == 1 and len(rig.device.painted) >= 3
        await rig.stop()

    run(go())


def test_standby_dongle_is_left_alone(run):
    """Headset off: the dongle is another product with nothing to talk to."""

    async def go():
        rig = Rig(pid=STANDBY_PID)
        await rig.start()
        await asyncio.sleep(0.1)
        await rig.stop()
        assert rig.device.frames == [] and rig.pairings == 0
        assert rig.receiver.state["link"] == "standby" and rig.receiver.state["online"] is False

    run(go())

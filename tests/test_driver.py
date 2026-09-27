import asyncio

import pytest

from borochid.service.drivers import DriverError

from conftest import MANIFEST, STANDBY_PID, WIRED_PID, make_driver

RECEIVER, HEADSET = 0x08, 0x09
HW, SW = 0x01, 0x02


def test_handshake_takes_only_the_headset_and_stop_hands_it_back(run):
    async def go():
        driver, headset, _, _ = make_driver()
        await driver.start()
        assert driver.state["link"] == "online"
        assert headset.mode == {RECEIVER: HW, HEADSET: SW}, "receiver software mode darkens the dongle LED"
        await driver.stop()
        assert headset.mode == {RECEIVER: HW, HEADSET: HW}, "the mic button must work again after exit"

    run(go())


def test_leds_are_repainted_right_after_the_handshake(run):
    async def go():
        driver, headset, _, _ = make_driver(settings={"logo_color": "#ff8000", "logo_brightness": 50})
        await driver.start()
        # Painted before the battery is read, so the battery zone starts dark,
        # then turns green (85%) once the reading arrives. Logo at 50%.
        assert headset.painted[0] == [(127, 64, 0), (0, 0, 0), (0, 255, 0)]
        assert headset.painted[1] == [(127, 64, 0), (0, 255, 0), (0, 255, 0)]
        await driver.stop()

    run(go())


def test_heartbeat_reply_is_never_read_as_battery(run):
    async def go():
        driver, headset, _, _ = make_driver()
        await driver.start()
        assert driver.state["battery"] == 85 and driver.state["charging"] is False
        await driver.stop()

    run(go())


def test_mic_button_mutes_through_host_and_recolours_mic_zone(run):
    async def go():
        driver, headset, _, _ = make_driver()
        await driver.start()
        headset.press_mic()
        await asyncio.sleep(0.02)
        assert driver.host.audio.mic_muted is True
        assert headset.painted[-1][2] == (255, 0, 0), "muted colour on the mic zone"
        headset.press_mic()
        await asyncio.sleep(0.02)
        assert driver.host.audio.mic_muted is False and headset.painted[-1][2] == (0, 255, 0)
        await driver.stop()

    run(go())


def test_power_cycle_goes_offline_then_rehandshakes_and_reapplies(run):
    async def go():
        driver, headset, _, _ = make_driver(settings={"sidetone": 40})
        await driver.start()
        headset.power_cycle(False)
        await asyncio.sleep(0.15)
        assert driver.state["link"] == "offline" and driver.state["battery"] is None
        paints = len(headset.painted)
        headset.power_cycle(True)
        await asyncio.sleep(0.15)
        assert driver.state["link"] == "online"
        assert headset.mode[HEADSET] == SW and len(headset.painted) > paints
        assert headset.count(HEADSET, 0x01, 0x47) >= 2, "sidetone re-applied after reconnect"
        await driver.stop()

    run(go())


def test_wired_link_never_speaks_v2w(run):
    async def go():
        driver, headset, _, _ = make_driver(pid=WIRED_PID)
        await driver.start()
        await asyncio.sleep(0.1)
        await driver.stop()
        assert headset.frames == [] and driver.state["link"] == "wired"

    run(go())


def test_low_battery_stops_polling_to_avoid_beeps(run):
    async def go():
        driver, headset, _, _ = make_driver()
        headset.battery_raw = 100  # 10%
        await driver.start()
        before = headset.count(HEADSET, 0x02, 0x0F)
        await asyncio.sleep(0.2)
        # Only keep-alive probes (one query each) remain, no 3-sample refreshes.
        keepalives = headset.count(HEADSET, 0x02, 0x12) - 1
        assert headset.count(HEADSET, 0x02, 0x0F) - before <= keepalives + 1
        headset.charging = 1
        headset.notify_battery(11)
        await asyncio.sleep(0.02)
        await driver.stop()

    run(go())


def test_battery_notice_updates_state_and_led(run):
    async def go():
        driver, headset, _, _ = make_driver()
        await driver.start()
        headset.notify_battery(12)
        await asyncio.sleep(0.02)
        assert driver.state["battery"] == 12 and headset.painted[-1][1] == (255, 0, 0)
        await driver.stop()

    run(go())


def test_settings_are_validated_persisted_and_applied_when_back_online(run):
    async def go():
        driver, headset, events, store = make_driver()
        await driver.start()
        headset.power_cycle(False)
        await asyncio.sleep(0.15)
        painted = len(headset.painted)
        await driver.invoke("set_logo_color", {"value": "#0000FF"})
        assert store.load()["logo_color"] == "#0000ff" and len(headset.painted) == painted
        with pytest.raises(DriverError, match="colour"):
            await driver.invoke("set_logo_color", {"value": "blue"})
        with pytest.raises(DriverError, match="unknown action"):
            await driver.invoke("set_link", {"value": "online"})
        headset.power_cycle(True)
        await asyncio.sleep(0.15)
        assert headset.painted[-1][0] == (0, 0, 255)
        await driver.stop()

    run(go())


def test_sidetone_is_left_alone_until_set_and_off_on_exit(run):
    async def go():
        driver, headset, _, _ = make_driver()
        await driver.start()
        assert headset.count(HEADSET, 0x01, 0x47) == 0
        await driver.invoke("set_sidetone", {"value": 30})
        await driver.stop()
        levels = [p for ep, cmd, p in headset.frames if (ep, cmd) == (HEADSET, 0x01) and p[:1] == b"\x47"]
        assert levels[-1] == b"\x47", "level 0 on exit (trailing zero bytes stripped)"

    run(go())


def test_unknown_product_id_is_a_driver_error():
    with pytest.raises(DriverError, match="not listed"):
        make_driver(pid=0x9999)


def test_reconnects_as_soon_as_the_dongle_announces_the_headset(run):
    """With a slow safety probe, only the dongle's notice can explain a fast reconnect."""

    async def go():
        driver, headset, _, _ = make_driver(probe_s=30, probe_max_s=30)
        await driver.start()
        headset.power_cycle(False)
        await asyncio.sleep(0.12)
        assert driver.state["link"] == "offline"
        headset.power_cycle(True)
        await asyncio.sleep(0.1)
        assert driver.state["link"] == "offline", "no probe yet: the safety net is 30 s away"
        headset.announce()
        await asyncio.sleep(0.1)
        assert driver.state["link"] == "online" and headset.mode[HEADSET] == SW
        await driver.stop()

    run(go())


def test_safety_probe_backs_off_while_the_headset_stays_off(run):
    async def go():
        driver, headset, _, _ = make_driver(probe_s=0.02, probe_max_s=0.16)
        headset.power_cycle(False)
        await driver.start()
        await asyncio.sleep(0.6)
        handshakes = headset.count(RECEIVER, 0x02, 0x13)
        await driver.stop()
        # Fixed 20 ms probing would be ~30 handshakes; backoff (20, 40, 80, 160, 160...) gives ~6.
        assert 3 <= handshakes <= 9

    run(go())


def test_replies_to_our_own_probe_do_not_trigger_another(run):
    async def go():
        driver, headset, _, _ = make_driver(probe_s=30, probe_max_s=30)
        headset.power_cycle(False)
        await driver.start()
        before = headset.count(RECEIVER, 0x02, 0x13)
        await asyncio.sleep(0.2)
        assert headset.count(RECEIVER, 0x02, 0x13) == before, "idle while offline"
        await driver.stop()

    run(go())


def test_dongle_replies_do_not_make_a_switched_off_headset_look_alive(run):
    """The dongle answers its own commands while the headset is off."""

    async def go():
        driver, headset, _, _ = make_driver(probe_s=30, probe_max_s=30)
        headset.power_cycle(False)
        headset.dongle_delay = 0.04  # its replies land during the liveness check
        await driver.start()
        assert driver.state["link"] == "offline"
        assert headset.count(RECEIVER, 0x02, 0x13) == 1, "the dongle did reply to the handshake"
        await driver.stop()

    run(go())


def test_slow_first_contact_does_not_cost_a_probe_cycle(run):
    """Captured: the first headset reply after (re)connecting took ~600 ms."""

    async def go():
        driver, headset, _, _ = make_driver(probe_s=30, probe_max_s=30)
        headset.first_contact_delay = 0.2  # > reply_timeout_s (0.05), < first_contact_s (0.3)
        await driver.start()
        assert driver.state["link"] == "online"
        assert headset.count(RECEIVER, 0x02, 0x13) == 1, "connected on the first handshake"
        await driver.stop()

    run(go())


def test_rejected_sidetone_is_reported_not_silently_ignored(run):
    async def go():
        driver, headset, _, _ = make_driver()
        headset.rejected_ops = {0xD1, 0x46, 0x47}
        await driver.start()
        with pytest.raises(DriverError, match="rejected hardware sidetone"):
            await driver.invoke("set_sidetone", {"value": 40})
        assert driver.state["sidetone_supported"] is False
        sent = headset.count(HEADSET, 0x01, 0xD1)
        with pytest.raises(DriverError, match="does not support"):
            await driver.invoke("set_sidetone", {"value": 50})
        assert headset.count(HEADSET, 0x01, 0xD1) == sent, "no more frames once known unsupported"
        await driver.stop()

    run(go())


def test_led_init_is_sent_once_per_connection(run):
    async def go():
        driver, headset, _, _ = make_driver()
        await driver.start()
        await driver.invoke("set_logo_color", {"value": "#00ff00"})
        await driver.invoke("set_logo_color", {"value": "#0000ff"})
        assert headset.count(HEADSET, 0x0D, 0x00) == 1 and len(headset.painted) >= 3
        await driver.stop()

    run(go())


def test_standby_dongle_is_left_alone(run):
    """Headset off: the dongle is another product with nothing to talk to."""

    async def go():
        driver, headset, _, _ = make_driver(pid=STANDBY_PID)
        await driver.start()
        await asyncio.sleep(0.1)
        await driver.stop()
        assert headset.frames == []
        assert driver.state["link"] == "standby" and driver.state["online"] is False
        assert driver.state["sidetone_supported"] is False

    run(go())

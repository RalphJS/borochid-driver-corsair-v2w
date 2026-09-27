import copy

import pytest

from borochid_corsair_v2w import protocol
from borochid_corsair_v2w.profile import Link, Profile, ProfileError

from conftest import MANIFEST


def with_v2w(**changes):
    m = copy.deepcopy(MANIFEST)
    m["v2w"].update(changes)
    return m


def test_profile_parses_links_and_zones():
    p = Profile.from_manifest(MANIFEST)
    assert p.link_for(0x0A3E) is Link.WIRELESS and p.link_for(0x0A3D) is Link.WIRED
    assert [z.kind for z in p.zones] == ["color", "battery", "mic"]


@pytest.mark.parametrize(
    "changes, message",
    [
        ({"links": {}}, "no product ids"),
        ({"links": {"bluetooth": ["0x1"]}}, "bluetooth"),
        ({"features": {"teleport": {}}}, "unknown v2w features"),
        ({"features": {"lighting": {"zones": [{"id": "a", "kind": "laser"}]}}}, "kind must be"),
        ({"features": {"lighting": {"zones": [{"id": "b", "kind": "battery"}]}}}, "needs the battery feature"),
        ({"probe_s": -1}, "non-negative"),
    ],
)
def test_profile_rejects_bad_data(changes, message):
    with pytest.raises(ProfileError, match=message):
        Profile.from_manifest(with_v2w(**changes))


def test_frames_have_the_fixed_report_layout():
    f = protocol.set_mode(protocol.Endpoint.HEADSET, protocol.Mode.SOFTWARE)
    assert len(f) == 65 and f[:7] == bytes([0x00, 0x02, 0x09, 0x01, 0x03, 0x00, 0x02])
    rgb = protocol.rgb([(1, 2, 3), (4, 5, 6)])
    assert rgb[4:15] == bytes([0x00, 6, 0, 0, 0, 1, 4, 2, 5, 3, 6])


def test_input_classification():
    assert protocol.classify(bytes([3, 1, 2, 1])) == protocol.Button(True)
    assert protocol.classify(bytes([3, 1, 1, 0x0F, 0, 0x52, 0x03])) == protocol.Notice(0x0F, 850)
    reply = protocol.classify(bytes([1, 1, 2, 0, 0x52, 0x03]))
    assert isinstance(reply, protocol.Reply) and reply.answers(protocol.Command.SET) and reply.word() == 850
    dongle = protocol.classify(bytes([1, 0, 2, 0, 0x3E, 0x0A]))
    assert dongle.source == protocol.Endpoint.RECEIVER and not dongle.answers(protocol.Command.SET)
    assert not protocol.classify(bytes([1, 1, 0x0D, 3])).ok, "status 03 is a failure"
    assert protocol.battery_percent(0) is None and protocol.battery_percent(1001) is None
    assert protocol.battery_percent(1000) == 100


def test_standby_products_must_have_no_channel():
    m = copy.deepcopy(MANIFEST)
    m["match"][2].pop("channel")
    with pytest.raises(ProfileError, match="standby products need"):
        Profile.from_manifest(m)

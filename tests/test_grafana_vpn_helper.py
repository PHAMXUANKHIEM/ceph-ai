import importlib.util
from pathlib import Path

import pytest


MODULE_PATH = Path(__file__).parents[1] / "scripts" / "deploy" / "grafana_vpn_helper.py"
SPEC = importlib.util.spec_from_file_location("grafana_vpn_helper", MODULE_PATH)
vpn = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(vpn)


PROFILE = b"""client
dev tun
proto udp
remote vpn.example.org 1194
remote-cert-tls server
<ca>
CA DATA
</ca>
<cert>
CERT DATA
</cert>
<key>
KEY DATA
</key>
"""


def test_accepts_inline_certificate_profile():
    assert vpn.validate_profile(PROFILE).startswith("client\n")


@pytest.mark.parametrize("directive", (
    b"script-security 2", b"up /bin/sh", b"plugin /tmp/evil.so",
    b"redirect-gateway def1", b"route 0.0.0.0 0.0.0.0",
    b"pull-filter ignore route", b"auth-user-pass /tmp/secret",
    b"management 127.0.0.1 5555", b"ca /tmp/other.pem",
))
def test_rejects_untrusted_or_route_changing_directives(directive):
    with pytest.raises(ValueError):
        vpn.validate_profile(PROFILE + directive + b"\n")


def test_rejects_networks_used_by_control_plane():
    for address in ("10.3.54.241", "10.20.1.41", "10.89.0.195", "127.0.0.1"):
        with pytest.raises(ValueError):
            vpn.validate_target(address)
    assert vpn.validate_target("172.16.20.10") == "172.16.20.10"

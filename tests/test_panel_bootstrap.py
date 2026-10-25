"""The browser runner's Home Assistant stand-in must offer what setup calls."""

import re
from pathlib import Path

import panel_bootstrap

PACKAGE = Path(__file__).resolve().parents[1] / "custom_components" / "ld2410_tuner"


def test_fake_hass_offers_every_http_method_the_integration_calls(tmp_path):
    called = {
        name
        for path in PACKAGE.rglob("*.py")
        for name in re.findall(r"hass\.http\.(\w+)", path.read_text(encoding="utf-8"))
    }
    assert "register_view" in called
    http = panel_bootstrap.fake_hass(tmp_path).http
    assert sorted(name for name in called if not hasattr(http, name)) == []

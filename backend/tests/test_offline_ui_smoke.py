"""The offline smoke page must not be able to trade or call the network."""

from pathlib import Path

PAGE = Path(__file__).resolve().parents[1] / "tools" / "offline_ui_smoke.html"


def test_smoke_page_has_no_network_or_orders():
    text = PAGE.read_text(encoding="utf-8").lower()
    assert "fetch(" not in text
    assert "websocket" not in text
    assert "xmlhttprequest" not in text
    assert "http://" not in text
    assert "https://" not in text
    assert "api_key" not in text
    assert "private_key" not in text
    assert "place_order" not in text
    assert "stop_loss_selling_enabled" in text
    assert "0.8" in text
    assert "disabled" in text

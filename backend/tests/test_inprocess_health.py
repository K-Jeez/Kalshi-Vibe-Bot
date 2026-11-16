"""In-process /health check with trading tasks replaced by fakes.

This is not a Windows startup and not the dashboard. The real bot loop and
Kalshi client are not constructed. Empty keys are required; values are not printed.
"""

from __future__ import annotations

import socket

from fastapi.testclient import TestClient

from src.config import DEFAULT_STOP_LOSS_DRAWDOWN_PCT, settings
from src.database.models import BotState


def test_health_with_trading_tasks_faked(tmp_path, monkeypatch):
    assert not (settings.kalshi_api_key or "").strip()
    assert not (settings.xai_api_key or "").strip()
    assert settings.stop_loss_selling_enabled is False
    assert settings.stop_loss_drawdown_pct == DEFAULT_STOP_LOSS_DRAWDOWN_PCT == 0.80

    import src.database.models as models

    old_url = settings.database_url
    old_engine = models._engine
    old_session = models._session_factory
    settings.database_url = f"sqlite:///{tmp_path / 'health.db'}"
    if models._engine is not None:
        models._engine.dispose()
    models._engine = None
    models._session_factory = None

    loop_calls = []
    client_inits = []

    class FakeKalshi:
        def __init__(self, api_key, private_key_path, base_url):
            client_inits.append(api_key)
            if (api_key or "").strip():
                raise AssertionError("refusing a nonempty API key")
            del private_key_path, base_url

        async def aclose(self):
            return None

    async def fake_loop(*_args, **_kwargs):
        loop_calls.append("fake")
        return None

    real_connect = socket.socket.connect

    def _block_connect(self, address):
        host = address[0] if isinstance(address, tuple) and address else None
        if host in {"127.0.0.1", "::1", "localhost"}:
            return real_connect(self, address)
        raise AssertionError("network connect is blocked in this harness")

    monkeypatch.setattr("src.main.KalshiClient", FakeKalshi)
    monkeypatch.setattr("src.main.run_bot_loop", fake_loop)
    monkeypatch.setattr(socket.socket, "connect", _block_connect)

    from src.main import app

    try:
        with TestClient(app) as client:
            response = client.get("/health")
            assert response.status_code == 200
            body = response.json()
            assert body["status"] == "healthy"
            assert body["version"]
            session = models.get_session_local()()
            try:
                row = session.query(BotState).filter(BotState.id == 1).one()
                assert row.state == "stop"
            finally:
                session.close()
        assert loop_calls == ["fake"]
        assert client_inits == [""]
    finally:
        if models._engine is not None:
            models._engine.dispose()
        settings.database_url = old_url
        models._engine = old_engine
        models._session_factory = old_session

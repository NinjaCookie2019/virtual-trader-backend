from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.router import StateBroadcaster, attach_routes
from app.core.config import Settings
from app.services.strategy_engine import StrategyEngine
from pathlib import Path
from tempfile import TemporaryDirectory


def build_engine() -> StrategyEngine:
    temp_dir = TemporaryDirectory()
    base_path = Path(temp_dir.name)
    engine = StrategyEngine(
        Settings(
            runtime_state_path=base_path / "runtime_state.json",
            trade_ledger_path=base_path / "trade_ledger.json",
            dhan_token_state_path=base_path / "dhan_token.json",
            dhan_client_id="",
            dhan_access_token="",
        )
    )
    engine._test_temp_dir = temp_dir  # type: ignore[attr-defined]
    return engine


def test_version_endpoint_reports_commit_from_environment(monkeypatch) -> None:
    monkeypatch.setenv("APP_COMMIT_SHA", "abc123")
    monkeypatch.setenv("RAILWAY_DEPLOYMENT_ID", "deploy-1")
    engine = build_engine()
    app = FastAPI()
    attach_routes(app, engine, StateBroadcaster())

    response = TestClient(app).get("/api/version")

    assert response.status_code == 200
    assert response.json() == {
        "app_name": "Virtual Trader",
        "environment": "development",
        "commit": "abc123",
        "deployment_id": "deploy-1",
    }


def test_trade_endpoint_migrates_legacy_gap_oi_basis() -> None:
    engine = build_engine()
    engine.ledger_store.path.write_text(
        """
        {
          "trades": [
            {
              "trade_id": "legacy",
              "side": "BUY",
              "option_type": "PUT",
              "strike": 23350,
              "security_id": "51347",
              "lots": 1,
              "lot_size": 65,
              "quantity": 65,
              "trade_capital": 10000.0,
              "expiry_date": "2026-05-19",
              "entry_price": 116.75,
              "entry_reason": "Previous day low breakdown confirmed by option-chain OI: post-gap fresh OI delta confirmed",
              "entry_spot_price": 23383.8,
              "entry_trigger_price": 23610.3,
              "entry_oi_strike": 23600,
              "entry_oi_rule": "post-gap fresh OI delta confirmed CE 544375.00, PE -11310.00",
              "current_price": 119.65,
              "pnl": 188.5,
              "mode": "paper",
              "highest_price_seen": 129.0,
              "stop_loss_price": 99.2375,
              "target_price": 151.775,
              "status": "CLOSED",
              "opened_at": "2026-05-18T03:50:28.394571Z",
              "closed_at": "2026-05-18T04:02:53.600829Z"
            }
          ]
        }
        """,
        encoding="utf-8",
    )
    app = FastAPI()
    attach_routes(app, engine, StateBroadcaster())

    response = TestClient(app).get("/api/trades?status=ALL")

    assert response.status_code == 200
    trade = response.json()["trades"][0]
    assert trade["entry_oi_basis"] == "legacy_breakout"
    assert trade["entry_oi_reference_price"] == 23610.3


def test_admin_refresh_reference_levels_endpoint_requires_admin_and_calls_engine() -> None:
    engine = build_engine()
    engine.settings.admin_api_key = "secret"
    called = {"count": 0}

    def refresh_reference_levels():
        called["count"] += 1
        return engine.get_snapshot()

    engine.refresh_reference_levels = refresh_reference_levels  # type: ignore[method-assign]
    app = FastAPI()
    attach_routes(app, engine, StateBroadcaster())
    client = TestClient(app)

    forbidden = client.post("/api/admin/refresh-reference-levels")
    assert forbidden.status_code == 403

    response = client.post(
        "/api/admin/refresh-reference-levels",
        headers={"Authorization": "Bearer secret"},
    )

    assert response.status_code == 200
    assert called["count"] == 1

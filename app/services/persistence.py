from __future__ import annotations

import json
from pathlib import Path

from app.models.schemas import ActivityEvent, PositionState, StrategyConfig


class RuntimeStateStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def load(self) -> tuple[StrategyConfig | None, list[ActivityEvent], list[PositionState]]:
        if not self.path.exists():
            return None, [], []

        payload = json.loads(self.path.read_text(encoding="utf-8"))
        config = self._migrate_config(payload.get("config"))
        events = payload.get("events", [])
        trades = [self._migrate_trade(item) for item in payload.get("trade_history", [])]
        parsed_events = [ActivityEvent.model_validate(item) for item in events]
        parsed_trades = [PositionState.model_validate(item) for item in trades]
        return (
            StrategyConfig.model_validate(config) if config else None,
            parsed_events,
            parsed_trades,
        )

    def save(self, config: StrategyConfig, events: list[ActivityEvent], trade_history: list[PositionState]) -> None:
        payload = {
            "config": config.model_dump(mode="json"),
            "events": [event.model_dump(mode="json") for event in events[-200:]],
            "trade_history": [trade.model_dump(mode="json") for trade in trade_history[-200:]],
        }
        self.path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def _migrate_config(self, config: dict | None) -> dict | None:
        if not isinstance(config, dict):
            return config

        migrated = dict(config)
        legacy_quantity = migrated.pop("quantity", None)
        migrated.setdefault("capital_sizing_enabled", True)
        migrated.setdefault("account_capital", 20000.0)
        migrated.setdefault("trade_capital", 10000.0)
        migrated.setdefault("lots", 1)
        if legacy_quantity is not None:
            migrated.setdefault("lot_size", 65)
        else:
            migrated.setdefault("lot_size", 65)
        if int(migrated.get("lot_size") or 0) <= 1:
            migrated["lot_size"] = 65
        return migrated

    def _migrate_trade(self, trade: dict) -> dict:
        migrated = dict(trade)
        legacy_quantity = migrated.get("quantity")
        if migrated.get("lots") is None:
            migrated["lots"] = 1
        if migrated.get("lot_size") is None:
            migrated["lot_size"] = max(int(legacy_quantity), 1) if legacy_quantity is not None else 65
        if legacy_quantity is None:
            migrated["quantity"] = max(int(migrated["lots"]) * int(migrated["lot_size"]), 1)
        if int(migrated.get("lot_size") or 0) <= 1 and int(migrated.get("quantity") or 0) == int(migrated["lots"]):
            migrated["lot_size"] = 65
            migrated["quantity"] = max(int(migrated["lots"]) * int(migrated["lot_size"]), 1)
        migrated.setdefault("trade_capital", 10000.0)
        migrated["trade_value"] = float(migrated.get("entry_price", 0)) * int(migrated["quantity"])
        migrated["pnl"] = (
            float(migrated.get("current_price", 0)) - float(migrated.get("entry_price", 0))
        ) * int(migrated["quantity"])
        return migrated

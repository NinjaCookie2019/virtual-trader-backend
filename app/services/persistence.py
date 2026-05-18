from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from app.models.schemas import ActivityEvent, PositionState, StrategyConfig


def migrate_trade_payload(trade: dict) -> dict:
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
    migrated.setdefault("entry_oi_basis", _infer_entry_oi_basis(migrated))
    migrated.setdefault("entry_oi_reference_price", _infer_entry_oi_reference_price(migrated))
    return migrated


def _nearest_strike(price: float, strike_step: int = 50) -> int:
    return int((price / strike_step) + 0.5) * strike_step


def _infer_entry_oi_basis(trade: dict) -> str | None:
    if not trade.get("entry_oi_strike"):
        return None
    reason = f"{trade.get('entry_reason') or ''} {trade.get('entry_oi_rule') or ''}".lower()
    spot_price = trade.get("entry_spot_price")
    if spot_price is not None and "post-gap" in reason:
        try:
            if int(trade["entry_oi_strike"]) != _nearest_strike(float(spot_price)):
                return "legacy_breakout"
        except (TypeError, ValueError):
            return "legacy_breakout"
    return "breakout"


def _infer_entry_oi_reference_price(trade: dict) -> float | None:
    basis = trade.get("entry_oi_basis") or _infer_entry_oi_basis(trade)
    if basis == "atm":
        candidate = trade.get("entry_spot_price")
    else:
        candidate = trade.get("entry_trigger_price")
    try:
        return float(candidate) if candidate is not None else None
    except (TypeError, ValueError):
        return None


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
        trades = [migrate_trade_payload(item) for item in payload.get("trade_history", [])]
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
        migrated.setdefault("oi_confirmation_enabled", True)
        migrated.setdefault("gap_open_filter_enabled", True)
        migrated.setdefault("gap_open_continuation_points", 15.0)
        migrated.setdefault("gap_open_option_premium_min_move_percent", 3.0)
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


class TradeLedgerStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def load(self) -> list[PositionState]:
        if not self.path.exists():
            return []
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return []

        raw_trades = payload.get("trades") if isinstance(payload, dict) else payload
        if not isinstance(raw_trades, list):
            return []

        parsed: list[PositionState] = []
        for item in raw_trades:
            if not isinstance(item, dict):
                continue
            try:
                parsed.append(PositionState.model_validate(migrate_trade_payload(item)))
            except Exception:
                continue
        return self._dedupe(parsed)

    def replace_all(self, trades: list[PositionState]) -> None:
        ordered = self._dedupe(trades)
        payload = {
            "trades": [trade.model_dump(mode="json") for trade in ordered],
        }
        self.path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def upsert(self, trade: PositionState) -> None:
        trades = [existing for existing in self.load() if existing.trade_id != trade.trade_id]
        trades.append(trade.model_copy(deep=True))
        self.replace_all(trades)

    def remove(self, trade_id: str) -> None:
        self.replace_all([trade for trade in self.load() if trade.trade_id != trade_id])

    def query(
        self,
        *,
        from_date: str | None = None,
        to_date: str | None = None,
        status: str | None = None,
        option_type: str | None = None,
        limit: int | None = None,
    ) -> list[PositionState]:
        trades = self.load()
        filtered: list[PositionState] = []
        for trade in trades:
            trade_date = self._trade_date_key(trade)
            if from_date and trade_date < from_date:
                continue
            if to_date and trade_date > to_date:
                continue
            if status and status != "ALL" and trade.status != status:
                continue
            if option_type and option_type != "ALL" and trade.option_type != option_type:
                continue
            filtered.append(trade)

        filtered.sort(key=lambda trade: trade.closed_at or trade.opened_at, reverse=True)
        return filtered[:limit] if limit else filtered

    @staticmethod
    def _dedupe(trades: list[PositionState]) -> list[PositionState]:
        by_id: dict[str, PositionState] = {}
        for trade in trades:
            by_id[trade.trade_id] = trade.model_copy(deep=True)
        return sorted(by_id.values(), key=lambda trade: trade.closed_at or trade.opened_at)

    @staticmethod
    def _trade_date_key(trade: PositionState) -> str:
        trade_time = trade.closed_at or trade.opened_at
        return trade_time.date().isoformat()

    @staticmethod
    def _migrate_trade(trade: dict) -> dict:
        return migrate_trade_payload(trade)


class DhanTokenStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def load(self) -> dict | None:
        if not self.path.exists():
            return None
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None
        if not isinstance(payload, dict) or not payload.get("access_token"):
            return None
        return payload

    def save(
        self,
        *,
        access_token: str,
        token_valid_until: datetime | None,
        renewed_at: datetime,
    ) -> None:
        payload = {
            "access_token": access_token,
            "token_valid_until": token_valid_until.isoformat() if token_valid_until else None,
            "renewed_at": renewed_at.isoformat(),
        }
        self.path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class ActivityEvent(BaseModel):
    id: str
    timestamp: datetime
    level: Literal["info", "warn", "error", "trade"] = "info"
    title: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class StrategyConfig(BaseModel):
    enabled: bool = False
    paper_trading: bool = True
    capital_sizing_enabled: bool = True
    account_capital: float = 20000.0
    trade_capital: float = 10000.0
    lots: int = 1
    lot_size: int = 65
    product_type: Literal["INTRADAY", "MARGIN", "CNC"] = "INTRADAY"
    strike_step: int = 50
    breakout_buffer: float = 0.0
    oi_confirmation_enabled: bool = True
    max_trades_per_day: int = 2
    cooldown_seconds: int = 120
    stop_loss_percent: float = 15.0
    target_percent: float = 30.0
    trailing_stop_enabled: bool = True
    trailing_activation_percent: float = 15.0
    trailing_distance_percent: float = 10.0
    auto_close_enabled: bool = True
    auto_close_time: str = "15:15"
    reverse_signal_exit_enabled: bool = True


class ConnectionState(BaseModel):
    configured: bool = False
    api_ready: bool = False
    market_feed_connected: bool = False
    order_updates_connected: bool = False
    last_error: str | None = None
    token_auto_renew_enabled: bool = False
    token_valid_until: datetime | None = None
    token_last_checked_at: datetime | None = None
    token_last_renewed_at: datetime | None = None
    token_renewal_status: Literal["idle", "valid", "renewing", "renewed", "expired", "error"] = "idle"


class ReferenceLevels(BaseModel):
    previous_day_high: float | None = None
    previous_day_low: float | None = None
    source_date: str | None = None
    expiry_date: str | None = None
    updated_at: datetime | None = None


class SelectedInstrument(BaseModel):
    option_type: Literal["CALL", "PUT"]
    strike: int
    security_id: str
    exchange_segment: str = "NSE_FNO"
    expiry_date: str
    last_price: float | None = None
    top_bid_price: float | None = None
    top_ask_price: float | None = None


class PositionState(BaseModel):
    trade_id: str
    side: Literal["BUY"]
    option_type: Literal["CALL", "PUT"]
    strike: int
    security_id: str
    lots: int
    lot_size: int
    quantity: int
    trade_capital: float
    trade_value: float
    expiry_date: str
    entry_price: float
    entry_reason: str | None = None
    entry_spot_price: float | None = None
    entry_trigger_price: float | None = None
    entry_oi_strike: int | None = None
    entry_ce_change_oi: float | None = None
    entry_pe_change_oi: float | None = None
    entry_oi_rule: str | None = None
    entry_reference_high: float | None = None
    entry_reference_low: float | None = None
    current_price: float
    pnl: float
    mode: Literal["paper", "live"]
    highest_price_seen: float
    stop_loss_price: float
    target_price: float
    trailing_stop_price: float | None = None
    trailing_armed: bool = False
    exit_reason: str | None = None
    exit_reason_detail: str | None = None
    exit_trigger_price: float | None = None
    exit_requested: bool = False
    status: Literal["OPEN", "CLOSED"] = "OPEN"
    opened_at: datetime
    closed_at: datetime | None = None
    order_id: str | None = None
    exit_order_id: str | None = None


class StrategyRuntime(BaseModel):
    spot_price: float | None = None
    previous_spot_price: float | None = None
    spot_updated_at: datetime | None = None
    selected_instrument: SelectedInstrument | None = None
    open_position: PositionState | None = None
    trade_history: list[PositionState] = Field(default_factory=list)
    exit_in_progress: bool = False
    trades_today: int = 0
    last_signal: Literal["CALL", "PUT"] | None = None
    last_signal_at: datetime | None = None
    previous_high_broken: bool = False
    previous_low_broken: bool = False
    session_date: str | None = None
    market_session_open: bool = False
    next_trade_window_starts_at: datetime | None = None


class StrategySnapshot(BaseModel):
    app_name: str
    generated_at: datetime
    config: StrategyConfig
    connections: ConnectionState
    reference_levels: ReferenceLevels
    runtime: StrategyRuntime
    events: list[ActivityEvent]


class ConfigUpdateRequest(BaseModel):
    enabled: bool | None = None
    paper_trading: bool | None = None
    capital_sizing_enabled: bool | None = None
    account_capital: float | None = None
    trade_capital: float | None = None
    lots: int | None = None
    lot_size: int | None = None
    product_type: Literal["INTRADAY", "MARGIN", "CNC"] | None = None
    strike_step: int | None = None
    breakout_buffer: float | None = None
    oi_confirmation_enabled: bool | None = None
    max_trades_per_day: int | None = None
    cooldown_seconds: int | None = None
    stop_loss_percent: float | None = None
    target_percent: float | None = None
    trailing_stop_enabled: bool | None = None
    trailing_activation_percent: float | None = None
    trailing_distance_percent: float | None = None
    auto_close_enabled: bool | None = None
    auto_close_time: str | None = None
    reverse_signal_exit_enabled: bool | None = None


class ActionResponse(BaseModel):
    ok: bool = True
    message: str
    snapshot: StrategySnapshot

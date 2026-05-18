from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "Virtual Trader"
    app_env: str = "development"
    app_timezone: str = "Asia/Kolkata"
    frontend_origin: str = "http://localhost:5173"
    runtime_state_path: Path = Field(default=Path("app/storage/runtime_state.json"))
    trade_ledger_path: Path = Field(default=Path("app/storage/trade_ledger.json"))
    admin_api_key: str = ""
    scheduler_secret: str = ""

    dhan_client_id: str = ""
    dhan_access_token: str = ""
    dhan_disable_ssl: bool = False
    dhan_auto_renew_enabled: bool = True
    dhan_token_state_path: Path = Field(default=Path("app/storage/dhan_token.json"))
    dhan_token_check_seconds: float = 1800.0
    dhan_token_renew_buffer_minutes: float = 90.0

    underlying_name: str = "NIFTY 50"
    underlying_security_id: str = "13"
    underlying_exchange_segment: str = "IDX_I"
    underlying_instrument_type: str = "INDEX"

    default_capital_sizing_enabled: bool = True
    default_account_capital: float = 20000.0
    default_trade_capital: float = 10000.0
    default_lots: int = 1
    default_lot_size: int = 65
    default_product_type: str = "INTRADAY"
    default_strategy_enabled: bool = True
    default_paper_trading: bool = True
    default_max_trades_per_day: int = 2
    default_cooldown_seconds: int = 120
    default_breakout_buffer: float = 15.0
    default_breakout_confirmation_ticks: int = 3
    default_breakout_confirmation_seconds: float = 30.0
    default_second_trade_extra_buffer: float = 15.0
    default_no_trade_before_time: str = "09:20"
    default_oi_confirmation_enabled: bool = True
    default_gap_open_filter_enabled: bool = True
    default_gap_open_continuation_points: float = 15.0
    default_gap_open_option_premium_min_move_percent: float = 3.0
    default_stop_loss_percent: float = 15.0
    default_target_percent: float = 30.0
    default_trailing_stop_enabled: bool = True
    default_trailing_activation_percent: float = 8.0
    default_trailing_distance_percent: float = 5.0
    default_time_decay_exit_enabled: bool = True
    default_time_decay_exit_minutes: int = 12
    default_time_decay_min_profit_percent: float = 5.0
    default_auto_close_enabled: bool = True
    default_auto_close_time: str = "15:15"
    default_reverse_signal_exit_enabled: bool = False
    default_reclaim_exit_enabled: bool = True
    default_reclaim_exit_buffer: float = 5.0
    strike_step: int = 50
    option_chain_poll_seconds: float = 3.2
    option_quote_poll_seconds: float = 2.0
    market_feed_retry_seconds: float = 5.0
    market_feed_max_retry_seconds: float = 60.0

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    settings = Settings()
    if not settings.runtime_state_path.is_absolute():
        settings.runtime_state_path = Path(__file__).resolve().parents[2] / settings.runtime_state_path
    if not settings.trade_ledger_path.is_absolute():
        settings.trade_ledger_path = Path(__file__).resolve().parents[2] / settings.trade_ledger_path
    if not settings.dhan_token_state_path.is_absolute():
        settings.dhan_token_state_path = Path(__file__).resolve().parents[2] / settings.dhan_token_state_path
    return settings

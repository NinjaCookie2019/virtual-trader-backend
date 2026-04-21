from __future__ import annotations

from tempfile import TemporaryDirectory
from pathlib import Path

from app.core.config import Settings
from app.services.dhan_gateway import OptionContract
from app.services.strategy_engine import StrategyEngine


def build_engine() -> StrategyEngine:
    temp_dir = TemporaryDirectory()
    base_path = Path(temp_dir.name)
    engine = StrategyEngine(
        Settings(
            runtime_state_path=base_path / "runtime_state.json",
            dhan_token_state_path=base_path / "dhan_token.json",
            dhan_client_id="",
            dhan_access_token="",
            default_cooldown_seconds=0,
            default_max_trades_per_day=2,
        )
    )
    engine._test_temp_dir = temp_dir  # type: ignore[attr-defined]
    engine._is_market_session_open = lambda: True  # type: ignore[method-assign]
    engine.config.enabled = True
    engine.config.cooldown_seconds = 0
    engine.config.max_trades_per_day = 2
    engine.reference_levels.previous_day_high = 100.0
    engine.reference_levels.previous_day_low = 90.0
    engine.reference_levels.expiry_date = "2026-04-21"
    return engine


def test_high_breakout_rearms_only_after_spot_returns_below_high() -> None:
    engine = build_engine()
    triggered: list[tuple[str, float]] = []
    engine._trigger_trade = lambda option_type, spot_price: triggered.append((option_type, spot_price))  # type: ignore[method-assign]

    engine.runtime.spot_price = 101.0
    engine.runtime.trades_today = 1
    engine.runtime.previous_high_broken = True

    engine.handle_market_tick({"LTP": 102.0})
    assert triggered == []
    assert engine.runtime.previous_high_broken is True

    engine.handle_market_tick({"LTP": 99.0})
    assert triggered == []
    assert engine.runtime.previous_high_broken is False

    engine.handle_market_tick({"LTP": 101.0})
    assert triggered == [("CALL", 101.0)]


def test_low_breakout_rearms_only_after_spot_returns_above_low() -> None:
    engine = build_engine()
    triggered: list[tuple[str, float]] = []
    engine._trigger_trade = lambda option_type, spot_price: triggered.append((option_type, spot_price))  # type: ignore[method-assign]

    engine.runtime.spot_price = 89.0
    engine.runtime.trades_today = 1
    engine.runtime.previous_low_broken = True

    engine.handle_market_tick({"LTP": 88.0})
    assert triggered == []
    assert engine.runtime.previous_low_broken is True

    engine.handle_market_tick({"LTP": 91.0})
    assert triggered == []
    assert engine.runtime.previous_low_broken is False

    engine.handle_market_tick({"LTP": 89.0})
    assert triggered == [("PUT", 89.0)]


def test_restart_hydrates_today_call_breakout_lock_from_trade_history() -> None:
    engine = build_engine()
    contract = OptionContract(
        option_type="CALL",
        strike=150,
        security_id="test-call",
        exchange_segment="NSE_FNO",
        expiry_date="2026-04-21",
        last_price=10.0,
        top_bid_price=9.9,
        top_ask_price=10.0,
    )
    trade = engine._build_position_state(
        contract=contract,
        fill_price=10.0,
        lots=1,
        quantity=65,
        trade_value=650.0,
        mode="paper",
        order_id="paper-test",
        entry_spot_price=101.0,
    )
    trade.status = "CLOSED"
    trade.closed_at = engine._now()
    engine.runtime.trade_history.append(trade)
    engine.runtime.previous_high_broken = False
    engine.runtime.trades_today = 0

    engine._hydrate_session_state_from_history(engine._today_session_date())

    triggered: list[tuple[str, float]] = []
    engine._trigger_trade = lambda option_type, spot_price: triggered.append((option_type, spot_price))  # type: ignore[method-assign]
    engine._evaluate_breakout_from_state(102.0)

    assert engine.runtime.trades_today == 1
    assert engine.runtime.previous_high_broken is True
    assert triggered == []

from __future__ import annotations

from datetime import timedelta
from tempfile import TemporaryDirectory
from pathlib import Path

from app.models.schemas import ActivityEvent
from app.core.config import Settings
from app.services.dhan_gateway import DhanGatewayError, OptionContract, OptionOiSignal
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


def test_breakout_does_not_trade_before_configured_entry_time() -> None:
    engine = build_engine()
    triggered: list[tuple[str, float]] = []
    engine._trigger_trade = lambda option_type, spot_price: triggered.append((option_type, spot_price))  # type: ignore[method-assign]
    engine._is_trade_entry_window_open = lambda: False  # type: ignore[method-assign]

    engine.runtime.spot_price = 99.0
    engine.handle_market_tick({"LTP": 101.0})

    assert triggered == []


def test_gap_down_is_evaluated_when_entry_window_opens() -> None:
    engine = build_engine()
    triggered: list[tuple[str, float]] = []
    entry_window_open = False

    engine._trigger_trade = lambda option_type, spot_price: triggered.append((option_type, spot_price))  # type: ignore[method-assign]
    engine._is_trade_entry_window_open = lambda: entry_window_open  # type: ignore[method-assign]

    engine.handle_market_tick({"LTP": 89.0})
    assert triggered == []

    entry_window_open = True
    engine.handle_market_tick({"LTP": 88.0})

    assert triggered == [("PUT", 88.0)]


def test_capital_sizing_takes_one_lot_when_trade_budget_is_too_small_but_equity_allows() -> None:
    engine = build_engine()
    engine.config.capital_sizing_enabled = True
    engine.config.account_capital = 20000.0
    engine.config.trade_capital = 10000.0
    engine.config.lot_size = 65

    assert engine._calculate_trade_size(224.40) == (1, 65, 14586.0)


def test_capital_sizing_skips_when_one_lot_exceeds_account_equity() -> None:
    engine = build_engine()
    engine.config.capital_sizing_enabled = True
    engine.config.account_capital = 12000.0
    engine.config.trade_capital = 10000.0
    engine.config.lot_size = 65

    assert engine._calculate_trade_size(224.40) is None


def test_time_decay_exit_triggers_after_12_minutes_without_minimum_profit() -> None:
    engine = build_engine()
    contract = OptionContract(
        option_type="CALL",
        strike=150,
        security_id="test-call",
        exchange_segment="NSE_FNO",
        expiry_date="2026-04-21",
        last_price=100.0,
        top_bid_price=99.0,
        top_ask_price=100.0,
    )
    position = engine._build_position_state(
        contract=contract,
        fill_price=100.0,
        lots=1,
        quantity=65,
        trade_value=6500.0,
        mode="paper",
        order_id="paper-time-decay",
        entry_spot_price=101.0,
        oi_signal=None,
    )
    position.opened_at = engine._now() - timedelta(minutes=12, seconds=1)

    assert engine._is_time_decay_exit_due(position, 104.0) is True
    assert engine._is_time_decay_exit_due(position, 105.0) is False


def test_time_decay_exit_waits_before_12_minutes() -> None:
    engine = build_engine()
    contract = OptionContract(
        option_type="PUT",
        strike=50,
        security_id="test-put",
        exchange_segment="NSE_FNO",
        expiry_date="2026-04-21",
        last_price=100.0,
        top_bid_price=99.0,
        top_ask_price=100.0,
    )
    position = engine._build_position_state(
        contract=contract,
        fill_price=100.0,
        lots=1,
        quantity=65,
        trade_value=6500.0,
        mode="paper",
        order_id="paper-time-decay",
        entry_spot_price=89.0,
        oi_signal=None,
    )
    position.opened_at = engine._now() - timedelta(minutes=11, seconds=59)

    assert engine._is_time_decay_exit_due(position, 80.0) is False


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
        oi_signal=None,
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


def test_profitable_trade_today_blocks_new_entries_even_when_daily_cap_allows() -> None:
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
        order_id="paper-profit",
        entry_spot_price=101.0,
        oi_signal=None,
    )
    trade.status = "CLOSED"
    trade.closed_at = engine._now()
    trade.pnl = 325.0
    engine.runtime.trade_history.append(trade)
    engine.runtime.trades_today = 1
    engine.runtime.previous_high_broken = False

    triggered: list[tuple[str, float]] = []
    engine._trigger_trade = lambda option_type, spot_price: triggered.append((option_type, spot_price))  # type: ignore[method-assign]
    engine._evaluate_breakout(previous_spot=99.0, spot_price=101.0)
    engine._evaluate_breakout_from_state(101.0)

    assert triggered == []


def test_losing_trade_today_can_retry_when_daily_cap_allows() -> None:
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
        order_id="paper-loss",
        entry_spot_price=101.0,
        oi_signal=None,
    )
    trade.status = "CLOSED"
    trade.closed_at = engine._now()
    trade.pnl = -130.0
    engine.runtime.trade_history.append(trade)
    engine.runtime.trades_today = 1
    engine.runtime.previous_high_broken = False

    triggered: list[tuple[str, float]] = []
    engine._trigger_trade = lambda option_type, spot_price: triggered.append((option_type, spot_price))  # type: ignore[method-assign]
    engine._evaluate_breakout(previous_spot=99.0, spot_price=101.0)

    assert triggered == [("CALL", 101.0)]


def test_trade_reset_lock_hydrates_breakout_without_counting_trade() -> None:
    engine = build_engine()
    engine.events.append(
        ActivityEvent(
            id="reset-lock",
            timestamp=engine._now(),
            level="warn",
            title="Trade Reset Lock",
            message="Today trade removed; keep PUT side locked until rearm.",
            details={
                "session_date": engine._today_session_date(),
                "option_type": "PUT",
            },
        )
    )

    engine._hydrate_session_state_from_history(engine._today_session_date())

    triggered: list[tuple[str, float]] = []
    engine._trigger_trade = lambda option_type, spot_price: triggered.append((option_type, spot_price))  # type: ignore[method-assign]
    engine._evaluate_breakout_from_state(89.0)

    assert engine.runtime.trades_today == 0
    assert engine.runtime.previous_low_broken is True
    assert engine.runtime.previous_high_broken is False
    assert triggered == []


def test_reset_today_trade_removes_trade_and_locks_side() -> None:
    engine = build_engine()
    contract = OptionContract(
        option_type="PUT",
        strike=50,
        security_id="test-put",
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
        entry_spot_price=89.0,
        oi_signal=None,
    )
    trade.status = "CLOSED"
    trade.closed_at = engine._now()
    engine.runtime.trade_history.append(trade)
    engine._log("trade", "Paper Trade Opened", "Bought PUT.")
    engine._hydrate_session_state_from_history(engine._today_session_date())

    snapshot = engine.reset_today_trade(trade.trade_id)

    assert snapshot.runtime.trades_today == 0
    assert snapshot.runtime.previous_low_broken is True
    assert snapshot.runtime.trade_history == []
    assert snapshot.events[-1].title == "Trade Reset Lock"


def test_pending_call_breakout_is_rechecked_while_spot_stays_above_high() -> None:
    engine = build_engine()
    triggered: list[tuple[str, float]] = []
    engine._trigger_trade = lambda option_type, spot_price: triggered.append((option_type, spot_price))  # type: ignore[method-assign]
    engine.pending_oi_breakouts["CALL"] = OptionOiSignal(
        option_type="CALL",
        strike=100,
        ce_change_oi=50.0,
        pe_change_oi=45.0,
        confirmed=False,
        rule="PE change OI > CE change OI",
    )

    engine._evaluate_breakout(previous_spot=101.0, spot_price=102.0)

    assert triggered == [("CALL", 102.0)]


def test_call_oi_confirmation_allows_trade_when_resistance_decreases() -> None:
    engine = build_engine()
    engine.pending_oi_breakouts["CALL"] = OptionOiSignal(
        option_type="CALL",
        strike=100,
        ce_change_oi=50.0,
        pe_change_oi=45.0,
        confirmed=False,
        rule="PE change OI > CE change OI",
    )

    confirmed = engine._weakening_oi_confirmation(
        OptionOiSignal(
            option_type="CALL",
            strike=100,
            ce_change_oi=49.0,
            pe_change_oi=45.0,
            confirmed=False,
            rule="PE change OI > CE change OI",
        )
    )

    assert confirmed is not None
    assert confirmed.confirmed is True
    assert "CE resistance change OI decreasing" in confirmed.rule


def test_token_renewal_attempts_renew_when_validity_check_fails() -> None:
    engine = build_engine()
    engine.settings.dhan_client_id = "client"
    engine.settings.dhan_access_token = "old-token"
    engine.gateway.settings.dhan_client_id = "client"
    engine.gateway.settings.dhan_access_token = "old-token"
    engine.gateway.client = object()
    calls: list[str] = []

    def fail_token_check():
        calls.append("check")
        raise DhanGatewayError("Dhan profile request failed: HTTP 401")

    def renew_token():
        calls.append("renew")
        return "new-token", None, {"accessToken": "new-token"}

    engine.gateway.fetch_token_valid_until = fail_token_check  # type: ignore[method-assign]
    engine.gateway.renew_access_token = renew_token  # type: ignore[method-assign]
    engine._restart_dhan_streams = lambda: None  # type: ignore[method-assign]

    engine._check_and_renew_token(force=False)

    assert calls == ["check", "renew", "check"]
    assert engine.connections.token_renewal_status == "error"
    assert "Token check failed before renewal" in (engine.connections.last_error or "")


def test_token_renewal_renews_when_validity_is_unknown() -> None:
    engine = build_engine()
    engine.settings.dhan_client_id = "client"
    engine.settings.dhan_access_token = "old-token"
    engine.gateway.settings.dhan_client_id = "client"
    engine.gateway.settings.dhan_access_token = "old-token"
    engine.gateway.client = object()
    calls: list[str] = []

    def unknown_validity():
        calls.append("check")
        return None

    def renew_token():
        calls.append("renew")
        return "new-token", engine._now(), {"accessToken": "new-token"}

    engine.gateway.fetch_token_valid_until = unknown_validity  # type: ignore[method-assign]
    engine.gateway.renew_access_token = renew_token  # type: ignore[method-assign]
    engine._restart_dhan_streams = lambda: None  # type: ignore[method-assign]

    engine._check_and_renew_token(force=False)

    assert calls == ["check", "renew"]
    assert engine.connections.token_renewal_status == "renewed"
    assert engine.connections.last_error is None


def test_saved_token_is_ignored_after_likely_expiry() -> None:
    engine = build_engine()
    stale_token = {
        "access_token": "stale-token",
        "token_valid_until": None,
        "renewed_at": (engine._now() - timedelta(hours=24)).isoformat(),
    }

    assert engine._should_use_saved_token(stale_token) is False


def test_saved_token_is_used_when_recently_renewed() -> None:
    engine = build_engine()
    recent_token = {
        "access_token": "recent-token",
        "token_valid_until": None,
        "renewed_at": (engine._now() - timedelta(hours=2)).isoformat(),
    }

    assert engine._should_use_saved_token(recent_token) is True

from __future__ import annotations

from tempfile import TemporaryDirectory
from pathlib import Path

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

from __future__ import annotations

from datetime import timedelta
from tempfile import TemporaryDirectory
from pathlib import Path

from app.models.schemas import ActivityEvent
from app.core.config import Settings
from app.services.dhan_gateway import DhanGatewayError, OptionContract, OptionOiSignal
from app.services.persistence import RuntimeStateStore, migrate_trade_payload
from app.services.strategy_engine import OpeningGapLock, StrategyEngine


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
            default_cooldown_seconds=0,
            default_max_trades_per_day=2,
        )
    )
    engine._test_temp_dir = temp_dir  # type: ignore[attr-defined]
    engine._is_market_session_open = lambda: True  # type: ignore[method-assign]
    engine._is_trade_entry_window_open = lambda: True  # type: ignore[method-assign]
    engine.config.enabled = True
    engine.config.cooldown_seconds = 0
    engine.config.max_trades_per_day = 2
    engine.config.breakout_buffer = 0.0
    engine.config.breakout_confirmation_ticks = 1
    engine.config.breakout_confirmation_seconds = 0.0
    engine.config.second_trade_extra_buffer = 0.0
    engine.reference_levels.previous_day_high = 100.0
    engine.reference_levels.previous_day_low = 90.0
    engine.reference_levels.expiry_date = "2026-04-21"
    return engine


def test_default_oi_thresholds_use_replay_supported_values() -> None:
    engine = build_engine()

    assert engine.config.oi_confirmation_min_edge_change_oi == 650000.0
    assert engine.config.oi_confirmation_min_edge_percent == 12.0


def test_runtime_config_migration_upgrades_legacy_oi_threshold_pair() -> None:
    with TemporaryDirectory() as temp_dir:
        state_path = Path(temp_dir) / "runtime_state.json"
        state_path.write_text(
            """
            {
              "config": {
                "enabled": true,
                "paper_trading": true,
                "oi_confirmation_min_edge_change_oi": 500000.0,
                "oi_confirmation_min_edge_percent": 10.0
              },
              "events": [],
              "trade_history": []
            }
            """,
            encoding="utf-8",
        )

        config, _, _ = RuntimeStateStore(state_path).load()

    assert config is not None
    assert config.oi_confirmation_min_edge_change_oi == 650000.0
    assert config.oi_confirmation_min_edge_percent == 12.0


def test_runtime_config_migration_preserves_stronger_oi_thresholds() -> None:
    with TemporaryDirectory() as temp_dir:
        state_path = Path(temp_dir) / "runtime_state.json"
        state_path.write_text(
            """
            {
              "config": {
                "enabled": true,
                "paper_trading": true,
                "oi_confirmation_min_edge_change_oi": 1000000.0,
                "oi_confirmation_min_edge_percent": 20.0
              },
              "events": [],
              "trade_history": []
            }
            """,
            encoding="utf-8",
        )

        config, _, _ = RuntimeStateStore(state_path).load()

    assert config is not None
    assert config.oi_confirmation_min_edge_change_oi == 1000000.0
    assert config.oi_confirmation_min_edge_percent == 20.0


def make_closed_trade(
    engine: StrategyEngine,
    *,
    option_type: str = "CALL",
    entry_spot_price: float = 101.0,
    pnl: float = 0.0,
    exit_reason: str | None = None,
):
    contract = OptionContract(
        option_type=option_type,
        strike=150 if option_type == "CALL" else 50,
        security_id=f"test-{option_type.lower()}",
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
        order_id=f"paper-{option_type.lower()}",
        entry_spot_price=entry_spot_price,
        oi_signal=None,
    )
    trade.status = "CLOSED"
    trade.closed_at = engine._now()
    trade.pnl = pnl
    trade.exit_reason = exit_reason
    return trade


def test_price_breakout_requires_sustained_ticks_before_trade() -> None:
    engine = build_engine()
    engine.config.breakout_confirmation_ticks = 2
    triggered: list[tuple[str, float]] = []
    engine._trigger_trade = lambda option_type, spot_price: triggered.append((option_type, spot_price))  # type: ignore[method-assign]

    engine._evaluate_breakout(previous_spot=99.0, spot_price=101.0)

    assert triggered == []
    assert "CALL" in engine.pending_price_breakouts

    engine._evaluate_breakout(previous_spot=101.0, spot_price=102.0)

    assert triggered == [("CALL", 102.0)]
    assert "CALL" not in engine.pending_price_breakouts


def test_reference_refresh_due_when_same_session_source_date_is_stale() -> None:
    engine = build_engine()
    engine._today_session_date = lambda: "2026-06-04"  # type: ignore[method-assign]
    engine.runtime.session_date = "2026-06-04"
    engine.reference_levels.source_date = "2026-06-02"
    engine.last_reference_refresh_attempt_at = None

    assert engine._reference_refresh_due(engine.runtime.session_date) is True


def test_reference_refresh_not_due_when_source_date_matches_previous_weekday() -> None:
    engine = build_engine()
    engine._today_session_date = lambda: "2026-06-04"  # type: ignore[method-assign]
    engine.runtime.session_date = "2026-06-04"
    engine.reference_levels.source_date = "2026-06-03"
    engine.last_reference_refresh_attempt_at = None

    assert engine._reference_refresh_due(engine.runtime.session_date) is False


def test_reference_refresh_due_honors_retry_throttle() -> None:
    engine = build_engine()
    engine._today_session_date = lambda: "2026-06-04"  # type: ignore[method-assign]
    engine.runtime.session_date = "2026-06-04"
    engine.reference_levels.source_date = "2026-06-02"
    engine.last_reference_refresh_attempt_at = engine._now()

    assert engine._reference_refresh_due(engine.runtime.session_date) is False

    engine.last_reference_refresh_attempt_at = engine._now() - timedelta(
        seconds=engine.settings.reference_level_retry_seconds + 1
    )

    assert engine._reference_refresh_due(engine.runtime.session_date) is True


def test_refresh_reference_levels_warns_when_dhan_returns_stale_source_date() -> None:
    engine = build_engine()
    engine._today_session_date = lambda: "2026-06-04"  # type: ignore[method-assign]

    class FakeGateway:
        def fetch_previous_day_levels(self):
            return 23556.95, 23229.15, "2026-06-02"

        def fetch_expiry_list(self):
            return ["2026-06-09"]

    engine.gateway = FakeGateway()  # type: ignore[assignment]

    engine.refresh_reference_levels()

    assert engine.reference_levels.source_date == "2026-06-02"
    stale_events = [event for event in engine.events if event.title == "Reference Levels Stale"]
    assert stale_events
    assert stale_events[-1].details == {
        "source_date": "2026-06-02",
        "expected_source_date": "2026-06-03",
        "session_date": "2026-06-04",
    }


def test_price_breakout_confirmation_resets_when_spot_reclaims_trigger() -> None:
    engine = build_engine()
    engine.config.breakout_confirmation_ticks = 2
    triggered: list[tuple[str, float]] = []
    engine._trigger_trade = lambda option_type, spot_price: triggered.append((option_type, spot_price))  # type: ignore[method-assign]

    engine._evaluate_breakout(previous_spot=99.0, spot_price=101.0)
    engine._rearm_breakout_flags(99.0)
    engine._evaluate_breakout(previous_spot=99.0, spot_price=101.0)

    assert triggered == []
    assert engine.pending_price_breakouts["CALL"].count == 1

    engine._evaluate_breakout(previous_spot=101.0, spot_price=102.0)

    assert triggered == [("CALL", 102.0)]


def test_stop_loss_today_blocks_new_entries() -> None:
    engine = build_engine()
    engine.runtime.trade_history.append(make_closed_trade(engine, pnl=-130.0, exit_reason="stop_loss"))
    engine.runtime.trades_today = 1
    triggered: list[tuple[str, float]] = []
    engine._trigger_trade = lambda option_type, spot_price: triggered.append((option_type, spot_price))  # type: ignore[method-assign]

    engine._evaluate_breakout(previous_spot=99.0, spot_price=101.0)

    assert triggered == []
    assert engine.events[-1].title == "Daily Kill Switch"


def test_profit_lock_blocks_new_entries_after_daily_threshold() -> None:
    engine = build_engine()
    engine.config.daily_profit_lock_enabled = True
    engine.config.daily_profit_lock_amount = 1000.0
    engine.runtime.trade_history.append(make_closed_trade(engine, pnl=1200.0, exit_reason="target"))
    engine.runtime.trades_today = 1
    triggered: list[tuple[str, float]] = []
    engine._trigger_trade = lambda option_type, spot_price: triggered.append((option_type, spot_price))  # type: ignore[method-assign]

    engine._evaluate_breakout(previous_spot=99.0, spot_price=101.0)

    assert triggered == []
    assert engine.events[-1].title == "Daily Profit Locked"


def test_profitable_first_trade_below_lock_uses_stricter_second_breakout_trigger() -> None:
    engine = build_engine()
    engine.config.second_trade_extra_buffer = 10.0
    engine.runtime.trade_history.append(make_closed_trade(engine, pnl=325.0, exit_reason="target"))
    engine.runtime.trades_today = 1
    triggered: list[tuple[str, float]] = []
    engine._trigger_trade = lambda option_type, spot_price: triggered.append((option_type, spot_price))  # type: ignore[method-assign]

    engine._evaluate_breakout(previous_spot=99.0, spot_price=105.0)
    engine._evaluate_breakout(previous_spot=105.0, spot_price=111.0)

    assert triggered == [("CALL", 111.0)]


def test_reclaim_exit_closes_call_when_spot_falls_back_below_high() -> None:
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
    engine.config.reverse_signal_exit_enabled = False
    engine.config.reclaim_exit_enabled = True
    engine.config.reclaim_exit_buffer = 5.0
    engine.runtime.open_position = engine._build_position_state(
        contract=contract,
        fill_price=10.0,
        lots=1,
        quantity=65,
        trade_value=650.0,
        mode="paper",
        order_id="paper-reclaim",
        entry_spot_price=101.0,
        oi_signal=None,
    )

    engine._evaluate_reverse_signal_exit(previous_spot=101.0, spot_price=94.0)

    assert engine.runtime.open_position is None
    assert engine.runtime.trade_history[-1].exit_reason == "reclaim_exit"


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


def test_gap_down_is_locked_when_entry_window_opens_and_requires_continuation() -> None:
    engine = build_engine()
    triggered: list[tuple[str, float]] = []
    entry_window_open = False

    engine.config.gap_open_continuation_points = 2.0
    engine._trigger_trade = lambda option_type, spot_price, *_: triggered.append((option_type, spot_price))  # type: ignore[method-assign]
    engine._is_trade_entry_window_open = lambda: entry_window_open  # type: ignore[method-assign]

    engine.handle_market_tick({"LTP": 89.0})
    assert triggered == []

    entry_window_open = True
    engine.handle_market_tick({"LTP": 88.0})
    assert triggered == []
    assert engine.runtime.opening_gap_put_locked is True

    engine.handle_market_tick({"LTP": 87.0})
    assert triggered == []

    engine.handle_market_tick({"LTP": 86.0})
    assert triggered == [("PUT", 86.0)]


def test_gap_up_lock_clears_after_spot_reclaims_trigger() -> None:
    engine = build_engine()
    triggered: list[tuple[str, float]] = []
    engine._trigger_trade = lambda option_type, spot_price, *_: triggered.append((option_type, spot_price))  # type: ignore[method-assign]

    engine._evaluate_breakout_from_state(102.0)
    assert triggered == []
    assert engine.runtime.opening_gap_call_locked is True

    engine.handle_market_tick({"LTP": 100.0})
    assert engine.runtime.opening_gap_call_locked is False

    engine.handle_market_tick({"LTP": 101.0})
    assert triggered == [("CALL", 101.0)]


def test_opening_gap_premium_requires_strong_follow_through() -> None:
    engine = build_engine()
    lock = OpeningGapLock(
        option_type="CALL",
        trigger_price=23850.65,
        baseline_spot_price=23967.60,
        baseline_option_strike=24000,
        baseline_option_price=98.00,
    )
    contract = OptionContract(
        option_type="CALL",
        strike=24000,
        security_id="72179",
        exchange_segment="NSE_FNO",
        expiry_date="2026-05-26",
        last_price=102.45,
        top_bid_price=102.30,
        top_ask_price=102.45,
    )

    assert engine.config.gap_open_option_premium_min_move_percent == 6.0
    assert engine._opening_gap_premium_confirms(lock, contract, 102.45) is False
    assert engine._opening_gap_premium_confirms(lock, contract, 103.89) is True


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


def test_profitable_trade_below_lock_can_retry_when_daily_cap_allows() -> None:
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

    assert triggered == [("CALL", 101.0)]


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
    assert engine.get_trade_ledger(status="ALL")["trades"] == []
    assert snapshot.events[-1].title == "Trade Reset Lock"


def test_closed_trade_is_written_to_persistent_ledger() -> None:
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
    engine.runtime.open_position = engine._build_position_state(
        contract=contract,
        fill_price=10.0,
        lots=1,
        quantity=65,
        trade_value=650.0,
        mode="paper",
        order_id="paper-ledger",
        entry_spot_price=101.0,
        oi_signal=None,
    )

    engine._mark_position_closed(
        order_id="paper-exit",
        exit_price=12.0,
        exit_reason="target",
    )

    ledger = engine.get_trade_ledger(status="CLOSED")

    assert ledger["summary"]["count"] == 1
    assert ledger["summary"]["realized_pnl"] == 130.0
    assert ledger["trades"][0]["trade_id"] == engine.runtime.trade_history[0].trade_id


def test_engine_bootstraps_runtime_history_from_persistent_ledger() -> None:
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
        order_id="paper-ledger",
        entry_spot_price=89.0,
        oi_signal=None,
    )
    trade.status = "CLOSED"
    trade.closed_at = engine._now()
    trade.current_price = 8.0
    trade.pnl = -130.0
    engine.ledger_store.upsert(trade)

    restarted = StrategyEngine(
        Settings(
            runtime_state_path=engine.settings.runtime_state_path,
            trade_ledger_path=engine.settings.trade_ledger_path,
            dhan_token_state_path=engine.settings.dhan_token_state_path,
            dhan_client_id="",
            dhan_access_token="",
        )
    )

    assert restarted.runtime.trade_history[0].trade_id == trade.trade_id
    assert restarted.get_trade_ledger(status="CLOSED")["summary"]["realized_pnl"] == -130.0


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


def test_opening_gap_oi_uses_fresh_delta_from_baseline_not_raw_bias() -> None:
    engine = build_engine()
    lock = OpeningGapLock(
        option_type="PUT",
        trigger_price=90.0,
        baseline_spot_price=88.0,
        baseline_oi_signal=OptionOiSignal(
            option_type="PUT",
            strike=90,
            ce_change_oi=2800000.0,
            pe_change_oi=-600000.0,
            confirmed=True,
            rule="baseline",
        ),
    )
    engine.opening_gap_locks["PUT"] = lock

    stale_bearish_snapshot = OptionOiSignal(
        option_type="PUT",
        strike=90,
        ce_change_oi=2800000.0,
        pe_change_oi=-650000.0,
        confirmed=True,
        rule="CE change OI > PE change OI",
    )
    fresh_bearish_build = OptionOiSignal(
        option_type="PUT",
        strike=90,
        ce_change_oi=3400000.0,
        pe_change_oi=-650000.0,
        confirmed=True,
        rule="CE change OI > PE change OI",
    )

    assert engine._opening_gap_oi_confirms(lock, stale_bearish_snapshot) is False
    assert engine._opening_gap_oi_confirms(lock, fresh_bearish_build) is True


def test_opening_gap_oi_does_not_compare_delta_across_different_strikes() -> None:
    engine = build_engine()
    lock = OpeningGapLock(
        option_type="PUT",
        trigger_price=90.0,
        baseline_spot_price=88.0,
        baseline_oi_signal=OptionOiSignal(
            option_type="PUT",
            strike=90,
            ce_change_oi=2800000.0,
            pe_change_oi=-600000.0,
            confirmed=True,
            rule="baseline",
        ),
    )
    current = OptionOiSignal(
        option_type="PUT",
        strike=85,
        ce_change_oi=1000.0,
        pe_change_oi=5000.0,
        confirmed=False,
        rule="CE change OI > PE change OI",
    )

    assert engine._opening_gap_oi_confirms(lock, current) is False
    assert engine._opening_gap_oi_signal(lock, current) is current


def test_opening_gap_trade_confirms_oi_at_live_atm_not_old_breakout_level() -> None:
    engine = build_engine()
    engine.reference_levels.previous_day_high = 23839.30
    engine.reference_levels.previous_day_low = 23610.30
    engine.reference_levels.expiry_date = "2026-05-19"

    class FakeGateway:
        def __init__(self) -> None:
            self.oi_calls: list[dict[str, object]] = []

        def fetch_option_chain(self, expiry_date: str) -> dict:
            assert expiry_date == "2026-05-19"
            return {"oc": {}}

        def evaluate_oi_confirmation(
            self,
            *,
            chain: dict,
            option_type: str,
            reference_price: float,
            strike_step: int,
            strike_basis: str = "breakout",
        ) -> OptionOiSignal:
            self.oi_calls.append(
                {
                    "option_type": option_type,
                    "reference_price": reference_price,
                    "strike_step": strike_step,
                    "strike_basis": strike_basis,
                }
            )
            return OptionOiSignal(
                option_type="PUT",
                strike=23400,
                ce_change_oi=3000000.0,
                pe_change_oi=-807950.0,
                confirmed=True,
                rule="post-gap fresh OI delta confirmed CE 658180.00, PE -11310.00",
                basis=strike_basis,
                reference_price=reference_price,
            )

        def resolve_contract_from_chain(
            self,
            *,
            chain: dict,
            spot_price: float,
            option_type: str,
            strike_step: int,
            expiry_date: str,
        ) -> OptionContract:
            assert spot_price == 23383.80
            return OptionContract(
                option_type="PUT",
                strike=23350,
                security_id="51347",
                exchange_segment="NSE_FNO",
                expiry_date=expiry_date,
                last_price=116.75,
                top_bid_price=116.50,
                top_ask_price=116.75,
            )

    fake_gateway = FakeGateway()
    engine.gateway = fake_gateway  # type: ignore[assignment]
    lock = OpeningGapLock(
        option_type="PUT",
        trigger_price=23610.30,
        baseline_spot_price=23399.50,
        baseline_oi_signal=OptionOiSignal(
            option_type="PUT",
            strike=23400,
            ce_change_oi=2341820.0,
            pe_change_oi=-796640.0,
            confirmed=True,
            rule="baseline",
        ),
        baseline_option_strike=23350,
        baseline_option_price=109.70,
    )
    engine.opening_gap_locks["PUT"] = lock

    engine._trigger_trade("PUT", 23383.80, lock)

    assert fake_gateway.oi_calls == [
        {
            "option_type": "PUT",
            "reference_price": 23383.80,
            "strike_step": 50,
            "strike_basis": "atm",
        }
    ]
    assert engine.runtime.open_position is not None
    assert engine.runtime.open_position.strike == 23350
    assert engine.runtime.open_position.entry_oi_strike == 23400
    assert engine.runtime.open_position.entry_oi_basis == "atm"
    assert engine.runtime.open_position.entry_oi_reference_price == 23383.80
    assert engine.runtime.open_position.entry_trigger_price == 23610.30


def test_normal_breakout_trade_confirms_oi_at_live_atm() -> None:
    engine = build_engine()
    engine.reference_levels.previous_day_high = 100.0
    engine.reference_levels.previous_day_low = 90.0
    engine.reference_levels.expiry_date = "2026-04-21"

    class FakeGateway:
        def __init__(self) -> None:
            self.oi_calls: list[dict[str, object]] = []

        def fetch_option_chain(self, expiry_date: str) -> dict:
            return {"oc": {}}

        def evaluate_oi_confirmation(
            self,
            *,
            chain: dict,
            option_type: str,
            reference_price: float,
            strike_step: int,
            strike_basis: str = "breakout",
        ) -> OptionOiSignal:
            self.oi_calls.append(
                {
                    "option_type": option_type,
                    "reference_price": reference_price,
                    "strike_basis": strike_basis,
                }
            )
            return OptionOiSignal(
                option_type="CALL",
                strike=100,
                ce_change_oi=1000000.0,
                pe_change_oi=1700000.0,
                confirmed=True,
                rule="PE change OI > CE change OI",
                basis=strike_basis,
                reference_price=reference_price,
            )

        def resolve_contract_from_chain(
            self,
            *,
            chain: dict,
            spot_price: float,
            option_type: str,
            strike_step: int,
            expiry_date: str,
        ) -> OptionContract:
            return OptionContract(
                option_type="CALL",
                strike=150,
                security_id="test-call",
                exchange_segment="NSE_FNO",
                expiry_date=expiry_date,
                last_price=10.0,
                top_bid_price=9.9,
                top_ask_price=10.0,
            )

    fake_gateway = FakeGateway()
    engine.gateway = fake_gateway  # type: ignore[assignment]

    engine._trigger_trade("CALL", 101.2)

    assert fake_gateway.oi_calls == [
        {
            "option_type": "CALL",
            "reference_price": 101.2,
            "strike_basis": "atm",
        }
    ]
    assert engine.runtime.open_position is not None
    assert engine.runtime.open_position.entry_oi_basis == "atm"
    assert engine.runtime.open_position.entry_oi_reference_price == 101.2


def test_trade_resolution_refreshes_stale_expiry_and_retries_option_chain() -> None:
    engine = build_engine()
    engine.reference_levels.expiry_date = "2026-04-21"

    class FakeGateway:
        def __init__(self) -> None:
            self.option_chain_calls: list[str] = []

        def fetch_expiry_list(self) -> list[str]:
            return ["2026-06-04"]

        def fetch_option_chain(self, expiry_date: str) -> dict:
            self.option_chain_calls.append(expiry_date)
            if expiry_date == "2026-04-21":
                raise DhanGatewayError("Dhan option chain request failed (811): Invalid Expiry Date")
            return {"oc": {}}

        def evaluate_oi_confirmation(
            self,
            *,
            chain: dict,
            option_type: str,
            reference_price: float,
            strike_step: int,
            strike_basis: str = "breakout",
        ) -> OptionOiSignal:
            return OptionOiSignal(
                option_type=option_type,
                strike=100,
                ce_change_oi=1000000.0,
                pe_change_oi=2000000.0,
                confirmed=True,
                rule="PE change OI > CE change OI",
                basis=strike_basis,
                reference_price=reference_price,
            )

        def resolve_contract_from_chain(self, **kwargs) -> OptionContract:
            return OptionContract(
                option_type=kwargs["option_type"],
                strike=150,
                security_id="test-call",
                exchange_segment="NSE_FNO",
                expiry_date=kwargs["expiry_date"],
                last_price=10.0,
                top_bid_price=9.9,
                top_ask_price=10.0,
            )

    fake_gateway = FakeGateway()
    engine.gateway = fake_gateway  # type: ignore[assignment]

    engine._trigger_trade("CALL", 101.0)

    assert fake_gateway.option_chain_calls == ["2026-04-21", "2026-06-04"]
    assert engine.reference_levels.expiry_date == "2026-06-04"
    assert engine.runtime.open_position is not None
    assert engine.runtime.open_position.expiry_date == "2026-06-04"
    assert any(event.title == "Expiry Refreshed" for event in engine.events)


def test_option_chain_oi_endpoint_refreshes_stale_expiry_and_retries() -> None:
    engine = build_engine()
    engine.reference_levels.expiry_date = "2026-04-21"

    class FakeGateway:
        def __init__(self) -> None:
            self.option_chain_calls: list[str] = []

        def fetch_expiry_list(self) -> list[str]:
            return ["2026-06-04"]

        def fetch_option_chain(self, expiry_date: str) -> dict:
            self.option_chain_calls.append(expiry_date)
            if expiry_date == "2026-04-21":
                raise DhanGatewayError("Dhan option chain request failed (811): Invalid Expiry Date")
            return {"oc": {"100.000000": {}}}

        def get_strike_oi_change(self, chain: dict, strike: int) -> dict[str, float | None]:
            return {
                "ce_change_oi": 1000.0,
                "pe_change_oi": 2000.0,
                "ce_last_price": 10.0,
                "pe_last_price": 11.0,
                "ce_oi": 10000.0,
                "pe_oi": 12000.0,
            }

    fake_gateway = FakeGateway()
    engine.gateway = fake_gateway  # type: ignore[assignment]

    changes = engine.get_option_chain_oi_changes([100])

    assert fake_gateway.option_chain_calls == ["2026-04-21", "2026-06-04"]
    assert engine.reference_levels.expiry_date == "2026-06-04"
    assert changes[0].expiry_date == "2026-06-04"
    assert changes[0].ce_change_oi == 1000.0
    assert changes[0].pe_change_oi == 2000.0


def test_legacy_gap_trade_migration_marks_old_breakout_oi_basis() -> None:
    migrated = migrate_trade_payload(
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
            "closed_at": "2026-05-18T04:02:53.600829Z",
        }
    )

    assert migrated["entry_oi_basis"] == "legacy_breakout"
    assert migrated["entry_oi_reference_price"] == 23610.3


def test_weak_oi_confirmation_waits_instead_of_trading_on_resistance_decrease() -> None:
    engine = build_engine()
    calls: list[tuple[str, float]] = []

    class FakeGateway:
        def fetch_option_chain(self, expiry_date: str) -> dict:
            return {"oc": {}}

        def evaluate_oi_confirmation(
            self,
            *,
            chain: dict,
            option_type: str,
            reference_price: float,
            strike_step: int,
            strike_basis: str = "breakout",
        ) -> OptionOiSignal:
            return OptionOiSignal(
                option_type="CALL",
                strike=100,
                ce_change_oi=49.0,
                pe_change_oi=45.0,
                confirmed=False,
                rule="PE change OI > CE change OI",
                basis=strike_basis,
                reference_price=reference_price,
            )

        def resolve_contract_from_chain(self, **kwargs) -> OptionContract:
            calls.append((kwargs["option_type"], kwargs["spot_price"]))
            return OptionContract(
                option_type="CALL",
                strike=150,
                security_id="test-call",
                exchange_segment="NSE_FNO",
                expiry_date=kwargs["expiry_date"],
                last_price=10.0,
                top_bid_price=9.9,
                top_ask_price=10.0,
            )

    engine.gateway = FakeGateway()  # type: ignore[assignment]

    engine._trigger_trade("CALL", 101.0)

    assert calls == []
    assert engine.runtime.open_position is None
    assert "CALL" in engine.pending_oi_breakouts


def test_minor_call_put_oi_gap_is_not_clean_confirmation() -> None:
    engine = build_engine()
    signal = OptionOiSignal(
        option_type="CALL",
        strike=24000,
        ce_change_oi=5837585.0,
        pe_change_oi=6001190.0,
        confirmed=True,
        rule="PE change OI > CE change OI",
        basis="atm",
        reference_price=23983.05,
    )

    filtered = engine._apply_oi_edge_filter(signal)

    assert filtered is not None
    assert filtered.confirmed is False
    assert "edge 163605.00 (2.73%)" in filtered.rule


def test_token_renewal_reports_auth_failure_without_renew_attempt() -> None:
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
        raise AssertionError("auth failures cannot be recovered by RenewToken")

    engine.gateway.fetch_token_valid_until = fail_token_check  # type: ignore[method-assign]
    engine.gateway.renew_access_token = renew_token  # type: ignore[method-assign]
    engine._restart_dhan_streams = lambda: None  # type: ignore[method-assign]

    engine._check_and_renew_token(force=False)

    assert calls == ["check"]
    assert engine.connections.token_renewal_status == "error"
    assert engine.connections.api_ready is False
    assert "replace DHAN_ACCESS_TOKEN in Railway" in (engine.connections.last_error or "")


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

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from datetime import date, datetime, time as dt_time, timedelta, timezone
from zoneinfo import ZoneInfo
from uuid import uuid4

from app.core.config import Settings
from app.models.schemas import (
    ActionResponse,
    ActivityEvent,
    ConfigUpdateRequest,
    ConnectionState,
    PositionState,
    ReferenceLevels,
    SelectedInstrument,
    OptionChainOiChange,
    StrategyConfig,
    StrategyRuntime,
    StrategySnapshot,
)
from app.services.dhan_gateway import DhanGateway, DhanGatewayError, OptionContract, OptionOiSignal
from app.services.persistence import DhanTokenStore, RuntimeStateStore


class StrategyEngine:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.gateway = DhanGateway(settings)
        self.store = RuntimeStateStore(settings.runtime_state_path)
        self.token_store = DhanTokenStore(settings.dhan_token_state_path)
        self.lock = threading.RLock()
        self.notifier: Callable[[StrategySnapshot], None] | None = None

        saved_config, saved_events, saved_trades = self.store.load()
        self.config = self._normalize_config(saved_config or StrategyConfig(
            enabled=settings.default_strategy_enabled,
            paper_trading=settings.default_paper_trading,
            capital_sizing_enabled=settings.default_capital_sizing_enabled,
            account_capital=settings.default_account_capital,
            trade_capital=settings.default_trade_capital,
            lots=settings.default_lots,
            lot_size=settings.default_lot_size,
            product_type=settings.default_product_type,
            strike_step=settings.strike_step,
            oi_confirmation_enabled=settings.default_oi_confirmation_enabled,
            max_trades_per_day=settings.default_max_trades_per_day,
            cooldown_seconds=settings.default_cooldown_seconds,
            breakout_buffer=settings.default_breakout_buffer,
            no_trade_before_time=settings.default_no_trade_before_time,
            stop_loss_percent=settings.default_stop_loss_percent,
            target_percent=settings.default_target_percent,
            trailing_stop_enabled=settings.default_trailing_stop_enabled,
            trailing_activation_percent=settings.default_trailing_activation_percent,
            trailing_distance_percent=settings.default_trailing_distance_percent,
            auto_close_enabled=settings.default_auto_close_enabled,
            auto_close_time=settings.default_auto_close_time,
            reverse_signal_exit_enabled=settings.default_reverse_signal_exit_enabled,
        ))
        saved_token = self.token_store.load()
        saved_token_expiry = self._parse_saved_token_datetime(saved_token, "token_valid_until")
        saved_token_renewed_at = self._parse_saved_token_datetime(saved_token, "renewed_at")
        if saved_token and saved_token.get("access_token"):
            self.gateway.update_access_token(str(saved_token["access_token"]))

        self.connections = ConnectionState(
            configured=self.gateway.is_configured,
            token_auto_renew_enabled=settings.dhan_auto_renew_enabled,
            token_valid_until=saved_token_expiry,
            token_last_renewed_at=saved_token_renewed_at,
            token_renewal_status="idle",
        )
        self.reference_levels = ReferenceLevels()
        session_date = self._today_session_date()
        self.runtime = StrategyRuntime(
            session_date=session_date,
            trade_history=saved_trades[-100:],
        )
        self._hydrate_session_state_from_history(session_date)
        self.events = saved_events[-150:]

        self.market_feed_thread: threading.Thread | None = None
        self.market_feed_stop = threading.Event()
        self.order_updates_thread: threading.Thread | None = None
        self.order_updates_stop = threading.Event()
        self.position_poller_thread: threading.Thread | None = None
        self.position_poller_stop = threading.Event()
        self.token_renewal_thread: threading.Thread | None = None
        self.token_renewal_stop = threading.Event()
        self.pending_oi_breakouts: dict[str, OptionOiSignal] = {}

    def set_notifier(self, notifier: Callable[[StrategySnapshot], None]) -> None:
        self.notifier = notifier

    def startup(self) -> None:
        with self.lock:
            if self.settings.default_strategy_enabled and not self.config.enabled:
                self.config.enabled = True
                self._log("info", "Strategy Started", "Strategy armed automatically on startup.")
        self._log("info", "Engine Ready", "Strategy engine initialised.")
        if not self.gateway.is_configured:
            self.connections.last_error = "Dhan credentials missing in backend/.env."
            self._emit()
            return

        self.connections.api_ready = True
        self._start_token_renewal()
        self.refresh_reference_levels()
        self._start_market_feed()
        self._start_order_updates()
        self._start_position_poller()

    def shutdown(self) -> None:
        self.token_renewal_stop.set()
        self.market_feed_stop.set()
        self.order_updates_stop.set()
        self.position_poller_stop.set()
        self.store.save(self.config, self.events, self.runtime.trade_history)

    def get_snapshot(self) -> StrategySnapshot:
        with self.lock:
            self.runtime.market_session_open = self._is_market_session_open()
            self.runtime.next_trade_window_starts_at = self._next_trade_window_starts_at()
            return StrategySnapshot(
                app_name=self.settings.app_name,
                generated_at=self._now(),
                config=self.config.model_copy(deep=True),
                connections=self.connections.model_copy(deep=True),
                reference_levels=self.reference_levels.model_copy(deep=True),
                runtime=self.runtime.model_copy(deep=True),
                events=[event.model_copy(deep=True) for event in self.events[-100:]],
            )

    def get_option_chain_oi_change(self, strike: int) -> OptionChainOiChange:
        return self.get_option_chain_oi_changes([strike])[0]

    def get_option_chain_oi_changes(self, strikes: list[int]) -> list[OptionChainOiChange]:
        with self.lock:
            expiry_date = self.reference_levels.expiry_date
        if not expiry_date:
            expiry_date = self.gateway.fetch_expiry_list()[0]

        chain = self.gateway.fetch_option_chain(expiry_date)
        updated_at = self._now()
        changes: list[OptionChainOiChange] = []
        for strike in strikes:
            oi = self.gateway.get_strike_oi_change(chain, strike)
            changes.append(
                OptionChainOiChange(
                    strike=strike,
                    expiry_date=expiry_date,
                    ce_change_oi=float(oi["ce_change_oi"] or 0),
                    pe_change_oi=float(oi["pe_change_oi"] or 0),
                    ce_last_price=oi["ce_last_price"],
                    pe_last_price=oi["pe_last_price"],
                    ce_oi=oi["ce_oi"],
                    pe_oi=oi["pe_oi"],
                    updated_at=updated_at,
                )
            )
        return changes

    def update_config(self, payload: ConfigUpdateRequest) -> StrategySnapshot:
        with self.lock:
            updated = self.config.model_dump()
            for field, value in payload.model_dump(exclude_none=True).items():
                updated[field] = value
            self.config = self._normalize_config(StrategyConfig.model_validate(updated))
            self._log("info", "Config Updated", "Strategy settings saved.", updated)
            self._persist_and_emit()
            return self.get_snapshot()

    def start_strategy(self) -> StrategySnapshot:
        with self.lock:
            self.config.enabled = True
            self._log("info", "Strategy Started", "Breakout monitoring is armed.")
            self._persist_and_emit()
            spot_price = self.runtime.spot_price

        if spot_price is not None:
            self._evaluate_breakout_from_state(spot_price)
        return self.get_snapshot()

    def stop_strategy(self) -> StrategySnapshot:
        with self.lock:
            self.config.enabled = False
            self._log("warn", "Strategy Stopped", "Breakout monitoring is paused.")
            self._persist_and_emit()
            return self.get_snapshot()

    def refresh_reference_levels(self) -> StrategySnapshot:
        try:
            previous_high, previous_low, candle_date = self.gateway.fetch_previous_day_levels()
            expiries = self.gateway.fetch_expiry_list()
        except DhanGatewayError as exc:
            with self.lock:
                self.connections.last_error = str(exc)
                self._log("error", "Refresh Failed", str(exc))
                self._persist_and_emit()
                return self.get_snapshot()

        with self.lock:
            session_date = self._today_session_date()
            is_new_session = self.runtime.session_date != session_date
            self.reference_levels.previous_day_high = previous_high
            self.reference_levels.previous_day_low = previous_low
            self.reference_levels.source_date = candle_date
            self.reference_levels.expiry_date = expiries[0]
            self.reference_levels.updated_at = self._now()
            self.connections.last_error = None
            if is_new_session:
                self.runtime.previous_high_broken = False
                self.runtime.previous_low_broken = False
                self.runtime.trades_today = 0
                self.runtime.last_signal = None
                self.runtime.last_signal_at = None
                self.runtime.session_date = session_date
            else:
                self._hydrate_session_state_from_history(session_date)
            self._log(
                "info",
                "Reference Levels Updated",
                f"Loaded previous day high {previous_high:.2f} and low {previous_low:.2f}.",
                {"expiry_date": expiries[0], "source_date": candle_date},
            )
            self._persist_and_emit()
            spot_price = self.runtime.spot_price
            enabled = self.config.enabled

        if spot_price is not None:
            self._rearm_breakout_flags(spot_price)
        if enabled and spot_price is not None:
            self._evaluate_breakout_from_state(spot_price)
        return self.get_snapshot()

    def close_position(self) -> StrategySnapshot:
        self._request_position_close(
            reason="manual",
            message="Manual close requested by operator.",
        )
        return self.get_snapshot()

    def renew_token_now(self) -> StrategySnapshot:
        self._check_and_renew_token(force=True)
        return self.get_snapshot()

    def handle_market_tick(self, packet: dict[str, object]) -> None:
        ltp = self._extract_spot_ltp(packet)
        if ltp is None:
            return

        with self.lock:
            previous_spot = self.runtime.spot_price
            has_open_position = self.runtime.open_position is not None and self.runtime.open_position.status == "OPEN"

        with self.lock:
            self.runtime.previous_spot_price = previous_spot
            self.runtime.spot_price = ltp
            self.runtime.spot_updated_at = self._now()

        self._emit()
        self._rearm_breakout_flags(ltp)
        if previous_spot is None:
            self._evaluate_breakout_from_state(ltp)
            return
        if has_open_position:
            self._evaluate_reverse_signal_exit(previous_spot, ltp)
            return
        self._evaluate_breakout(previous_spot, ltp)

    def handle_order_update(self, message: dict[str, object]) -> None:
        with self.lock:
            self._log("info", "Order Update", "Received order update from Dhan.", {"payload": message})
            self._persist_and_emit()

    def set_market_feed_status(self, connected: bool, error: str | None = None) -> None:
        with self.lock:
            self.connections.market_feed_connected = connected
            if error:
                self.connections.last_error = error
                self._mark_token_error_if_auth_related(error)
                self._log("error", "Market Feed", error)
            self._persist_and_emit()

    def set_order_updates_status(self, connected: bool, error: str | None = None) -> None:
        with self.lock:
            self.connections.order_updates_connected = connected
            if error:
                self.connections.last_error = error
                self._mark_token_error_if_auth_related(error)
                self._log("error", "Order Updates", error)
            self._persist_and_emit()

    def _evaluate_breakout(self, previous_spot: float, spot_price: float) -> None:
        with self.lock:
            reference_high = self.reference_levels.previous_day_high
            reference_low = self.reference_levels.previous_day_low
            is_enabled = self.config.enabled
            has_open_position = self.runtime.open_position is not None and self.runtime.open_position.status == "OPEN"
            trades_today = self.runtime.trades_today
            last_signal_at = self.runtime.last_signal_at
            cooldown_seconds = self.config.cooldown_seconds
            breakout_buffer = self.config.breakout_buffer
            max_trades_per_day = self.config.max_trades_per_day
            previous_high_broken = self.runtime.previous_high_broken
            previous_low_broken = self.runtime.previous_low_broken
            has_pending_call_oi = "CALL" in self.pending_oi_breakouts
            has_pending_put_oi = "PUT" in self.pending_oi_breakouts

        if not is_enabled or reference_high is None or reference_low is None or has_open_position:
            return
        if not self._is_trade_entry_window_open():
            return
        if trades_today >= max_trades_per_day:
            return
        if last_signal_at and (self._now() - last_signal_at).total_seconds() < cooldown_seconds:
            return

        high_trigger = reference_high + breakout_buffer
        low_trigger = reference_low - breakout_buffer

        if previous_spot <= high_trigger < spot_price and not previous_high_broken:
            self._trigger_trade("CALL", spot_price)
            return
        if spot_price > high_trigger and not previous_high_broken and has_pending_call_oi:
            self._trigger_trade("CALL", spot_price)
            return

        if previous_spot >= low_trigger > spot_price and not previous_low_broken:
            self._trigger_trade("PUT", spot_price)
            return
        if spot_price < low_trigger and not previous_low_broken and has_pending_put_oi:
            self._trigger_trade("PUT", spot_price)

    def _rearm_breakout_flags(self, spot_price: float) -> None:
        with self.lock:
            reference_high = self.reference_levels.previous_day_high
            reference_low = self.reference_levels.previous_day_low
            breakout_buffer = self.config.breakout_buffer

            if reference_high is None or reference_low is None:
                return

            high_trigger = reference_high + breakout_buffer
            low_trigger = reference_low - breakout_buffer
            changed = False

            if self.runtime.previous_high_broken and spot_price <= high_trigger:
                self.runtime.previous_high_broken = False
                self.pending_oi_breakouts.pop("CALL", None)
                changed = True

            if self.runtime.previous_low_broken and spot_price >= low_trigger:
                self.runtime.previous_low_broken = False
                self.pending_oi_breakouts.pop("PUT", None)
                changed = True

            if changed:
                self._persist_and_emit()

    def _evaluate_breakout_from_state(self, spot_price: float) -> None:
        with self.lock:
            reference_high = self.reference_levels.previous_day_high
            reference_low = self.reference_levels.previous_day_low
            is_enabled = self.config.enabled
            has_open_position = self.runtime.open_position is not None and self.runtime.open_position.status == "OPEN"
            trades_today = self.runtime.trades_today
            breakout_buffer = self.config.breakout_buffer
            max_trades_per_day = self.config.max_trades_per_day
            previous_high_broken = self.runtime.previous_high_broken
            previous_low_broken = self.runtime.previous_low_broken

        if not is_enabled or reference_high is None or reference_low is None or has_open_position:
            return
        if not self._is_trade_entry_window_open():
            return
        if trades_today >= max_trades_per_day:
            return

        high_trigger = reference_high + breakout_buffer
        low_trigger = reference_low - breakout_buffer

        if spot_price > high_trigger and not previous_high_broken:
            self._trigger_trade("CALL", spot_price)
            return

        if spot_price < low_trigger and not previous_low_broken:
            self._trigger_trade("PUT", spot_price)

    def _trigger_trade(self, option_type: str, spot_price: float) -> None:
        with self.lock:
            expiry_date = self.reference_levels.expiry_date
            reference_high = self.reference_levels.previous_day_high
            reference_low = self.reference_levels.previous_day_low
            breakout_buffer = self.config.breakout_buffer
            oi_confirmation_enabled = self.config.oi_confirmation_enabled
            strike_step = self.config.strike_step
            if not expiry_date:
                self._log("warn", "Trade Skipped", "Expiry date is not loaded yet.")
                self._persist_and_emit()
                return

        trigger_price = (
            (reference_high + breakout_buffer)
            if option_type == "CALL" and reference_high is not None
            else (reference_low - breakout_buffer if reference_low is not None else None)
        )
        if trigger_price is None:
            with self.lock:
                self._log("warn", "Trade Skipped", "Breakout trigger price is not available.")
                self._persist_and_emit()
            return

        try:
            chain = self.gateway.fetch_option_chain(expiry_date)
            oi_signal = self.gateway.evaluate_oi_confirmation(
                chain=chain,
                option_type=option_type,
                trigger_price=trigger_price,
                strike_step=strike_step,
            ) if oi_confirmation_enabled else None
            if oi_signal and not oi_signal.confirmed:
                weakening_signal = self._weakening_oi_confirmation(oi_signal)
                if weakening_signal:
                    oi_signal = weakening_signal
                else:
                    with self.lock:
                        self.pending_oi_breakouts[option_type] = oi_signal
                        self._log(
                            "warn",
                            "OI Confirmation Waiting",
                            (
                                f"Breakout detected but OI is not clean at strike {oi_signal.strike}: "
                                f"CE change OI {oi_signal.ce_change_oi:.2f}, "
                                f"PE change OI {oi_signal.pe_change_oi:.2f}. "
                                f"Waiting for blocking OI to decrease or for {oi_signal.rule}."
                            ),
                            {
                                "option_type": option_type,
                                "spot_price": spot_price,
                                "trigger_price": trigger_price,
                                "oi_strike": oi_signal.strike,
                                "ce_change_oi": oi_signal.ce_change_oi,
                                "pe_change_oi": oi_signal.pe_change_oi,
                                "required_rule": oi_signal.rule,
                            },
                        )
                        self._persist_and_emit()
                    return
            if oi_signal:
                with self.lock:
                    self.pending_oi_breakouts.pop(option_type, None)
            contract = self.gateway.resolve_contract_from_chain(
                chain=chain,
                spot_price=spot_price,
                option_type=option_type,
                strike_step=strike_step,
                expiry_date=expiry_date,
            )
        except DhanGatewayError as exc:
            with self.lock:
                self.connections.last_error = str(exc)
                self._log("error", "Trade Resolution Failed", str(exc))
                self._persist_and_emit()
                return

        fill_price = self._entry_fill_price(contract)
        sizing = self._calculate_trade_size(fill_price)
        if sizing is None:
            return
        lots, quantity, trade_value = sizing

        if self.config.paper_trading:
            self._open_paper_position(contract, fill_price, lots, quantity, trade_value, spot_price, oi_signal)
            return

        try:
            response = self.gateway.place_entry_order(
                contract=contract,
                quantity=quantity,
                product_type=self.config.product_type,
                tag=self.gateway.create_tag("entry"),
            )
        except DhanGatewayError as exc:
            with self.lock:
                self.connections.last_error = str(exc)
                self._log("error", "Entry Failed", str(exc))
                self._persist_and_emit()
                return

        with self.lock:
            self.runtime.selected_instrument = self._to_selected_instrument(contract)
            self.runtime.open_position = self._build_position_state(
                contract=contract,
                fill_price=fill_price,
                lots=lots,
                quantity=quantity,
                trade_value=trade_value,
                mode="live",
                order_id=str((response.get("data") or {}).get("orderId") or ""),
                entry_spot_price=spot_price,
                oi_signal=oi_signal,
            )
            self.runtime.trades_today += 1
            self.runtime.last_signal = contract.option_type
            self.runtime.last_signal_at = self._now()
            self.runtime.exit_in_progress = False
            if contract.option_type == "CALL":
                self.runtime.previous_high_broken = True
            else:
                self.runtime.previous_low_broken = True
            self._log(
                "trade",
                "Live Entry Submitted",
                f"Submitted {contract.strike} {contract.option_type} order.",
                {
                    "response": response,
                    "entry_price": fill_price,
                    "initial_pnl": 0.0,
                    "lots": self.runtime.open_position.lots if self.runtime.open_position else None,
                    "lot_size": self.runtime.open_position.lot_size if self.runtime.open_position else None,
                    "quantity": self.runtime.open_position.quantity if self.runtime.open_position else None,
                    "trade_value": self.runtime.open_position.trade_value if self.runtime.open_position else None,
                    "entry_reason": self.runtime.open_position.entry_reason if self.runtime.open_position else None,
                    "entry_spot_price": self.runtime.open_position.entry_spot_price if self.runtime.open_position else None,
                    "entry_trigger_price": self.runtime.open_position.entry_trigger_price if self.runtime.open_position else None,
                    "entry_oi_strike": self.runtime.open_position.entry_oi_strike if self.runtime.open_position else None,
                    "entry_ce_change_oi": self.runtime.open_position.entry_ce_change_oi if self.runtime.open_position else None,
                    "entry_pe_change_oi": self.runtime.open_position.entry_pe_change_oi if self.runtime.open_position else None,
                    "entry_oi_rule": self.runtime.open_position.entry_oi_rule if self.runtime.open_position else None,
                    "stop_loss_price": self.runtime.open_position.stop_loss_price if self.runtime.open_position else None,
                    "target_price": self.runtime.open_position.target_price if self.runtime.open_position else None,
                },
            )
            self._persist_and_emit()

    def _open_paper_position(
        self,
        contract: OptionContract,
        fill_price: float,
        lots: int,
        quantity: int,
        trade_value: float,
        spot_price: float,
        oi_signal: OptionOiSignal | None,
    ) -> None:
        with self.lock:
            self.runtime.selected_instrument = self._to_selected_instrument(contract)
            self.runtime.open_position = self._build_position_state(
                contract=contract,
                fill_price=fill_price,
                lots=lots,
                quantity=quantity,
                trade_value=trade_value,
                mode="paper",
                order_id=f"paper-{uuid4().hex[:10]}",
                entry_spot_price=spot_price,
                oi_signal=oi_signal,
            )
            self.runtime.trades_today += 1
            self.runtime.last_signal = contract.option_type
            self.runtime.last_signal_at = self._now()
            self.runtime.exit_in_progress = False
            if contract.option_type == "CALL":
                self.runtime.previous_high_broken = True
            else:
                self.runtime.previous_low_broken = True
            self._log(
                "trade",
                "Paper Trade Opened",
                f"Bought {contract.strike} {contract.option_type} in paper mode.",
                {
                    "entry_price": fill_price,
                    "security_id": contract.security_id,
                    "initial_pnl": 0.0,
                    "lots": self.runtime.open_position.lots if self.runtime.open_position else None,
                    "lot_size": self.runtime.open_position.lot_size if self.runtime.open_position else None,
                    "quantity": self.runtime.open_position.quantity if self.runtime.open_position else None,
                    "trade_value": self.runtime.open_position.trade_value if self.runtime.open_position else None,
                    "entry_reason": self.runtime.open_position.entry_reason if self.runtime.open_position else None,
                    "entry_spot_price": self.runtime.open_position.entry_spot_price if self.runtime.open_position else None,
                    "entry_trigger_price": self.runtime.open_position.entry_trigger_price if self.runtime.open_position else None,
                    "entry_oi_strike": self.runtime.open_position.entry_oi_strike if self.runtime.open_position else None,
                    "entry_ce_change_oi": self.runtime.open_position.entry_ce_change_oi if self.runtime.open_position else None,
                    "entry_pe_change_oi": self.runtime.open_position.entry_pe_change_oi if self.runtime.open_position else None,
                    "entry_oi_rule": self.runtime.open_position.entry_oi_rule if self.runtime.open_position else None,
                    "stop_loss_price": self.runtime.open_position.stop_loss_price if self.runtime.open_position else None,
                    "target_price": self.runtime.open_position.target_price if self.runtime.open_position else None,
                },
            )
            self._persist_and_emit()

    def _mark_position_closed(
        self,
        order_id: str | None,
        exit_price: float,
        exit_reason: str,
        exit_reason_detail: str | None = None,
        exit_trigger_price: float | None = None,
    ) -> None:
        position = self.runtime.open_position
        if not position:
            return
        position.current_price = exit_price
        position.pnl = (exit_price - position.entry_price) * position.quantity
        position.status = "CLOSED"
        position.closed_at = self._now()
        position.exit_order_id = order_id
        position.exit_reason = exit_reason
        position.exit_reason_detail = exit_reason_detail
        position.exit_trigger_price = exit_trigger_price
        position.exit_requested = False
        self.runtime.trade_history.append(position.model_copy(deep=True))
        self.runtime.trade_history = self.runtime.trade_history[-100:]
        self.runtime.open_position = None
        self.runtime.selected_instrument = None
        self.runtime.exit_in_progress = False

    def _start_market_feed(self) -> None:
        if self.market_feed_thread and self.market_feed_thread.is_alive():
            return
        self.market_feed_thread = threading.Thread(
            target=self.gateway.stream_market_feed,
            args=(self.market_feed_stop, self.handle_market_tick, self.set_market_feed_status),
            daemon=True,
            name="market-feed",
        )
        self.market_feed_thread.start()

    def _start_order_updates(self) -> None:
        if self.order_updates_thread and self.order_updates_thread.is_alive():
            return
        self.order_updates_thread = threading.Thread(
            target=self.gateway.stream_order_updates,
            args=(self.order_updates_stop, self.handle_order_update, self.set_order_updates_status),
            daemon=True,
            name="order-updates",
        )
        self.order_updates_thread.start()

    def _start_position_poller(self) -> None:
        if self.position_poller_thread and self.position_poller_thread.is_alive():
            return

        def worker() -> None:
            while not self.position_poller_stop.is_set():
                time.sleep(max(self.settings.option_quote_poll_seconds, 5.0))
                with self.lock:
                    position = self.runtime.open_position
                    current_session = self.runtime.session_date
                if current_session != date.today().isoformat():
                    self.refresh_reference_levels()
                    continue
                if not position or position.status != "OPEN" or not self.gateway.is_configured:
                    continue
                if self._is_auto_close_due():
                    self._request_position_close(
                        reason="auto_close",
                        message=f"Auto-close time {self.config.auto_close_time} reached.",
                    )
                    continue
                try:
                    latest = self.gateway.fetch_security_ltp("NSE_FNO", position.security_id)
                except DhanGatewayError as exc:
                    with self.lock:
                        self.connections.last_error = str(exc)
                        self._log("error", "Quote Poll Failed", str(exc))
                        self._persist_and_emit()
                    continue
                if latest is None:
                    continue
                with self.lock:
                    if not self.runtime.open_position:
                        continue
                    position = self.runtime.open_position
                    position.current_price = latest
                    position.pnl = (latest - position.entry_price) * position.quantity

                    trailing_armed_before = position.trailing_armed
                    if latest > position.highest_price_seen:
                        position.highest_price_seen = latest
                    if self.config.trailing_stop_enabled:
                        activation_price = position.entry_price * (
                            1 + self.config.trailing_activation_percent / 100
                        )
                        if position.highest_price_seen >= activation_price:
                            position.trailing_armed = True
                            position.trailing_stop_price = position.highest_price_seen * (
                                1 - self.config.trailing_distance_percent / 100
                            )
                    if position.trailing_armed and position.trailing_stop_price is not None:
                        position.trailing_stop_price = max(
                            position.trailing_stop_price,
                            position.highest_price_seen * (1 - self.config.trailing_distance_percent / 100),
                        )

                    should_log_trailing_arm = position.trailing_armed and not trailing_armed_before
                    stop_loss_price = position.stop_loss_price
                    target_price = position.target_price
                    trailing_stop_price = position.trailing_stop_price
                    exit_in_progress = self.runtime.exit_in_progress
                    self._emit()

                if should_log_trailing_arm:
                    with self.lock:
                        self._log(
                            "info",
                            "Trailing Stop Armed",
                            f"Trailing stop armed at {trailing_stop_price:.2f}.",
                        )
                        self._persist_and_emit()

                if exit_in_progress:
                    continue
                if latest <= stop_loss_price:
                    self._request_position_close(
                        reason="stop_loss",
                        message=f"Stop-loss hit at option price {latest:.2f}.",
                        triggered_price=latest,
                    )
                    continue
                if latest >= target_price:
                    self._request_position_close(
                        reason="target",
                        message=f"Target hit at option price {latest:.2f}.",
                        triggered_price=latest,
                    )
                    continue
                if trailing_stop_price is not None and latest <= trailing_stop_price:
                    self._request_position_close(
                        reason="trailing_stop",
                        message=f"Trailing stop hit at option price {latest:.2f}.",
                        triggered_price=latest,
                    )

        self.position_poller_thread = threading.Thread(target=worker, daemon=True, name="position-poller")
        self.position_poller_thread.start()

    def _start_token_renewal(self) -> None:
        if not self.settings.dhan_auto_renew_enabled:
            with self.lock:
                self.connections.token_auto_renew_enabled = False
                self.connections.token_renewal_status = "idle"
            return
        if self.token_renewal_thread and self.token_renewal_thread.is_alive():
            return

        def worker() -> None:
            self._check_and_renew_token(force=False)
            while not self.token_renewal_stop.wait(max(self.settings.dhan_token_check_seconds, 60.0)):
                self._check_and_renew_token(force=False)

        self.token_renewal_stop.clear()
        self.token_renewal_thread = threading.Thread(target=worker, daemon=True, name="token-renewal")
        self.token_renewal_thread.start()

    def _check_and_renew_token(self, *, force: bool) -> None:
        if not self.gateway.is_configured:
            with self.lock:
                self.connections.configured = False
                self.connections.api_ready = False
                self.connections.token_renewal_status = "error"
                self.connections.last_error = "Dhan credentials missing in backend/.env."
                self._persist_and_emit()
            return

        now = self._now()
        token_check_error: str | None = None
        try:
            token_valid_until = self.gateway.fetch_token_valid_until()
        except DhanGatewayError as exc:
            token_valid_until = None
            token_check_error = str(exc)

        should_renew = force or token_check_error is not None
        if token_valid_until:
            renew_at = token_valid_until - timedelta(minutes=max(self.settings.dhan_token_renew_buffer_minutes, 5.0))
            should_renew = should_renew or now >= renew_at
        elif not should_renew:
            last_renewed_at = self.connections.token_last_renewed_at
            should_renew = last_renewed_at is None or now - last_renewed_at >= timedelta(hours=12)

        with self.lock:
            self.connections.configured = True
            self.connections.api_ready = True
            self.connections.token_auto_renew_enabled = self.settings.dhan_auto_renew_enabled
            self.connections.token_last_checked_at = now
            self.connections.token_valid_until = token_valid_until
            if token_check_error:
                self.connections.token_renewal_status = "renewing"
                self.connections.last_error = f"Token check failed; attempting renewal. {token_check_error}"
                self._log("warn", "Token Check Failed", self.connections.last_error)
                self._persist_and_emit()
            elif token_valid_until is None and should_renew:
                self.connections.token_renewal_status = "renewing"
                self.connections.last_error = "Dhan did not return token validity; attempting renewal as a safety measure."
                self._log("warn", "Token Validity Unknown", self.connections.last_error)
                self._persist_and_emit()
            elif token_valid_until is None:
                self.connections.token_renewal_status = "valid"
                self._persist_and_emit()
                return
            if token_valid_until and token_valid_until <= now:
                self.connections.token_renewal_status = "expired"
                self.connections.last_error = "Dhan token is expired. It cannot be renewed automatically after expiry."
                self._log("error", "Token Expired", self.connections.last_error)
                self._persist_and_emit()
                return
            self.connections.token_renewal_status = "renewing" if should_renew else "valid"
            self._persist_and_emit()

        if not should_renew:
            return

        try:
            new_token, renewed_until, _ = self.gateway.renew_access_token()
            if renewed_until is None:
                renewed_until = self.gateway.fetch_token_valid_until()
            renewed_at = self._now()
            self.token_store.save(
                access_token=new_token,
                token_valid_until=renewed_until,
                renewed_at=renewed_at,
            )
        except DhanGatewayError as exc:
            error_message = str(exc)
            if token_check_error:
                error_message = f"{error_message}. Token check failed before renewal: {token_check_error}"
            with self.lock:
                self.connections.token_renewal_status = "error"
                self.connections.last_error = error_message
                self._mark_token_error_if_auth_related(error_message)
                self._log("error", "Token Renewal Failed", error_message)
                self._persist_and_emit()
            return

        with self.lock:
            self.connections.configured = True
            self.connections.api_ready = True
            self.connections.token_valid_until = renewed_until
            self.connections.token_last_renewed_at = renewed_at
            self.connections.token_last_checked_at = renewed_at
            self.connections.token_renewal_status = "renewed"
            self.connections.last_error = None
            expiry_text = renewed_until.astimezone(ZoneInfo(self.settings.app_timezone)).strftime("%d %b %Y, %I:%M %p") if renewed_until else "unknown"
            self._log(
                "info",
                "Dhan Token Renewed",
                f"Access token renewed automatically. Valid until {expiry_text}.",
            )
            self._persist_and_emit()

        self._restart_dhan_streams()

    def _restart_dhan_streams(self) -> None:
        self.market_feed_stop.set()
        self.order_updates_stop.set()
        if self.market_feed_thread and self.market_feed_thread.is_alive():
            self.market_feed_thread.join(timeout=2.0)
        if self.order_updates_thread and self.order_updates_thread.is_alive():
            self.order_updates_thread.join(timeout=2.0)

        self.market_feed_stop = threading.Event()
        self.order_updates_stop = threading.Event()
        with self.lock:
            self.connections.market_feed_connected = False
            self.connections.order_updates_connected = False
            self._emit()
        self._start_market_feed()
        self._start_order_updates()

    def _mark_token_error_if_auth_related(self, error: str) -> None:
        normalized = error.lower()
        auth_markers = ("401", "expired", "unauthorized", "invalid token", "access token", "dh-901")
        if any(marker in normalized for marker in auth_markers):
            self.connections.token_renewal_status = "expired" if "expired" in normalized else "error"

    @staticmethod
    def _parse_saved_token_datetime(saved_token: dict | None, key: str) -> datetime | None:
        if not saved_token:
            return None
        raw_value = saved_token.get(key)
        if not isinstance(raw_value, str) or not raw_value:
            return None
        try:
            parsed = datetime.fromisoformat(raw_value)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    def _to_selected_instrument(self, contract: OptionContract) -> SelectedInstrument:
        return SelectedInstrument(
            option_type=contract.option_type,
            strike=contract.strike,
            security_id=contract.security_id,
            exchange_segment=contract.exchange_segment,
            expiry_date=contract.expiry_date,
            last_price=contract.last_price,
            top_bid_price=contract.top_bid_price,
            top_ask_price=contract.top_ask_price,
        )

    def _persist_and_emit(self) -> None:
        self.store.save(self.config, self.events, self.runtime.trade_history)
        self._emit()

    def _emit(self) -> None:
        if self.notifier:
            self.notifier(self.get_snapshot())

    def _log(self, level: str, title: str, message: str, details: dict[str, object] | None = None) -> None:
        self.events.append(
            ActivityEvent(
                id=uuid4().hex,
                timestamp=self._now(),
                level=level,  # type: ignore[arg-type]
                title=title,
                message=message,
                details=details or {},
            )
        )
        self.events = self.events[-150:]

    @staticmethod
    def _extract_spot_ltp(packet: dict[str, object]) -> float | None:
        value = packet.get("LTP")
        try:
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _extract_quote_price(quote: dict[str, object]) -> float | None:
        candidate_keys = ("last_price", "LTP", "top_ask_price", "top_bid_price")
        for key in candidate_keys:
            value = quote.get(key)
            try:
                return float(value) if value is not None else None
            except (TypeError, ValueError):
                continue
        return None

    @staticmethod
    def _entry_fill_price(contract: OptionContract) -> float:
        return contract.top_ask_price or contract.last_price or 0.0

    def _weakening_oi_confirmation(self, current: OptionOiSignal) -> OptionOiSignal | None:
        with self.lock:
            previous = self.pending_oi_breakouts.get(current.option_type)
        if not previous or previous.strike != current.strike:
            return None

        if current.option_type == "CALL" and current.ce_change_oi < previous.ce_change_oi:
            return OptionOiSignal(
                option_type=current.option_type,
                strike=current.strike,
                ce_change_oi=current.ce_change_oi,
                pe_change_oi=current.pe_change_oi,
                confirmed=True,
                rule=(
                    "CE resistance change OI decreasing "
                    f"from {previous.ce_change_oi:.2f} to {current.ce_change_oi:.2f}"
                ),
            )

        if current.option_type == "PUT" and current.pe_change_oi < previous.pe_change_oi:
            return OptionOiSignal(
                option_type=current.option_type,
                strike=current.strike,
                ce_change_oi=current.ce_change_oi,
                pe_change_oi=current.pe_change_oi,
                confirmed=True,
                rule=(
                    "PE support change OI decreasing "
                    f"from {previous.pe_change_oi:.2f} to {current.pe_change_oi:.2f}"
                ),
            )

        return None

    def _calculate_trade_size(self, fill_price: float) -> tuple[int, int, float] | None:
        with self.lock:
            capital_sizing_enabled = self.config.capital_sizing_enabled
            trade_capital = self.config.trade_capital
            configured_lots = self.config.lots
            lot_size = self.config.lot_size

        if fill_price <= 0:
            with self.lock:
                self._log(
                    "warn",
                    "Trade Skipped",
                    "Could not size the trade because the option entry price was unavailable.",
                )
                self._persist_and_emit()
            return None

        lots = configured_lots
        if capital_sizing_enabled:
            one_lot_cost = fill_price * lot_size
            lots = int(trade_capital // one_lot_cost)
            if lots < 1:
                with self.lock:
                    self._log(
                        "warn",
                        "Trade Skipped",
                        (
                            f"One lot costs {one_lot_cost:.2f}, which is above the "
                            f"per-trade budget of {trade_capital:.2f}."
                        ),
                        {
                            "entry_price": fill_price,
                            "lot_size": lot_size,
                            "one_lot_cost": one_lot_cost,
                            "trade_capital": trade_capital,
                        },
                    )
                    self._persist_and_emit()
                return None

        quantity = lots * lot_size
        trade_value = fill_price * quantity
        return lots, quantity, trade_value

    def _build_position_state(
        self,
        *,
        contract: OptionContract,
        fill_price: float,
        lots: int,
        quantity: int,
        trade_value: float,
        mode: str,
        order_id: str,
        entry_spot_price: float,
        oi_signal: OptionOiSignal | None,
    ) -> PositionState:
        reference_high = self.reference_levels.previous_day_high
        reference_low = self.reference_levels.previous_day_low
        breakout_buffer = self.config.breakout_buffer
        if contract.option_type == "CALL":
            trigger_price = reference_high + breakout_buffer if reference_high is not None else None
            entry_reason = "Previous day high breakout"
        else:
            trigger_price = reference_low - breakout_buffer if reference_low is not None else None
            entry_reason = "Previous day low breakdown"
        if oi_signal:
            entry_reason = f"{entry_reason} confirmed by option-chain OI: {oi_signal.rule}"

        return PositionState(
            trade_id=uuid4().hex,
            side="BUY",
            option_type=contract.option_type,
            strike=contract.strike,
            security_id=contract.security_id,
            lots=lots,
            lot_size=self.config.lot_size,
            quantity=quantity,
            trade_capital=self.config.trade_capital,
            trade_value=trade_value,
            expiry_date=contract.expiry_date,
            entry_price=fill_price,
            entry_reason=entry_reason,
            entry_spot_price=entry_spot_price,
            entry_trigger_price=trigger_price,
            entry_oi_strike=oi_signal.strike if oi_signal else None,
            entry_ce_change_oi=oi_signal.ce_change_oi if oi_signal else None,
            entry_pe_change_oi=oi_signal.pe_change_oi if oi_signal else None,
            entry_oi_rule=oi_signal.rule if oi_signal else None,
            entry_reference_high=reference_high,
            entry_reference_low=reference_low,
            current_price=fill_price,
            pnl=0.0,
            mode=mode,  # type: ignore[arg-type]
            highest_price_seen=fill_price,
            stop_loss_price=fill_price * (1 - self.config.stop_loss_percent / 100),
            target_price=fill_price * (1 + self.config.target_percent / 100),
            trailing_stop_price=None,
            trailing_armed=False,
            opened_at=self._now(),
            order_id=order_id,
        )

    def _evaluate_reverse_signal_exit(self, previous_spot: float, spot_price: float) -> None:
        with self.lock:
            position = self.runtime.open_position
            previous_high = self.reference_levels.previous_day_high
            previous_low = self.reference_levels.previous_day_low
            reverse_exit_enabled = self.config.reverse_signal_exit_enabled
            breakout_buffer = self.config.breakout_buffer
            exit_in_progress = self.runtime.exit_in_progress

        if not position or position.status != "OPEN" or exit_in_progress or not reverse_exit_enabled:
            return
        if not self._is_market_session_open():
            return
        if (
            position.option_type == "CALL"
            and previous_high is not None
            and previous_spot >= previous_high - breakout_buffer > spot_price
        ):
            self._request_position_close(
                reason="reverse_signal",
                message=f"Reverse signal exit triggered as spot moved back below previous day high at {spot_price:.2f}.",
            )
        if (
            position.option_type == "PUT"
            and previous_low is not None
            and previous_spot <= previous_low + breakout_buffer < spot_price
        ):
            self._request_position_close(
                reason="reverse_signal",
                message=f"Reverse signal exit triggered as spot moved back above previous day low at {spot_price:.2f}.",
            )

    def _request_position_close(
        self,
        *,
        reason: str,
        message: str,
        triggered_price: float | None = None,
    ) -> None:
        with self.lock:
            position = self.runtime.open_position
            if not position or position.status != "OPEN":
                if reason == "manual":
                    self._log("warn", "No Open Position", "There is no active position to close.")
                    self._persist_and_emit()
                return
            if self.runtime.exit_in_progress:
                return

            self.runtime.exit_in_progress = True
            position.exit_requested = True
            mode = position.mode
            quantity = position.quantity
            security_id = position.security_id
            current_price = triggered_price or position.current_price

        if mode == "paper":
            with self.lock:
                self._mark_position_closed(
                    order_id=f"paper-exit-{uuid4().hex[:10]}",
                    exit_price=current_price,
                    exit_reason=reason,
                    exit_reason_detail=message,
                    exit_trigger_price=triggered_price,
                )
                self._log(
                    "trade",
                    "Paper Position Closed",
                    message,
                    {"reason": reason, "exit_price": current_price, "realized_pnl": 0 if not position else (current_price - position.entry_price) * position.quantity},
                )
                self._persist_and_emit()
            return

        try:
            reference_price = triggered_price
            if reference_price is None:
                reference_price = self.gateway.fetch_security_ltp("NSE_FNO", security_id) or current_price
            response = self.gateway.place_exit_order(
                security_id=security_id,
                exchange_segment="NSE_FNO",
                quantity=quantity,
                product_type=self.config.product_type,
                reference_price=reference_price,
                tag=self.gateway.create_tag("exit"),
            )
        except DhanGatewayError as exc:
            with self.lock:
                self.connections.last_error = str(exc)
                self.runtime.exit_in_progress = False
                if self.runtime.open_position:
                    self.runtime.open_position.exit_requested = False
                self._log("error", "Exit Failed", str(exc))
                self._persist_and_emit()
            return

        with self.lock:
            response_payload = self.gateway._unwrap_data_payload(response, "exit order")
            order_id = ""
            realized_pnl = 0.0
            if self.runtime.open_position:
                realized_pnl = (reference_price - self.runtime.open_position.entry_price) * self.runtime.open_position.quantity
            if isinstance(response_payload, dict):
                order_id = str(response_payload.get("orderId") or response_payload.get("order_id") or "")
            self._mark_position_closed(
                order_id=order_id,
                exit_price=reference_price,
                exit_reason=reason,
                exit_reason_detail=message,
                exit_trigger_price=triggered_price,
            )
            self._log(
                "trade",
                "Exit Order Sent",
                message,
                {
                    "reason": reason,
                    "exit_price": reference_price,
                    "realized_pnl": realized_pnl,
                    "response": response,
                },
            )
            self._persist_and_emit()

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)

    def _today_session_date(self) -> str:
        return datetime.now(ZoneInfo(self.settings.app_timezone)).date().isoformat()

    def _trade_session_date(self, position: PositionState) -> str:
        opened_at = position.opened_at
        if opened_at.tzinfo is None:
            opened_at = opened_at.replace(tzinfo=timezone.utc)
        return opened_at.astimezone(ZoneInfo(self.settings.app_timezone)).date().isoformat()

    def _hydrate_session_state_from_history(self, session_date: str) -> None:
        todays_trades = [
            trade for trade in self.runtime.trade_history
            if self._trade_session_date(trade) == session_date
        ]
        self.runtime.trades_today = len(todays_trades)
        self.runtime.previous_high_broken = any(trade.option_type == "CALL" for trade in todays_trades)
        self.runtime.previous_low_broken = any(trade.option_type == "PUT" for trade in todays_trades)
        if todays_trades:
            latest_trade = max(todays_trades, key=lambda trade: trade.opened_at)
            self.runtime.last_signal = latest_trade.option_type
            self.runtime.last_signal_at = latest_trade.opened_at

    def _is_market_session_open(self) -> bool:
        local_now = datetime.now(ZoneInfo(self.settings.app_timezone))
        if local_now.weekday() >= 5:
            return False
        current_time = local_now.time()
        return dt_time(9, 15) <= current_time <= dt_time(15, 30)

    def _no_trade_before_time(self) -> dt_time:
        try:
            hours, minutes = self.config.no_trade_before_time.split(":", maxsplit=1)
            return dt_time(int(hours), int(minutes))
        except ValueError:
            return dt_time(9, 20)

    def _is_trade_entry_window_open(self) -> bool:
        local_now = datetime.now(ZoneInfo(self.settings.app_timezone))
        if local_now.weekday() >= 5:
            return False
        current_time = local_now.time()
        return self._no_trade_before_time() <= current_time <= dt_time(15, 30)

    def _next_trade_window_starts_at(self) -> datetime:
        timezone_info = ZoneInfo(self.settings.app_timezone)
        local_now = datetime.now(timezone_info)
        no_trade_before = self._no_trade_before_time()
        candidate = datetime.combine(local_now.date(), no_trade_before, tzinfo=timezone_info)
        if local_now.weekday() < 5 and local_now.time() < no_trade_before:
            return candidate.astimezone(timezone.utc)
        days_ahead = 1
        while True:
            next_day = local_now.date() + timedelta(days=days_ahead)
            if next_day.weekday() < 5:
                return datetime.combine(next_day, no_trade_before, tzinfo=timezone_info).astimezone(timezone.utc)
            days_ahead += 1

    def _is_auto_close_due(self) -> bool:
        if not self.config.auto_close_enabled:
            return False
        local_now = datetime.now(ZoneInfo(self.settings.app_timezone))
        try:
            hours, minutes = self.config.auto_close_time.split(":", maxsplit=1)
            close_time = dt_time(int(hours), int(minutes))
        except ValueError:
            return False
        return local_now.time() >= close_time

    def _normalize_config(self, config: StrategyConfig) -> StrategyConfig:
        normalized = config.model_copy(deep=True)
        normalized.no_trade_before_time = normalized.no_trade_before_time or self.settings.default_no_trade_before_time
        normalized.stop_loss_percent = min(max(normalized.stop_loss_percent, 5.0), 40.0)
        normalized.target_percent = min(max(normalized.target_percent, 10.0), 100.0)
        normalized.trailing_activation_percent = min(max(normalized.trailing_activation_percent, 5.0), 80.0)
        normalized.trailing_distance_percent = min(max(normalized.trailing_distance_percent, 3.0), 50.0)
        normalized.account_capital = max(normalized.account_capital, 1.0)
        normalized.trade_capital = min(max(normalized.trade_capital, 1.0), normalized.account_capital)
        normalized.lots = max(normalized.lots, 1)
        normalized.lot_size = max(normalized.lot_size, 1)
        normalized.strike_step = max(normalized.strike_step, 50)
        normalized.max_trades_per_day = max(normalized.max_trades_per_day, 1)
        normalized.cooldown_seconds = max(normalized.cooldown_seconds, 0)
        normalized.auto_close_time = normalized.auto_close_time or self.settings.default_auto_close_time
        return normalized

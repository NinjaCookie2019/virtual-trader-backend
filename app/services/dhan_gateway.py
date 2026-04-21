from __future__ import annotations

import asyncio
import contextlib
import io
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from math import ceil, floor
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

import httpx
from dhanhq.dhanhq import dhanhq
from dhanhq.marketfeed import IDX, Ticker, DhanFeed
from dhanhq.orderupdate import OrderSocket

from app.core.config import Settings


class DhanGatewayError(RuntimeError):
    pass


@dataclass(slots=True)
class OptionContract:
    option_type: str
    strike: int
    security_id: str
    exchange_segment: str
    expiry_date: str
    last_price: float | None
    top_bid_price: float | None
    top_ask_price: float | None


@dataclass(slots=True)
class OptionOiSignal:
    option_type: str
    strike: int
    ce_change_oi: float
    pe_change_oi: float
    confirmed: bool
    rule: str


class DhanGateway:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.client = None
        self.client_lock = threading.RLock()
        self.option_chain_lock = threading.Lock()
        self.last_option_chain_request_at = 0.0
        if self.is_configured:
            self.client = self._build_client(settings.dhan_access_token)

    @property
    def is_configured(self) -> bool:
        return bool(self.settings.dhan_client_id and self.settings.dhan_access_token)

    def _build_client(self, access_token: str) -> dhanhq:
        return dhanhq(
            self.settings.dhan_client_id,
            access_token,
            disable_ssl=self.settings.dhan_disable_ssl,
        )

    def update_access_token(self, access_token: str) -> None:
        if not access_token.strip():
            raise DhanGatewayError("Dhan access token cannot be empty.")
        with self.client_lock:
            self.settings.dhan_access_token = access_token.strip()
            self.client = self._build_client(self.settings.dhan_access_token)

    def _ensure_client(self) -> dhanhq:
        with self.client_lock:
            if not self.client:
                raise DhanGatewayError("Dhan credentials are missing. Add them to backend/.env first.")
            return self.client

    def fetch_profile(self) -> dict[str, Any]:
        if not self.is_configured:
            raise DhanGatewayError("Dhan credentials are missing. Add them to backend/.env first.")
        try:
            response = httpx.get(
                "https://api.dhan.co/v2/profile",
                headers={"access-token": self.settings.dhan_access_token},
                timeout=15.0,
            )
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPStatusError as exc:
            raise DhanGatewayError(f"Dhan profile request failed: HTTP {exc.response.status_code}") from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise DhanGatewayError(f"Dhan profile request failed: {exc}") from exc
        if not isinstance(payload, dict):
            raise DhanGatewayError(f"Dhan profile response was not usable: {payload}")
        return payload

    def fetch_token_valid_until(self) -> datetime | None:
        profile = self.fetch_profile()
        token_validity = profile.get("tokenValidity") or profile.get("token_validity")
        return self.parse_token_validity(token_validity)

    def renew_access_token(self) -> tuple[str, datetime | None, dict[str, Any]]:
        if not self.is_configured:
            raise DhanGatewayError("Dhan credentials are missing. Add them to backend/.env first.")
        try:
            response = httpx.get(
                "https://api.dhan.co/v2/RenewToken",
                headers={
                    "access-token": self.settings.dhan_access_token,
                    "dhanClientId": self.settings.dhan_client_id,
                },
                timeout=20.0,
            )
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPStatusError as exc:
            raise DhanGatewayError(f"Dhan token renewal failed: HTTP {exc.response.status_code}") from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise DhanGatewayError(f"Dhan token renewal failed: {exc}") from exc

        if not isinstance(payload, dict):
            raise DhanGatewayError(f"Dhan token renewal response was not usable: {payload}")

        new_token = self.extract_access_token(payload)
        if not new_token:
            raise DhanGatewayError(f"Dhan token renewal response did not include a new access token: {payload}")
        token_valid_until = self.extract_token_expiry(payload)
        self.update_access_token(new_token)
        return new_token, token_valid_until, payload

    def fetch_previous_day_levels(self) -> tuple[float, float, str]:
        client = self._ensure_client()
        today = date.today()
        from_date = (today - timedelta(days=14)).isoformat()
        to_date = today.isoformat()
        response = client.historical_daily_data(
            security_id=self.settings.underlying_security_id,
            exchange_segment=self.settings.underlying_exchange_segment,
            instrument_type=self.settings.underlying_instrument_type,
            from_date=from_date,
            to_date=to_date,
        )
        data = response.get("data") or {}
        timestamps = data.get("timestamp") or data.get("start_Time") or []
        highs = data.get("high") or []
        lows = data.get("low") or []
        if not timestamps or not highs or not lows:
            raise DhanGatewayError(f"Historical data did not include usable candles: {response}")

        candle_date = self._epoch_to_date_string(timestamps[-1])
        return float(highs[-1]), float(lows[-1]), candle_date

    def fetch_expiry_list(self) -> list[str]:
        client = self._ensure_client()
        response = client.expiry_list(
            under_security_id=int(self.settings.underlying_security_id),
            under_exchange_segment=self.settings.underlying_exchange_segment,
        )
        payload = self._unwrap_data_payload(response, "expiry list")
        expiries = payload if isinstance(payload, list) else payload.get("data", [])
        if not expiries:
            raise DhanGatewayError(f"No expiries returned for {self.settings.underlying_name}: {response}")
        return sorted(expiries)

    def fetch_option_chain(self, expiry_date: str) -> dict[str, Any]:
        client = self._ensure_client()
        with self.option_chain_lock:
            now = time.monotonic()
            wait_seconds = self.settings.option_chain_poll_seconds - (now - self.last_option_chain_request_at)
            if wait_seconds > 0:
                time.sleep(wait_seconds)
            response = client.option_chain(
                under_security_id=int(self.settings.underlying_security_id),
                under_exchange_segment=self.settings.underlying_exchange_segment,
                expiry=expiry_date,
            )
            self.last_option_chain_request_at = time.monotonic()
        data = self._unwrap_data_payload(response, "option chain")
        option_chain = data.get("oc") or {}
        if not option_chain:
            raise DhanGatewayError(f"Option chain response did not include strikes: {response}")
        return data

    def resolve_contract(self, spot_price: float, option_type: str, strike_step: int, expiry_date: str) -> OptionContract:
        chain = self.fetch_option_chain(expiry_date)
        return self.resolve_contract_from_chain(chain, spot_price, option_type, strike_step, expiry_date)

    def resolve_contract_from_chain(
        self,
        chain: dict[str, Any],
        spot_price: float,
        option_type: str,
        strike_step: int,
        expiry_date: str,
    ) -> OptionContract:
        strike = self._calculate_otm_strike(spot_price, option_type, strike_step)
        strike_key = f"{strike:.6f}"
        strike_row = (chain.get("oc") or {}).get(strike_key)
        if not strike_row:
            available = ", ".join(list((chain.get("oc") or {}).keys())[:5])
            raise DhanGatewayError(
                f"Strike {strike_key} not found in option chain for {expiry_date}. Sample strikes: {available}"
            )

        leg = strike_row["ce" if option_type == "CALL" else "pe"]
        return OptionContract(
            option_type=option_type,
            strike=strike,
            security_id=str(leg["security_id"]),
            exchange_segment="NSE_FNO",
            expiry_date=expiry_date,
            last_price=self._to_float(leg.get("last_price")),
            top_bid_price=self._to_float(leg.get("top_bid_price")),
            top_ask_price=self._to_float(leg.get("top_ask_price")),
        )

    def evaluate_oi_confirmation(
        self,
        chain: dict[str, Any],
        option_type: str,
        trigger_price: float,
        strike_step: int,
    ) -> OptionOiSignal:
        strike = self._calculate_oi_confirmation_strike(trigger_price, option_type, strike_step)
        strike_key = f"{strike:.6f}"
        strike_row = (chain.get("oc") or {}).get(strike_key)
        if not strike_row:
            available = ", ".join(list((chain.get("oc") or {}).keys())[:5])
            raise DhanGatewayError(
                f"OI confirmation strike {strike_key} not found in option chain. Sample strikes: {available}"
            )

        ce_change_oi = self._extract_change_oi(strike_row.get("ce") or {})
        pe_change_oi = self._extract_change_oi(strike_row.get("pe") or {})
        if ce_change_oi is None or pe_change_oi is None:
            raise DhanGatewayError(
                f"OI confirmation data missing at strike {strike}. CE change OI={ce_change_oi}, PE change OI={pe_change_oi}."
            )

        if option_type == "CALL":
            confirmed = pe_change_oi > ce_change_oi
            rule = "PE change OI > CE change OI"
        else:
            confirmed = ce_change_oi > pe_change_oi
            rule = "CE change OI > PE change OI"

        return OptionOiSignal(
            option_type=option_type,
            strike=strike,
            ce_change_oi=ce_change_oi,
            pe_change_oi=pe_change_oi,
            confirmed=confirmed,
            rule=rule,
        )

    def get_strike_oi_change(self, chain: dict[str, Any], strike: int) -> dict[str, float | None]:
        strike_key = f"{strike:.6f}"
        strike_row = (chain.get("oc") or {}).get(strike_key)
        if not strike_row:
            available = ", ".join(list((chain.get("oc") or {}).keys())[:5])
            raise DhanGatewayError(
                f"Strike {strike_key} not found in option chain. Sample strikes: {available}"
            )

        ce_leg = strike_row.get("ce") or {}
        pe_leg = strike_row.get("pe") or {}
        ce_change_oi = self._extract_change_oi(ce_leg)
        pe_change_oi = self._extract_change_oi(pe_leg)
        if ce_change_oi is None or pe_change_oi is None:
            raise DhanGatewayError(
                f"Change OI data missing at strike {strike}. CE={ce_change_oi}, PE={pe_change_oi}."
            )

        return {
            "ce_change_oi": ce_change_oi,
            "pe_change_oi": pe_change_oi,
            "ce_last_price": self._to_float(ce_leg.get("last_price")),
            "pe_last_price": self._to_float(pe_leg.get("last_price")),
            "ce_oi": self._extract_current_oi(ce_leg),
            "pe_oi": self._extract_current_oi(pe_leg),
        }

    def fetch_security_quote(self, exchange_segment: str, security_id: str) -> dict[str, Any]:
        client = self._ensure_client()
        response = client.quote_data({exchange_segment: [int(security_id)]})
        data = self._unwrap_data_payload(response, "security quote")
        segment_payload = data.get(exchange_segment) or {}
        return segment_payload.get(str(security_id)) or next(iter(segment_payload.values()), {})

    def fetch_security_ltp(self, exchange_segment: str, security_id: str) -> float | None:
        client = self._ensure_client()
        response = client.ticker_data({exchange_segment: [int(security_id)]})
        data = self._unwrap_data_payload(response, "security ltp")
        segment_payload = data.get(exchange_segment) or {}
        instrument = segment_payload.get(str(security_id)) or next(iter(segment_payload.values()), {})
        return self._to_float(instrument.get("last_price") or instrument.get("LTP"))

    def place_entry_order(self, contract: OptionContract, quantity: int, product_type: str, tag: str) -> dict[str, Any]:
        return self._place_limit_order(
            security_id=contract.security_id,
            exchange_segment=contract.exchange_segment,
            transaction_type="BUY",
            quantity=quantity,
            product_type=product_type,
            limit_price=contract.top_ask_price or contract.last_price,
            tag=tag,
        )

    def place_exit_order(self, security_id: str, exchange_segment: str, quantity: int, product_type: str, reference_price: float, tag: str) -> dict[str, Any]:
        return self._place_limit_order(
            security_id=security_id,
            exchange_segment=exchange_segment,
            transaction_type="SELL",
            quantity=quantity,
            product_type=product_type,
            limit_price=reference_price,
            tag=tag,
        )

    def _place_limit_order(
        self,
        *,
        security_id: str,
        exchange_segment: str,
        transaction_type: str,
        quantity: int,
        product_type: str,
        limit_price: float | None,
        tag: str,
    ) -> dict[str, Any]:
        client = self._ensure_client()
        if limit_price is None:
            raise DhanGatewayError("A limit price could not be resolved for this contract.")
        response = client.place_order(
            security_id=security_id,
            exchange_segment=exchange_segment,
            transaction_type=transaction_type,
            quantity=quantity,
            order_type="LIMIT",
            product_type=product_type,
            price=float(limit_price),
            tag=tag,
        )
        return response

    def stream_market_feed(
        self,
        stop_event: threading.Event,
        on_tick: Callable[[dict[str, Any]], None],
        on_status: Callable[[bool, str | None], None],
    ) -> None:
        if not self.is_configured:
            on_status(False, "Dhan credentials missing.")
            return

        while not stop_event.is_set():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                feed = DhanFeed(
                    self.settings.dhan_client_id,
                    self.settings.dhan_access_token,
                    [(IDX, self.settings.underlying_security_id, Ticker)],
                    version="v2",
                )
                feed.run_forever()
                on_status(True, None)
                while not stop_event.is_set():
                    packet = feed.get_data()
                    if packet:
                        on_tick(packet)
            except Exception as exc:
                on_status(False, str(exc))
                time.sleep(5)
            finally:
                on_status(False, None)
                loop.stop()
                loop.close()

    def stream_order_updates(
        self,
        stop_event: threading.Event,
        on_update: Callable[[dict[str, Any]], None],
        on_status: Callable[[bool, str | None], None],
    ) -> None:
        if not self.is_configured:
            on_status(False, "Dhan credentials missing.")
            return

        socket = OrderSocket(self.settings.dhan_client_id, self.settings.dhan_access_token)

        async def handle(message: dict[str, Any]) -> None:
            on_update(message)

        socket.handle_order_update = handle  # type: ignore[method-assign]
        on_status(True, None)
        while not stop_event.is_set():
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    socket.connect_to_dhan_websocket_sync()
            except Exception as exc:
                on_status(False, str(exc))
                time.sleep(5)
        on_status(False, None)

    @staticmethod
    def create_tag(prefix: str) -> str:
        return f"{prefix}-{uuid4().hex[:12]}"

    @staticmethod
    def extract_access_token(payload: dict[str, Any]) -> str | None:
        candidates: list[Any] = [
            payload.get("accessToken"),
            payload.get("access_token"),
            payload.get("token"),
        ]
        nested = payload.get("data")
        if isinstance(nested, dict):
            candidates.extend([
                nested.get("accessToken"),
                nested.get("access_token"),
                nested.get("token"),
            ])
        for candidate in candidates:
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
        return None

    @staticmethod
    def extract_token_expiry(payload: dict[str, Any]) -> datetime | None:
        candidates: list[Any] = [
            payload.get("expiryTime"),
            payload.get("tokenValidity"),
            payload.get("token_validity"),
        ]
        nested = payload.get("data")
        if isinstance(nested, dict):
            candidates.extend([
                nested.get("expiryTime"),
                nested.get("tokenValidity"),
                nested.get("token_validity"),
            ])
        for candidate in candidates:
            parsed = DhanGateway.parse_token_validity(candidate)
            if parsed:
                return parsed
        return None

    @staticmethod
    def parse_token_validity(value: Any) -> datetime | None:
        if not isinstance(value, str) or not value.strip():
            return None
        raw = value.strip()
        formats = (
            "%d/%m/%Y %H:%M",
            "%d/%m/%Y %H:%M:%S",
            "%Y-%m-%dT%H:%M:%S.%f",
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%d %H:%M:%S.%f",
            "%Y-%m-%d %H:%M:%S",
        )
        for fmt in formats:
            try:
                return datetime.strptime(raw, fmt).replace(tzinfo=ZoneInfo("Asia/Kolkata")).astimezone(timezone.utc)
            except ValueError:
                continue
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=ZoneInfo("Asia/Kolkata")).astimezone(timezone.utc)
        return parsed.astimezone(timezone.utc)

    @staticmethod
    def _to_float(value: Any) -> float | None:
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _calculate_oi_confirmation_strike(trigger_price: float, option_type: str, strike_step: int) -> int:
        if option_type == "CALL":
            return int(ceil(trigger_price / strike_step) * strike_step)
        return int(floor(trigger_price / strike_step) * strike_step)

    @staticmethod
    def _extract_change_oi(leg: dict[str, Any]) -> float | None:
        direct_keys = (
            "change_oi",
            "changeOI",
            "change_in_oi",
            "changeInOI",
            "oi_change",
            "oiChange",
            "changeinOpenInterest",
            "change_in_open_interest",
        )
        for key in direct_keys:
            parsed = DhanGateway._to_float(leg.get(key))
            if parsed is not None:
                return parsed

        current_oi = DhanGateway._to_float(
            leg.get("oi")
            or leg.get("open_interest")
            or leg.get("openInterest")
            or leg.get("OI")
        )
        previous_oi = DhanGateway._to_float(
            leg.get("previous_oi")
            or leg.get("previousOI")
            or leg.get("prev_oi")
            or leg.get("prevOI")
            or leg.get("previous_open_interest")
            or leg.get("previousOpenInterest")
        )
        if current_oi is None or previous_oi is None:
            return None
        return current_oi - previous_oi

    @staticmethod
    def _extract_current_oi(leg: dict[str, Any]) -> float | None:
        return DhanGateway._to_float(
            leg.get("oi")
            or leg.get("open_interest")
            or leg.get("openInterest")
            or leg.get("OI")
        )

    @staticmethod
    def _epoch_to_date_string(value: Any) -> str:
        try:
            epoch = float(value)
        except (TypeError, ValueError) as exc:
            raise DhanGatewayError(f"Unexpected timestamp value from historical data: {value}") from exc
        return datetime.utcfromtimestamp(epoch).date().isoformat()

    @staticmethod
    def _unwrap_data_payload(response: dict[str, Any], context: str) -> Any:
        status = response.get("status")
        if status and str(status).lower() != "success":
            raise DhanGatewayError(DhanGateway._format_failure_message(response, context))

        payload = response.get("data")
        while isinstance(payload, dict) and "data" in payload:
            nested_status = payload.get("status")
            if nested_status and str(nested_status).lower() not in {"success", "successful"}:
                raise DhanGatewayError(DhanGateway._format_failure_message(response, context))
            payload = payload.get("data")

        if payload is None:
            raise DhanGatewayError(f"Dhan {context} response was empty: {response}")
        return payload

    @staticmethod
    def _format_failure_message(response: dict[str, Any], context: str) -> str:
        remarks = response.get("remarks")
        if isinstance(remarks, dict):
            error_message = remarks.get("error_message")
            if error_message:
                return f"Dhan {context} request failed: {error_message}"

        payload = response.get("data")
        while isinstance(payload, dict) and "data" in payload:
            nested_data = payload.get("data")
            if isinstance(nested_data, dict) and nested_data:
                first_key = next(iter(nested_data))
                first_value = nested_data[first_key]
                if str(first_key).isdigit() and isinstance(first_value, str):
                    return f"Dhan {context} request failed ({first_key}): {first_value}"
            payload = nested_data

        return f"Dhan {context} request failed."

    @staticmethod
    def _calculate_otm_strike(spot_price: float, option_type: str, strike_step: int) -> int:
        if option_type == "CALL":
            return int(floor(spot_price / strike_step) * strike_step + strike_step)
        rounded_down = int(floor(spot_price / strike_step) * strike_step)
        if spot_price % strike_step == 0:
            return rounded_down - strike_step
        return rounded_down

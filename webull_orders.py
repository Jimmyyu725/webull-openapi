from __future__ import annotations

import json
import uuid
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Optional


ACCOUNT_BY_INSTRUMENT = {
    "EQUITY": "INDIVIDUAL_MARGIN",
    "OPTION": "INDIVIDUAL_MARGIN",
    "FUTURES": "FUTURES",
    "CRYPTO": "CRYPTO",
    "EVENT": "EVENTS_CASH",
}


def load_json(value: Optional[str], default: Any) -> Any:
    if not value:
        return default
    if value.startswith("@"):
        return json.loads(Path(value[1:]).read_text(encoding="utf-8"))
    return json.loads(value)


def positive_decimal(name: str, value: Any) -> str:
    try:
        number = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError(f"{name} must be numeric") from exc
    if number <= 0:
        raise ValueError(f"{name} must be greater than zero")
    return format(number, "f")


def validate_order(order: dict[str, Any]) -> None:
    required = (
        "client_order_id",
        "combo_type",
        "symbol",
        "instrument_type",
        "market",
        "order_type",
        "quantity",
        "side",
        "time_in_force",
        "entrust_type",
    )
    missing = [key for key in required if not order.get(key)]
    if missing:
        raise ValueError(f"Missing order fields: {', '.join(missing)}")

    order_type = str(order["order_type"]).upper()
    legs = order.get("legs") or []
    instrument_type = str(order["instrument_type"]).upper()
    side = str(order["side"]).upper()
    tif = str(order["time_in_force"]).upper()

    positive_decimal("quantity", order["quantity"])
    if order_type in {"LIMIT", "STOP_LOSS_LIMIT"}:
        positive_decimal("limit_price", order.get("limit_price"))
    if order_type in {"STOP_LOSS", "STOP_LOSS_LIMIT"}:
        positive_decimal("stop_price", order.get("stop_price"))

    if instrument_type == "OPTION":
        strategy = str(order.get("option_strategy", "")).upper()
        stock_combo = strategy in {"COVERED_STOCK", "COLLAR_WITH_STOCK"}
        if order_type not in {"LIMIT", "STOP_LOSS", "STOP_LOSS_LIMIT"} and not (
            order_type == "MARKET" and stock_combo
        ):
            raise ValueError("Options support LIMIT, STOP_LOSS, or STOP_LOSS_LIMIT")
        if not order.get("option_strategy") or not legs:
            raise ValueError("Option orders require option_strategy and legs")
        if side == "SELL" and tif != "DAY":
            raise ValueError("Option sell orders require DAY time-in-force")
        for leg in legs:
            missing = [key for key in ("side", "quantity", "symbol", "instrument_type", "market")
                       if not leg.get(key)]
            if missing:
                raise ValueError(f"Missing option leg fields: {', '.join(missing)}")
            leg_type = str(leg["instrument_type"]).upper()
            if leg_type not in {"OPTION", "EQUITY"}:
                raise ValueError("Option strategy legs must use instrument_type OPTION or EQUITY")
            if leg_type == "OPTION":
                missing = [key for key in ("strike_price", "option_expire_date", "option_type")
                           if not leg.get(key)]
                if missing:
                    raise ValueError(f"Missing option contract fields: {', '.join(missing)}")
                positive_decimal("option leg strike_price", leg["strike_price"])
            positive_decimal("option leg quantity", leg["quantity"])

    if instrument_type == "EVENT":
        if order_type != "LIMIT" or tif != "DAY":
            raise ValueError("Event orders require LIMIT and DAY")
        if str(order.get("event_outcome", "")).lower() not in {"yes", "no"}:
            raise ValueError("Event orders require event_outcome yes or no")
        price = Decimal(str(order["limit_price"]))
        if not Decimal("0.01") <= price <= Decimal("0.99"):
            raise ValueError("Event limit price must be between 0.01 and 0.99")

    if instrument_type == "CRYPTO" and order_type not in {"MARKET", "LIMIT", "STOP_LOSS_LIMIT"}:
        raise ValueError("Crypto supports MARKET, LIMIT, or STOP_LOSS_LIMIT")
    if instrument_type == "CRYPTO":
        if order_type == "MARKET" and tif != "IOC":
            raise ValueError("Crypto MARKET orders require IOC time-in-force")
        if order_type != "MARKET" and tif not in {"DAY", "GTC"}:
            raise ValueError("Crypto LIMIT and STOP_LOSS_LIMIT orders require DAY or GTC")

    if order_type == "TRAILING_STOP_LOSS":
        if tif != "DAY":
            raise ValueError("Trailing stop orders require DAY time-in-force")
        if not order.get("trailing_type") or order.get("trailing_stop_step") is None:
            raise ValueError("Trailing stop orders require trailing_type and trailing_stop_step")
        positive_decimal("trailing_stop_step", order["trailing_stop_step"])


def build_order(
    *,
    symbol: Optional[str],
    instrument_type: str,
    side: str,
    quantity: str,
    order_type: str,
    tif: str,
    limit_price: Optional[str] = None,
    stop_price: Optional[str] = None,
    client_order_id: Optional[str] = None,
    combo_type: str = "NORMAL",
    entrust_type: str = "QTY",
    session: Optional[str] = None,
    event_outcome: Optional[str] = None,
    option_strategy: Optional[str] = None,
    legs: Optional[list[dict[str, Any]]] = None,
    extra: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    normalized_instrument_type = instrument_type.upper()
    if not symbol:
        raise ValueError("symbol is required")
    order = {
        "client_order_id": client_order_id or uuid.uuid4().hex,
        "combo_type": combo_type.upper(),
        "symbol": symbol.upper(),
        "instrument_type": normalized_instrument_type,
        "market": "US",
        "order_type": order_type.upper(),
        "quantity": positive_decimal("quantity", quantity),
        "side": side.upper(),
        "time_in_force": tif.upper(),
        "entrust_type": entrust_type.upper(),
    }
    if limit_price is not None:
        order["limit_price"] = positive_decimal("limit_price", limit_price)
    if stop_price is not None:
        order["stop_price"] = positive_decimal("stop_price", stop_price)
    if session:
        order["support_trading_session"] = session.upper()
    if event_outcome:
        order["event_outcome"] = event_outcome.lower()
    if option_strategy:
        order["option_strategy"] = option_strategy.upper()
    if legs:
        order["legs"] = legs
    if extra:
        order.update(extra)

    validate_order(order)
    return order


def order_instrument_type(order: dict[str, Any]) -> str:
    instrument_type = str(order.get("instrument_type", "")).upper()
    if instrument_type:
        return instrument_type
    legs = order.get("legs") or []
    if legs and any(str(leg.get("instrument_type", "")).upper() == "OPTION" for leg in legs):
        return "OPTION"
    raise ValueError("Cannot determine order instrument type")


def validate_batch_orders(batch_orders: list[dict[str, Any]]) -> None:
    if not batch_orders:
        raise ValueError("Batch order list cannot be empty")
    if len(batch_orders) > 50:
        raise ValueError("Webull accepts at most 50 orders per batch")
    for order in batch_orders:
        validate_order(order)
        if order_instrument_type(order) != "EQUITY":
            raise ValueError("Webull batch placement currently supports equities only")

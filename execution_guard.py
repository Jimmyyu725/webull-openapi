from __future__ import annotations

import json
from datetime import datetime, time, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

from config import API_ENDPOINT


AUTHORIZATION_FILE = Path(__file__).parent / "reports" / "sandbox-execution-authorization.json"
SANDBOX_ENDPOINT = "api.sandbox.webull.com"
NEW_YORK = ZoneInfo("America/New_York")
PORTFOLIO_LIMIT_CEILINGS = {
    "max_gross_exposure_fraction": Decimal("0.05"),
    "max_single_position_fraction": Decimal("0.025"),
    "max_trade_risk_fraction": Decimal("0.0005"),
    "max_daily_loss_fraction": Decimal("0.0015"),
    "max_rolling_five_day_loss_fraction": Decimal("0.004"),
    "max_experiment_drawdown_fraction": Decimal("0.0075"),
}


def authorization_status(
    policy_file: Optional[Path] = None,
    *,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    path = policy_file or AUTHORIZATION_FILE
    output: dict[str, Any] = {
        "policy_file": str(path),
        "readable": False,
        "authorization_level": "BLOCKED",
        "new_entries_authorized": False,
        "approved_strategy_ids": [],
        "approved_symbols": [],
        "expires_at": None,
        "max_order_notional_fraction": None,
        "max_quote_age_seconds": None,
        "max_bid_ask_spread_fraction": None,
        "max_order_to_displayed_ask_fraction": None,
        **{name: None for name in PORTFOLIO_LIMIT_CEILINGS},
        "max_position_count": None,
        "max_daily_entries": None,
        "mandatory_exit_time_et": None,
        "blocking_reasons": ["policy_unreadable"],
        "orders_enabled": False,
    }
    try:
        policy = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return output
    if not isinstance(policy, dict):
        return output

    strategies = policy.get("approved_strategy_ids")
    symbols = policy.get("approved_symbols")
    expires_at = policy.get("expires_at")
    try:
        max_order_notional_fraction = Decimal(
            str(policy.get("max_order_notional_fraction"))
        )
    except (ArithmeticError, ValueError):
        max_order_notional_fraction = None
    notional_limit_valid = bool(
        max_order_notional_fraction is not None
        and max_order_notional_fraction.is_finite()
        and Decimal("0") < max_order_notional_fraction <= Decimal("0.025")
    )
    try:
        max_quote_age_seconds = Decimal(str(policy.get("max_quote_age_seconds")))
        max_bid_ask_spread_fraction = Decimal(
            str(policy.get("max_bid_ask_spread_fraction"))
        )
        max_order_to_displayed_ask_fraction = Decimal(
            str(policy.get("max_order_to_displayed_ask_fraction"))
        )
    except (ArithmeticError, ValueError):
        max_quote_age_seconds = None
        max_bid_ask_spread_fraction = None
        max_order_to_displayed_ask_fraction = None
    market_limits_valid = bool(
        max_quote_age_seconds is not None
        and max_quote_age_seconds.is_finite()
        and Decimal("0") < max_quote_age_seconds <= Decimal("5")
        and max_bid_ask_spread_fraction is not None
        and max_bid_ask_spread_fraction.is_finite()
        and Decimal("0") < max_bid_ask_spread_fraction <= Decimal("0.0005")
        and max_order_to_displayed_ask_fraction is not None
        and max_order_to_displayed_ask_fraction.is_finite()
        and Decimal("0") < max_order_to_displayed_ask_fraction <= Decimal("1")
    )
    portfolio_limits: dict[str, Optional[Decimal]] = {}
    for name, ceiling in PORTFOLIO_LIMIT_CEILINGS.items():
        try:
            value = Decimal(str(policy.get(name)))
        except (ArithmeticError, ValueError):
            value = None
        portfolio_limits[name] = value
        if value is None or not value.is_finite() or not Decimal("0") < value <= ceiling:
            portfolio_limits[name] = None
    max_position_count = policy.get("max_position_count")
    max_daily_entries = policy.get("max_daily_entries")
    mandatory_exit_time_et = policy.get("mandatory_exit_time_et")
    try:
        exit_time = time.fromisoformat(str(mandatory_exit_time_et))
    except ValueError:
        exit_time = None
    portfolio_limits_valid = bool(
        all(value is not None for value in portfolio_limits.values())
        and not isinstance(max_position_count, bool)
        and isinstance(max_position_count, int)
        and 1 <= max_position_count <= 2
        and not isinstance(max_daily_entries, bool)
        and isinstance(max_daily_entries, int)
        and 1 <= max_daily_entries <= 3
        and exit_time is not None
        and exit_time <= time(15, 55)
        and max_order_notional_fraction is not None
        and portfolio_limits["max_single_position_fraction"] is not None
        and max_order_notional_fraction
        <= portfolio_limits["max_single_position_fraction"]
        and portfolio_limits["max_daily_loss_fraction"]
        <= portfolio_limits["max_rolling_five_day_loss_fraction"]
        <= portfolio_limits["max_experiment_drawdown_fraction"]
    )
    structurally_valid = bool(
        policy.get("version") == 4
        and policy.get("environment") == SANDBOX_ENDPOINT
        and isinstance(strategies, list)
        and all(isinstance(item, str) and item for item in strategies)
        and isinstance(symbols, list)
        and all(isinstance(item, str) and item for item in symbols)
        and notional_limit_valid
        and market_limits_valid
        and portfolio_limits_valid
    )
    expires_in_future = False
    if expires_at:
        try:
            expiry = datetime.fromisoformat(str(expires_at))
            expires_in_future = bool(
                expiry.tzinfo is not None
                and expiry > (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
            )
        except ValueError:
            expires_in_future = False

    checks = {
        "policy_valid": structurally_valid,
        "sandbox_endpoint": API_ENDPOINT == SANDBOX_ENDPOINT,
        "sandbox_micro_level": policy.get("authorization_level") == "SANDBOX_MICRO",
        "new_entries_enabled": policy.get("new_entries_enabled") is True,
        "approved_strategy_present": bool(strategies),
        "approved_symbol_present": bool(symbols),
        "order_notional_limit_present": notional_limit_valid,
        "market_quality_limits_present": market_limits_valid,
        "portfolio_risk_limits_present": portfolio_limits_valid,
        "not_expired": expires_in_future,
    }
    blocking = [name for name, passed in checks.items() if not passed]
    output.update({
        "readable": structurally_valid,
        "authorization_level": policy.get("authorization_level", "BLOCKED"),
        "new_entries_authorized": not blocking,
        "approved_strategy_ids": strategies if isinstance(strategies, list) else [],
        "approved_symbols": symbols if isinstance(symbols, list) else [],
        "expires_at": expires_at,
        "max_order_notional_fraction": (
            str(max_order_notional_fraction) if notional_limit_valid else None
        ),
        "max_quote_age_seconds": (
            str(max_quote_age_seconds) if market_limits_valid else None
        ),
        "max_bid_ask_spread_fraction": (
            str(max_bid_ask_spread_fraction) if market_limits_valid else None
        ),
        "max_order_to_displayed_ask_fraction": (
            str(max_order_to_displayed_ask_fraction) if market_limits_valid else None
        ),
        **{
            name: str(value) if portfolio_limits_valid and value is not None else None
            for name, value in portfolio_limits.items()
        },
        "max_position_count": max_position_count if portfolio_limits_valid else None,
        "max_daily_entries": max_daily_entries if portfolio_limits_valid else None,
        "mandatory_exit_time_et": (
            str(mandatory_exit_time_et) if portfolio_limits_valid else None
        ),
        "gate_checks": checks,
        "blocking_reasons": blocking,
        "orders_enabled": not blocking,
    })
    return output


def authorize_automated_order(
    strategy_id: str,
    symbol: str,
    side: str,
    *,
    policy_file: Optional[Path] = None,
    now: Optional[datetime] = None,
    current_position_quantity: Optional[Decimal] = None,
    managed_position_quantity: Optional[Decimal] = None,
    order_quantity: Optional[Decimal] = None,
    current_open_order_count: Optional[int] = None,
    current_buying_power: Optional[Decimal] = None,
    order_reference_price: Optional[Decimal] = None,
    current_best_bid: Optional[Decimal] = None,
    current_best_ask: Optional[Decimal] = None,
    current_ask_size: Optional[Decimal] = None,
    quote_age_seconds: Optional[Decimal] = None,
    account_net_liquidation_value: Optional[Decimal] = None,
    current_gross_exposure: Optional[Decimal] = None,
    current_position_count: Optional[int] = None,
    order_risk_at_stop: Optional[Decimal] = None,
    current_daily_loss: Optional[Decimal] = None,
    current_rolling_five_day_loss: Optional[Decimal] = None,
    current_experiment_drawdown: Optional[Decimal] = None,
    current_daily_entry_count: Optional[int] = None,
) -> dict[str, Any]:
    normalized_side = side.upper()
    status = authorization_status(policy_file, now=now)
    reasons = list(status["blocking_reasons"])
    if API_ENDPOINT != SANDBOX_ENDPOINT:
        return {"authorized": False, "mode": "BLOCKED", "blocking_reasons": reasons}
    if normalized_side == "SELL":
        if current_position_quantity is None or order_quantity is None:
            return {
                "authorized": False,
                "mode": "BLOCKED",
                "blocking_reasons": ["risk_reduction_quantity_unverified"],
            }
        if current_open_order_count is None or current_open_order_count < 0:
            return {
                "authorized": False,
                "mode": "BLOCKED",
                "blocking_reasons": ["open_order_state_unverified"],
            }
        if current_open_order_count:
            return {
                "authorized": False,
                "mode": "BLOCKED",
                "blocking_reasons": ["open_orders_present"],
            }
        if not current_position_quantity.is_finite() or current_position_quantity <= 0:
            return {
                "authorized": False,
                "mode": "BLOCKED",
                "blocking_reasons": ["no_verified_long_position"],
            }
        if not order_quantity.is_finite() or order_quantity <= 0:
            return {
                "authorized": False,
                "mode": "BLOCKED",
                "blocking_reasons": ["invalid_risk_reduction_quantity"],
            }
        if order_quantity > current_position_quantity:
            return {
                "authorized": False,
                "mode": "BLOCKED",
                "blocking_reasons": ["sell_exceeds_verified_long_position"],
            }
        if managed_position_quantity is None:
            return {
                "authorized": False,
                "mode": "BLOCKED",
                "blocking_reasons": ["position_ownership_unverified"],
            }
        if not managed_position_quantity.is_finite() or managed_position_quantity <= 0:
            return {
                "authorized": False,
                "mode": "BLOCKED",
                "blocking_reasons": ["position_not_owned_by_strategy"],
            }
        if current_position_quantity != managed_position_quantity:
            return {
                "authorized": False,
                "mode": "BLOCKED",
                "blocking_reasons": ["position_ownership_mismatch"],
            }
        return {"authorized": True, "mode": "RISK_REDUCTION", "blocking_reasons": []}
    if normalized_side != "BUY":
        return {
            "authorized": False,
            "mode": "BLOCKED",
            "blocking_reasons": ["unsupported_side"],
        }
    if (
        current_position_quantity is None
        or order_quantity is None
        or current_open_order_count is None
        or current_open_order_count < 0
    ):
        reasons.append("new_risk_state_unverified")
    else:
        if not current_position_quantity.is_finite() or current_position_quantity != 0:
            reasons.append("position_already_exists")
        if not order_quantity.is_finite() or order_quantity <= 0:
            reasons.append("invalid_new_risk_quantity")
        if current_open_order_count:
            reasons.append("open_orders_present")
    order_notional = None
    maximum_order_notional = None
    bid_ask_spread_fraction = None
    maximum_displayed_order_quantity = None
    if current_buying_power is None or order_reference_price is None:
        reasons.append("capital_state_unverified")
    elif (
        not current_buying_power.is_finite()
        or not order_reference_price.is_finite()
        or current_buying_power <= 0
        or order_reference_price <= 0
    ):
        reasons.append("invalid_capital_state")
    elif (
        order_quantity is not None
        and order_quantity.is_finite()
        and order_quantity > 0
    ):
        order_notional = order_quantity * order_reference_price
        fraction = status.get("max_order_notional_fraction")
        if fraction is None:
            reasons.append("order_notional_limit_unavailable")
        else:
            maximum_order_notional = current_buying_power * Decimal(str(fraction))
            if order_notional > maximum_order_notional:
                reasons.append("order_notional_limit_exceeded")
    market_values = (
        current_best_bid,
        current_best_ask,
        current_ask_size,
        quote_age_seconds,
    )
    if any(value is None for value in market_values):
        reasons.append("market_state_unverified")
    elif any(not value.is_finite() for value in market_values if value is not None):
        reasons.append("invalid_market_state")
    elif (
        current_best_bid <= 0
        or current_best_ask <= 0
        or current_ask_size <= 0
    ):
        reasons.append("invalid_market_state")
    else:
        if quote_age_seconds < Decimal("-2"):
            reasons.append("market_quote_from_future")
        max_quote_age = status.get("max_quote_age_seconds")
        if max_quote_age is None:
            reasons.append("market_limits_unavailable")
        elif quote_age_seconds > Decimal(str(max_quote_age)):
            reasons.append("stale_market_quote")
        if current_best_ask < current_best_bid:
            reasons.append("crossed_market_quote")
        else:
            midpoint = (current_best_ask + current_best_bid) / Decimal("2")
            bid_ask_spread_fraction = (current_best_ask - current_best_bid) / midpoint
            max_spread = status.get("max_bid_ask_spread_fraction")
            if max_spread is None:
                reasons.append("market_limits_unavailable")
            elif bid_ask_spread_fraction > Decimal(str(max_spread)):
                reasons.append("market_spread_exceeded")
        if order_reference_price is not None and order_reference_price != current_best_ask:
            reasons.append("reference_price_not_current_ask")
        max_displayed_fraction = status.get("max_order_to_displayed_ask_fraction")
        if max_displayed_fraction is None:
            reasons.append("market_limits_unavailable")
        else:
            maximum_displayed_order_quantity = current_ask_size * Decimal(
                str(max_displayed_fraction)
            )
            if (
                order_quantity is not None
                and order_quantity.is_finite()
                and order_quantity > maximum_displayed_order_quantity
            ):
                reasons.append("displayed_liquidity_exceeded")
    if strategy_id not in status["approved_strategy_ids"]:
        reasons.append("strategy_not_approved")
    if symbol not in status["approved_symbols"]:
        reasons.append("symbol_not_approved")
    portfolio_values = (
        account_net_liquidation_value,
        current_gross_exposure,
        order_risk_at_stop,
        current_daily_loss,
        current_rolling_five_day_loss,
        current_experiment_drawdown,
    )
    portfolio_counts = (current_position_count, current_daily_entry_count)
    if not (status.get("gate_checks") or {}).get("portfolio_risk_limits_present"):
        reasons.append("portfolio_risk_limits_unavailable")
    elif any(value is None for value in portfolio_values + portfolio_counts):
        reasons.append("portfolio_risk_state_unverified")
    elif (
        any(not value.is_finite() for value in portfolio_values if value is not None)
        or account_net_liquidation_value <= 0
        or current_gross_exposure < 0
        or order_risk_at_stop <= 0
        or current_daily_loss < 0
        or current_rolling_five_day_loss < 0
        or current_experiment_drawdown < 0
        or isinstance(current_position_count, bool)
        or not isinstance(current_position_count, int)
        or current_position_count < 0
        or isinstance(current_daily_entry_count, bool)
        or not isinstance(current_daily_entry_count, int)
        or current_daily_entry_count < 0
    ):
        reasons.append("invalid_portfolio_risk_state")
    else:
        nlv = account_net_liquidation_value
        if order_notional is not None:
            if order_notional > nlv * Decimal(str(status["max_single_position_fraction"])):
                reasons.append("single_position_limit_exceeded")
            if (
                current_gross_exposure + order_notional
                > nlv * Decimal(str(status["max_gross_exposure_fraction"]))
            ):
                reasons.append("gross_exposure_limit_exceeded")
        if current_position_count + 1 > int(status["max_position_count"]):
            reasons.append("position_count_limit_exceeded")
        if order_risk_at_stop > nlv * Decimal(str(status["max_trade_risk_fraction"])):
            reasons.append("trade_risk_limit_exceeded")
        if current_daily_loss >= nlv * Decimal(str(status["max_daily_loss_fraction"])):
            reasons.append("daily_loss_limit_reached")
        if current_rolling_five_day_loss >= nlv * Decimal(
            str(status["max_rolling_five_day_loss_fraction"])
        ):
            reasons.append("rolling_five_day_loss_limit_reached")
        if current_experiment_drawdown >= nlv * Decimal(
            str(status["max_experiment_drawdown_fraction"])
        ):
            reasons.append("experiment_drawdown_limit_reached")
        if current_daily_entry_count >= int(status["max_daily_entries"]):
            reasons.append("daily_entry_limit_reached")
    evaluation_time = now or datetime.now(timezone.utc)
    if evaluation_time.tzinfo is None:
        reasons.append("risk_clock_unverified")
    elif status.get("mandatory_exit_time_et"):
        cutoff = time.fromisoformat(str(status["mandatory_exit_time_et"]))
        if evaluation_time.astimezone(NEW_YORK).time() >= cutoff:
            reasons.append("mandatory_exit_window_reached")
    return {
        "authorized": not reasons,
        "mode": "SANDBOX_MICRO" if not reasons else "BLOCKED",
        "blocking_reasons": list(dict.fromkeys(reasons)),
        "order_notional": str(order_notional) if order_notional is not None else None,
        "maximum_order_notional": (
            str(maximum_order_notional) if maximum_order_notional is not None else None
        ),
        "bid_ask_spread_fraction": (
            str(bid_ask_spread_fraction) if bid_ask_spread_fraction is not None else None
        ),
        "maximum_displayed_order_quantity": (
            str(maximum_displayed_order_quantity)
            if maximum_displayed_order_quantity is not None
            else None
        ),
    }

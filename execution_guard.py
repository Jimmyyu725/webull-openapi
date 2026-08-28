from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional

from config import API_ENDPOINT


AUTHORIZATION_FILE = Path(__file__).parent / "reports" / "sandbox-execution-authorization.json"
SANDBOX_ENDPOINT = "api.sandbox.webull.com"


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
    structurally_valid = bool(
        policy.get("version") == 2
        and policy.get("environment") == SANDBOX_ENDPOINT
        and isinstance(strategies, list)
        and all(isinstance(item, str) and item for item in strategies)
        and isinstance(symbols, list)
        and all(isinstance(item, str) and item for item in symbols)
        and notional_limit_valid
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
    order_quantity: Optional[Decimal] = None,
    current_open_order_count: Optional[int] = None,
    current_buying_power: Optional[Decimal] = None,
    order_reference_price: Optional[Decimal] = None,
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
    if strategy_id not in status["approved_strategy_ids"]:
        reasons.append("strategy_not_approved")
    if symbol not in status["approved_symbols"]:
        reasons.append("symbol_not_approved")
    return {
        "authorized": not reasons,
        "mode": "SANDBOX_MICRO" if not reasons else "BLOCKED",
        "blocking_reasons": list(dict.fromkeys(reasons)),
        "order_notional": str(order_notional) if order_notional is not None else None,
        "maximum_order_notional": (
            str(maximum_order_notional) if maximum_order_notional is not None else None
        ),
    }

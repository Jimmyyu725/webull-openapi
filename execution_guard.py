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
    structurally_valid = bool(
        policy.get("version") == 1
        and policy.get("environment") == SANDBOX_ENDPOINT
        and isinstance(strategies, list)
        and all(isinstance(item, str) and item for item in strategies)
        and isinstance(symbols, list)
        and all(isinstance(item, str) and item for item in symbols)
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
        if current_position_quantity <= 0:
            return {
                "authorized": False,
                "mode": "BLOCKED",
                "blocking_reasons": ["no_verified_long_position"],
            }
        if order_quantity <= 0:
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
    if strategy_id not in status["approved_strategy_ids"]:
        reasons.append("strategy_not_approved")
    if symbol not in status["approved_symbols"]:
        reasons.append("symbol_not_approved")
    return {
        "authorized": not reasons,
        "mode": "SANDBOX_MICRO" if not reasons else "BLOCKED",
        "blocking_reasons": list(dict.fromkeys(reasons)),
    }

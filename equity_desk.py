from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from config import API_ENDPOINT
from equity_forward_recorder import DATABASE, execution_diagnostics, status as recorder_status
from webull_api import WebullAPI, normalize_result


ACCOUNT_CLASSES = ("INDIVIDUAL_MARGIN", "CRYPTO")
CRYPTO_STATE_FILE = (
    Path.home()
    / "Library"
    / "Application Support"
    / "WebullCryptoSandbox"
    / "state"
    / "crypto_strategy.json"
)
CRYPTO_LAUNCH_PLIST = (
    Path.home() / "Library" / "LaunchAgents" / "com.jingtianyu.webull-crypto-sandbox.plist"
)


def _list(value: Any) -> list[dict[str, Any]]:
    return value if isinstance(value, list) else []


def _account_summary(api: WebullAPI, account: dict[str, Any]) -> dict[str, Any]:
    account_id = str(account["account_id"])
    try:
        balance = normalize_result(api.trade.account_v2.get_account_balance(account_id))
        positions = normalize_result(api.trade.account_v2.get_account_position(account_id))
        orders = normalize_result(api.trade.order_v3.get_order_open(account_id, page_size=50))
    except Exception as exc:
        return {
            "account_class": account.get("account_class"),
            "account_number": account.get("account_number"),
            "readable": False,
            "read_failures": [f"request:{type(exc).__name__}"],
            "net_liquidation_value": None,
            "market_value": None,
            "buying_power": None,
            "positions": [],
            "open_order_count": None,
        }
    failures = [
        name
        for name, result in (("balance", balance), ("positions", positions), ("open_orders", orders))
        if not result.ok
    ]
    balance_data = balance.data if isinstance(balance.data, dict) else {}
    currency_assets = _list(balance_data.get("account_currency_assets"))
    primary_asset = currency_assets[0] if currency_assets else {}
    position_rows = _list(positions.data)
    order_rows = _list(orders.data)
    return {
        "account_class": account.get("account_class"),
        "account_number": account.get("account_number"),
        "readable": not failures,
        "read_failures": failures,
        "net_liquidation_value": balance_data.get("total_net_liquidation_value"),
        "market_value": balance_data.get("total_market_value"),
        "buying_power": primary_asset.get("day_buying_power") or primary_asset.get("buying_power"),
        "positions": [
            {
                "symbol": row.get("symbol"),
                "instrument_type": row.get("instrument_type"),
                "quantity": row.get("quantity"),
                "cost_price": row.get("cost_price") or row.get("cost"),
                "market_value": row.get("market_value"),
                "unrealized_profit_loss": row.get("unrealized_profit_loss"),
            }
            for row in position_rows
        ],
        "open_order_count": len(order_rows),
    }


def _crypto_automation_status() -> dict[str, Any]:
    state = None
    readable = True
    if CRYPTO_STATE_FILE.exists():
        try:
            state = json.loads(CRYPTO_STATE_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            readable = False
    return {
        "status": (
            "state_unreadable"
            if not readable
            else ("not_started" if not state else ("completed" if state.get("completed") else "running"))
        ),
        "state": state,
        "state_readable": readable,
        "launch_agent": {"installed": CRYPTO_LAUNCH_PLIST.exists()},
    }


def desk_status(
    api: WebullAPI,
    *,
    database: Path = DATABASE,
    crypto_automation: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    recorder = recorder_status(database)
    tca = execution_diagnostics(database)
    accounts = {str(row.get("account_class")): row for row in api.accounts(refresh=True)}
    account_summaries = []
    missing_accounts = []
    for account_class in ACCOUNT_CLASSES:
        account = accounts.get(account_class)
        if account is None:
            missing_accounts.append(account_class)
            continue
        account_summaries.append(_account_summary(api, account))

    equity = next(
        (row for row in account_summaries if row["account_class"] == "INDIVIDUAL_MARGIN"),
        None,
    )
    unmanaged_symbols = sorted(
        {
            str(row["symbol"])
            for row in (equity or {}).get("positions", [])
            if row.get("symbol")
        }
    )
    reasons = []
    if API_ENDPOINT != "api.sandbox.webull.com":
        reasons.append("production_endpoint")
    if recorder["decision"] != "DATA_USABLE":
        reasons.append("forward_data_pending")
    if missing_accounts or any(not row["readable"] for row in account_summaries):
        reasons.append("account_state_unreadable")
    if (equity or {}).get("open_order_count", 0):
        reasons.append("preexisting_equity_open_orders")
    if unmanaged_symbols:
        reasons.append("unmanaged_equity_positions")

    if crypto_automation is None:
        crypto_automation = _crypto_automation_status()
    crypto_state = (crypto_automation or {}).get("state") or {}
    crypto_pending = crypto_state.get("pending_orders") or {}
    crypto_installed = ((crypto_automation or {}).get("launch_agent") or {}).get("installed")
    if crypto_installed and (crypto_automation or {}).get("state_readable") is False:
        reasons.append("legacy_crypto_state_unreadable")
    if crypto_installed and crypto_state and not crypto_state.get("paused") and not crypto_state.get("completed"):
        reasons.append("legacy_crypto_new_risk_enabled")
    if crypto_pending:
        reasons.append("legacy_crypto_pending_orders")

    authorization_level = (
        "RESEARCH" if recorder["decision"] == "DATA_USABLE" else "DATA_COLLECTION"
    )
    tca_progress = {
        symbol: {
            horizon: {
                "paired_observations": metrics["paired_observations"],
                "minimum_required_gross_edge_bps": metrics["minimum_required_gross_edge_bps"],
            }
            for horizon, metrics in item["horizons"].items()
        }
        for symbol, item in tca["symbols"].items()
    }
    return {
        "environment": (
            "Webull Sandbox / Paper Trading"
            if API_ENDPOINT == "api.sandbox.webull.com"
            else "UNSAFE_NON_SANDBOX"
        ),
        "endpoint": API_ENDPOINT,
        "authorization_level": authorization_level,
        "global_posture": "NO_NEW_RISK",
        "automatic_equity_trading": {
            "status": "BLOCKED",
            "new_entries_allowed": False,
            "position_exits_allowed": False,
            "reasons": reasons or ["strategy_not_preregistered"],
            "blocked_symbols": unmanaged_symbols,
            "next_gate": (
                "complete_forward_data"
                if recorder["decision"] != "DATA_USABLE"
                else "preregister_strategy"
            ),
        },
        "forward_data": {
            "decision": recorder["decision"],
            "complete_session_count": recorder.get("complete_session_count", 0),
            "target_complete_sessions": recorder["target_complete_sessions"],
        },
        "tca_progress": tca_progress,
        "accounts": account_summaries,
        "missing_accounts": missing_accounts,
        "legacy_crypto_automation": {
            "status": (crypto_automation or {}).get("status"),
            "installed": crypto_installed,
            "state_readable": (crypto_automation or {}).get("state_readable", True),
            "paused": crypto_state.get("paused"),
            "pending_order_count": len(crypto_pending),
            "submitted_order_count": len(crypto_state.get("submitted_order_ids") or []),
        },
        "orders_enabled": False,
    }

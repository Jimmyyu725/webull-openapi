from __future__ import annotations

import fcntl
import json
import os
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

from config import API_ENDPOINT
from equity_forward_recorder import (
    APP_DIR,
    DATABASE,
    _session_schedule,
    execution_diagnostics,
    status as recorder_status,
)
from execution_guard import authorization_status
from webull_api import WebullAPI, normalize_result


ACCOUNT_CLASSES = ("INDIVIDUAL_MARGIN", "CRYPTO")
RESEARCH_LEDGER = Path(__file__).parent / "reports" / "research-attempt-ledger.json"
DESK_JOURNAL = APP_DIR / "desk-journal.jsonl"
DESK_JOURNAL_LOCK = APP_DIR / "desk-journal.lock"
DESK_JOURNAL_REQUIRED_FROM = date(2026, 8, 31)
DESK_JOURNAL_PRE_OPEN_DUE = time(8, 25)
DESK_JOURNAL_POST_CLOSE_GRACE_MINUTES = 2
EASTERN = ZoneInfo("America/New_York")
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


def research_status(ledger: Path = RESEARCH_LEDGER) -> dict[str, Any]:
    try:
        data = json.loads(ledger.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {
            "readable": False,
            "decision": "BLOCKED",
            "ledger": str(ledger),
            "attempt_count": 0,
            "deployable_count": 0,
        }
    attempts = data.get("attempts")
    policy = data.get("policy")
    identifiers = [item.get("id") for item in attempts] if isinstance(attempts, list) else []
    readable = bool(
        data.get("version") == 1
        and isinstance(attempts, list)
        and isinstance(policy, dict)
        and all(isinstance(item, dict) and item.get("id") for item in attempts)
        and len(identifiers) == len(set(identifiers))
    )
    if not readable:
        return {
            "readable": False,
            "decision": "BLOCKED",
            "ledger": str(ledger),
            "attempt_count": len(attempts) if isinstance(attempts, list) else 0,
            "deployable_count": 0,
        }
    outcomes: dict[str, int] = {}
    for item in attempts:
        outcome = str(item.get("outcome"))
        outcomes[outcome] = outcomes.get(outcome, 0) + 1
    return {
        "readable": True,
        "decision": "RESEARCH_DEBT_RECORDED",
        "ledger": str(ledger),
        "frozen_at": data.get("frozen_at"),
        "attempt_count": len(attempts),
        "strategy_family_count": len({item.get("family") for item in attempts}),
        "deployable_count": sum(
            bool(item.get("deployable_under_current_mandate")) for item in attempts
        ),
        "consumed_holdout_count": sum(
            item.get("holdout_state") == "CONSUMED" for item in attempts
        ),
        "non_independent_count": sum(
            item.get("holdout_state") == "NOT_INDEPENDENT" for item in attempts
        ),
        "unrequested_holdout_count": sum(
            item.get("holdout_state") == "NOT_REQUESTED" for item in attempts
        ),
        "outcomes": dict(sorted(outcomes.items())),
        "next_candidate_budget": policy.get("next_candidate_budget"),
        "parameter_search_allowed": policy.get("parameter_search_allowed"),
    }


def desk_status(
    api: WebullAPI,
    *,
    database: Path = DATABASE,
    crypto_automation: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    recorder = recorder_status(database)
    tca = execution_diagnostics(database)
    research = research_status()
    execution_authorization = authorization_status()
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
    if not research["readable"]:
        reasons.append("research_ledger_unreadable")
    elif research["deployable_count"] == 0:
        reasons.append("no_approved_strategy")

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
                "qualified_forward_data"
                if recorder["decision"] != "DATA_USABLE"
                else "preregister_strategy"
            ),
        },
        "forward_data": {
            "decision": recorder["decision"],
            "complete_session_count": recorder.get("complete_session_count", 0),
            "qualified_session_count": recorder.get("qualified_session_count", 0),
            "rejected_complete_sessions": recorder.get(
                "rejected_complete_sessions", []
            ),
            "target_qualified_sessions": recorder.get(
                "target_qualified_sessions", recorder["target_complete_sessions"]
            ),
        },
        "tca_progress": tca_progress,
        "research": research,
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
        "execution_authorization": execution_authorization,
        "orders_enabled": False,
    }


def _journal_records(journal: Path) -> tuple[list[dict[str, Any]], list[int]]:
    if not journal.exists():
        return [], []
    records = []
    invalid_lines = []
    for line_number, line in enumerate(journal.read_text(encoding="utf-8").splitlines(), start=1):
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            invalid_lines.append(line_number)
            continue
        desk = record.get("desk") if isinstance(record, dict) else None
        if not (
            isinstance(record, dict)
            and record.get("record_key")
            and record.get("recorded_at")
            and record.get("session_day")
            and record.get("phase") in {"pre_open", "regular_hours", "post_close", "closed"}
            and isinstance(desk, dict)
            and desk.get("authorization_level")
            and desk.get("orders_enabled") is False
        ):
            invalid_lines.append(line_number)
            continue
        records.append(record)
    return records, invalid_lines


def desk_journal_status(
    journal: Path = DESK_JOURNAL,
    *,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    records, invalid_lines = _journal_records(journal)
    now = (now or datetime.now(timezone.utc)).astimezone(EASTERN)
    phases: dict[str, int] = {}
    authorizations: dict[str, int] = {}
    phases_by_day: dict[str, set[str]] = {}
    for record in records:
        phase = str(record.get("phase"))
        phases[phase] = phases.get(phase, 0) + 1
        phases_by_day.setdefault(str(record.get("session_day")), set()).add(phase)
        authorization = str((record.get("desk") or {}).get("authorization_level"))
        authorizations[authorization] = authorizations.get(authorization, 0) + 1
    session_checks = []
    day = DESK_JOURNAL_REQUIRED_FROM
    while day <= now.date():
        session_type, scheduled_close = _session_schedule(day)
        expected = []
        if session_type in {"FULL", "EARLY_CLOSE"}:
            if day < now.date() or now.time() >= DESK_JOURNAL_PRE_OPEN_DUE:
                expected.append("pre_open")
            close = datetime.combine(day, scheduled_close, tzinfo=EASTERN)
            close += timedelta(minutes=DESK_JOURNAL_POST_CLOSE_GRACE_MINUTES)
            if now >= close:
                expected.append("post_close")
        if expected:
            observed = phases_by_day.get(day.isoformat(), set())
            missing = [phase for phase in expected if phase not in observed]
            session_checks.append({
                "session_day": day.isoformat(),
                "session_type": session_type,
                "required_phases": expected,
                "missing_phases": missing,
            })
        day += timedelta(days=1)
    missing_sessions = [item for item in session_checks if item["missing_phases"]]
    last = records[-1] if records else {}
    return {
        "readable": not invalid_lines,
        "decision": (
            "BLOCKED"
            if invalid_lines or missing_sessions
            else (
                "AUDIT_OK"
                if session_checks
                else ("AUDIT_ARMED" if records else "NOT_STARTED")
            )
        ),
        "journal": str(journal),
        "entry_count": len(records),
        "unique_session_count": len({record.get("session_day") for record in records}),
        "phase_counts": dict(sorted(phases.items())),
        "authorization_counts": dict(sorted(authorizations.items())),
        "invalid_lines": invalid_lines,
        "required_from": DESK_JOURNAL_REQUIRED_FROM.isoformat(),
        "required_phases": ["pre_open", "post_close"],
        "post_close_grace_minutes": DESK_JOURNAL_POST_CLOSE_GRACE_MINUTES,
        "checked_session_count": len(session_checks),
        "compliant_session_count": len(session_checks) - len(missing_sessions),
        "missing_required_phase_sessions": missing_sessions,
        "last_recorded_at": last.get("recorded_at"),
        "last_record_key": last.get("record_key"),
        "orders_enabled": False,
    }


def _session_phase(now: datetime) -> tuple[str, str]:
    local = now.astimezone(EASTERN)
    session_type, scheduled_close = _session_schedule(local.date())
    if session_type in {"CLOSED", "UNSUPPORTED"}:
        phase = "closed"
    elif local.time() < time(9, 30):
        phase = "pre_open"
    elif scheduled_close is not None and local.time() < scheduled_close:
        phase = "regular_hours"
    else:
        phase = "post_close"
    return local.date().isoformat(), phase


def record_desk_snapshot(
    api: WebullAPI,
    *,
    now: Optional[datetime] = None,
    database: Path = DATABASE,
    journal: Path = DESK_JOURNAL,
    lock_file: Path = DESK_JOURNAL_LOCK,
    crypto_automation: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    if API_ENDPOINT != "api.sandbox.webull.com":
        raise RuntimeError("Desk journal is restricted to the Webull Sandbox endpoint")
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    session_day, phase = _session_phase(now)
    record_key = f"{session_day}:{phase}"
    journal.parent.mkdir(parents=True, exist_ok=True)
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    with lock_file.open("w", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        records, invalid_lines = _journal_records(journal)
        if invalid_lines:
            raise RuntimeError("Desk journal is unreadable; refusing to append")
        existing = next(
            (record for record in records if record.get("record_key") == record_key),
            None,
        )
        if existing:
            return {
                "outcome": "duplicate_phase",
                "record": existing,
                "journal_status": desk_journal_status(journal),
            }
        desk = desk_status(api, database=database, crypto_automation=crypto_automation)
        record = {
            "record_key": record_key,
            "recorded_at": now.isoformat(),
            "session_day": session_day,
            "phase": phase,
            "desk": desk,
        }
        with journal.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    return {
        "outcome": "recorded",
        "record": record,
        "journal_status": desk_journal_status(journal),
    }

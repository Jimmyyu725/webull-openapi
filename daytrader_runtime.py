from __future__ import annotations

import fcntl
import json
import os
import plistlib
import shutil
import subprocess
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterator, Optional

from crypto_runtime import (
    APP_SUPPORT_DIR,
    DEPLOY_DIR,
    DEPLOY_VENV,
    LAUNCH_LABEL,
    LAUNCH_PLIST,
    ROOT,
    TERMINAL_ORDER_STATUSES,
    _already_exists,
    _buying_power,
    _instrument_rule,
    _managed_position_quantity,
    _order_status,
    _open_order_count,
    _position_map,
    _snapshots,
    ensure_sandbox,
)
from crypto_strategy import deterministic_order_id, floor_time, quantity_for_notional, webull_bars
from daytrader_strategy import (
    ALLOCATION,
    MAX_HOLD_BARS,
    STOP_LOSS,
    TAKE_PROFIT,
    TIMEFRAME,
    decision_snapshot,
    load_report,
)
from execution_guard import authorization_status, authorize_automated_order
from runtime_environment import ensure_runtime_venv
from webull_api import WebullAPI, normalize_result, redact_secrets
from webull_orders import build_order


STATE_DIR = APP_SUPPORT_DIR / "state"
STATE_FILE = STATE_DIR / "daytrader.json"
LOG_FILE = STATE_DIR / "daytrader.jsonl"
LOCK_FILE = STATE_DIR / "daytrader.lock"
EXPERIMENT_REPORT = ROOT / "reports" / "daytrader-experiment.md"
POLL_SECONDS = 5
ERROR_BACKOFF_SECONDS = 30
MAX_DAILY_ENTRIES = 2
MAX_DAILY_LOSSES = 2
MAX_HOLD = timedelta(seconds=MAX_HOLD_BARS * 300)
STRATEGY_ID = "crypto-day-v2"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def read_state() -> Optional[dict[str, Any]]:
    if not STATE_FILE.exists():
        return None
    return json.loads(STATE_FILE.read_text(encoding="utf-8"))


def write_state(state: dict[str, Any]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    temporary = STATE_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    temporary.replace(STATE_FILE)


def log_event(event: str, **data: Any) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    record = {"time": utc_now().isoformat(), "event": event, **data}
    with LOG_FILE.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")


@contextmanager
def strategy_lock() -> Iterator[None]:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with LOCK_FILE.open("w", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("Another Sandbox day-trader cycle is active") from exc
        yield


def initialize_state(now: Optional[datetime] = None) -> dict[str, Any]:
    existing = read_state()
    if existing:
        if existing.get("strategy") != "crypto-day-v2":
            raise RuntimeError("Existing day-trader state predates the professional deployment gate")
        if "managed_positions" not in existing:
            existing["version"] = 3
            existing["managed_positions"] = {}
            write_state(existing)
            log_event("state_migrated", version=3, ownership_default="unmanaged")
        elif not isinstance(existing["managed_positions"], dict):
            raise RuntimeError("Managed position state is unreadable")
        return existing
    report = load_report()
    symbols = list(report.get("deployment_symbols", []))
    datasets = report.get("sources", {}).get("coinbase_m5_90d", {}).get("symbols", {})
    if not symbols:
        raise RuntimeError("No symbol passed the professional gate; decision is NO_TRADE")
    if any(
        not datasets.get(symbol, {}).get("professional_gate", {}).get("passed")
        for symbol in symbols
    ):
        raise RuntimeError("A deployment symbol does not satisfy the professional gate")
    now = now or utc_now()
    state = {
        "version": 3,
        "strategy": "crypto-day-v2",
        "started_at": now.isoformat(),
        "ends_at": (now + timedelta(days=30)).isoformat(),
        "symbols": symbols,
        "last_bar_check_bucket": None,
        "last_processed_candle": {},
        "pending_orders": {},
        "managed_positions": {},
        "submitted_order_ids": [],
        "entry_times": {},
        "entries_by_day": {},
        "risk_day": now.date().isoformat(),
        "daily_losses": 0,
        "paused": False,
        "halted": False,
        "halt_reason": None,
        "completed": False,
    }
    write_state(state)
    log_event("daytrader_started", symbols=symbols, ends_at=state["ends_at"])
    return state


def set_paused(paused: bool) -> dict[str, Any]:
    state = read_state()
    if not state:
        raise RuntimeError("The Sandbox day trader has not started")
    state["paused"] = paused
    if not paused:
        state["halted"] = False
        state["halt_reason"] = None
    write_state(state)
    log_event("paused" if paused else "resumed")
    return state


def _submit_order(
    api: WebullAPI,
    account_id: str,
    state: dict[str, Any],
    *,
    symbol: str,
    side: str,
    quantity: Decimal,
    event_time: datetime,
    reason: str,
    estimated_loss: bool = False,
    reference_price: Optional[Decimal] = None,
) -> dict[str, Any]:
    current_position = _position_map(api, account_id).get(symbol)
    current_open_order_count = _open_order_count(api, account_id)
    capital = (
        {
            "current_buying_power": _buying_power(api, account_id),
            "order_reference_price": reference_price,
        }
        if side.upper() == "BUY"
        else {}
    )
    ownership = (
        {"managed_position_quantity": _managed_position_quantity(state, symbol)}
        if side.upper() == "SELL"
        else {}
    )
    authorization = authorize_automated_order(
        STRATEGY_ID,
        symbol,
        side,
        current_position_quantity=(
            Decimal(str(current_position["quantity"]))
            if current_position
            else Decimal("0")
        ),
        order_quantity=quantity,
        current_open_order_count=current_open_order_count,
        **ownership,
        **capital,
    )
    if not authorization["authorized"]:
        log_event(
            "order_blocked",
            symbol=symbol,
            side=side,
            reasons=authorization["blocking_reasons"],
        )
        return {"blocked": True, "authorization": authorization}
    order_id = deterministic_order_id(symbol, event_time, side, strategy="crypto-day-v2")
    if order_id in state["submitted_order_ids"] or _already_exists(api, account_id, order_id):
        log_event("duplicate_order_skipped", symbol=symbol, side=side, order_id=order_id)
        return {"order_id": order_id, "duplicate": True}
    order = build_order(
        symbol=symbol,
        instrument_type="CRYPTO",
        side=side,
        quantity=format(quantity, "f"),
        order_type="MARKET",
        tif="IOC",
        client_order_id=order_id,
    )
    state["submitted_order_ids"].append(order_id)
    state["submitted_order_ids"] = state["submitted_order_ids"][-1000:]
    write_state(state)
    try:
        result = normalize_result(api.trade.order_v3.place_order(account_id, [order]))
    except Exception as exc:
        state["pending_orders"][symbol] = {
            "order_id": order_id,
            "side": side,
            "quantity": format(quantity, "f"),
            "reason": reason,
            "estimated_loss": estimated_loss,
            "submitted_at": utc_now().isoformat(),
            "uncertain": True,
        }
        state["halted"] = True
        state["halt_reason"] = "order_outcome_uncertain"
        write_state(state)
        log_event("order_uncertain", symbol=symbol, side=side, order_id=order_id)
        raise RuntimeError("Sandbox order outcome is uncertain; automatic retry was suppressed") from exc
    if not result.ok:
        state["halted"] = True
        state["halt_reason"] = f"order_failed_{result.status}"
        write_state(state)
        log_event("order_failed", symbol=symbol, side=side, order_id=order_id, status=result.status)
        raise RuntimeError(f"Sandbox order failed with status {result.status}")
    state["pending_orders"][symbol] = {
        "order_id": order_id,
        "side": side,
        "quantity": format(quantity, "f"),
        "reason": reason,
        "estimated_loss": estimated_loss,
        "submitted_at": utc_now().isoformat(),
    }
    write_state(state)
    log_event(
        "order_submitted",
        symbol=symbol,
        side=side,
        quantity=format(quantity, "f"),
        order_id=order_id,
        reason=reason,
    )
    return {"order_id": order_id, "duplicate": False, "data": result.data}


def _reconcile_pending(
    api: WebullAPI,
    account_id: str,
    state: dict[str, Any],
    positions: dict[str, dict[str, Any]],
    now: datetime,
) -> None:
    for symbol, pending in list(state["pending_orders"].items()):
        side = pending["side"]
        position = positions.get(symbol)
        position_quantity = (
            Decimal(str(position["quantity"])) if position is not None else Decimal("0")
        )
        pending_quantity = Decimal(str(pending["quantity"]))
        managed_quantity = _managed_position_quantity(state, symbol)
        resolved = (
            side == "BUY"
            and position is not None
            and position_quantity == pending_quantity
        ) or (
            side == "SELL"
            and managed_quantity is not None
            and managed_quantity - pending_quantity == position_quantity
        )
        status = ""
        if not resolved:
            try:
                detail = normalize_result(api.trade.order_v3.get_order_detail(account_id, pending["order_id"]))
                if detail.ok:
                    status = _order_status(detail.data)
            except Exception:
                status = ""
        age = now - datetime.fromisoformat(pending["submitted_at"])
        if resolved:
            if side == "BUY":
                state["managed_positions"][symbol] = {
                    "quantity": pending["quantity"],
                    "entry_order_id": pending["order_id"],
                    "opened_at": pending["submitted_at"],
                }
                state["entry_times"][symbol] = pending["submitted_at"]
            else:
                if position_quantity == 0:
                    state["managed_positions"].pop(symbol, None)
                    state["entry_times"].pop(symbol, None)
                else:
                    state["managed_positions"][symbol]["quantity"] = format(
                        position_quantity, "f"
                    )
                    state["managed_positions"][symbol]["last_exit_order_id"] = pending[
                        "order_id"
                    ]
                if pending.get("estimated_loss"):
                    state["daily_losses"] += 1
            del state["pending_orders"][symbol]
            log_event("order_reconciled", symbol=symbol, side=side, order_id=pending["order_id"])
        elif pending.get("uncertain") and age >= timedelta(minutes=2):
            pending["manual_review"] = True
        elif status in TERMINAL_ORDER_STATUSES or age >= timedelta(minutes=2):
            del state["pending_orders"][symbol]
            log_event(
                "order_unfilled",
                symbol=symbol,
                side=side,
                order_id=pending["order_id"],
                status=status or "timeout",
            )


def _reset_daily_limits(state: dict[str, Any], now: datetime) -> None:
    today = now.date().isoformat()
    if state.get("risk_day") != today:
        state["risk_day"] = today
        state["daily_losses"] = 0
        state["entries_by_day"] = {
            day: symbols for day, symbols in state.get("entries_by_day", {}).items() if day == today
        }
        log_event("daily_limits_reset", day=today)


def _ensure_old_runner_paused() -> None:
    from crypto_runtime import read_state as read_old_state

    old_state = read_old_state()
    if old_state and not old_state.get("paused") and not old_state.get("completed"):
        raise RuntimeError("Pause or replace the existing M120 runner before starting the day trader")


def _close_positions(
    api: WebullAPI,
    account_id: str,
    state: dict[str, Any],
    positions: dict[str, dict[str, Any]],
    now: datetime,
) -> None:
    for symbol, position in positions.items():
        if symbol not in state["symbols"] or symbol in state["pending_orders"]:
            continue
        _submit_order(
            api,
            account_id,
            state,
            symbol=symbol,
            side="SELL",
            quantity=Decimal(str(position["quantity"])),
            event_time=now.replace(microsecond=0),
            reason="experiment_end",
            estimated_loss=Decimal(str(position.get("unrealized_profit_loss", "0"))) < 0,
        )


def run_once(api: WebullAPI, *, confirmed: bool, now: Optional[datetime] = None) -> dict[str, Any]:
    if not confirmed:
        raise ValueError("Automatic Sandbox day trading requires --yes")
    ensure_sandbox()
    _ensure_old_runner_paused()
    now = now or utc_now()
    with strategy_lock():
        state = initialize_state(now)
        if state["completed"]:
            return {"status": "completed", "state": state}
        account_id = api.account_id("CRYPTO")
        positions = _position_map(api, account_id)
        _reconcile_pending(api, account_id, state, positions, now)
        positions = _position_map(api, account_id)
        _reset_daily_limits(state, now)
        if state.get("halted"):
            write_state(state)
            return {
                "status": "halted",
                "reason": state.get("halt_reason"),
                "pending_orders": state["pending_orders"],
            }

        if now >= datetime.fromisoformat(state["ends_at"]):
            _close_positions(api, account_id, state, positions, now)
            positions = _position_map(api, account_id)
            if not positions and not state["pending_orders"]:
                state["completed"] = True
                log_event("daytrader_completed")
            write_state(state)
            return {"status": "closing" if not state["completed"] else "completed", "state": state}

        symbols = list(state["symbols"])
        snapshots = _snapshots(api, symbols)
        for symbol, position in positions.items():
            if symbol not in symbols or symbol in state["pending_orders"]:
                continue
            snapshot = snapshots.get(symbol)
            if not snapshot:
                continue
            bid = Decimal(str(snapshot.get("bid") or snapshot.get("price")))
            cost_price = Decimal(str(position["cost_price"]))
            entered_at = datetime.fromisoformat(state["entry_times"].get(symbol, state["started_at"]))
            reason = None
            if bid <= cost_price * (Decimal("1") - STOP_LOSS):
                reason = "stop_loss"
            elif bid >= cost_price * (Decimal("1") + TAKE_PROFIT):
                reason = "take_profit"
            elif now - entered_at >= MAX_HOLD:
                reason = "max_hold"
            if reason:
                _submit_order(
                    api,
                    account_id,
                    state,
                    symbol=symbol,
                    side="SELL",
                    quantity=Decimal(str(position["quantity"])),
                    event_time=now.replace(microsecond=0),
                    reason=reason,
                    estimated_loss=bid < cost_price,
                )

        bucket = floor_time(now, 300).isoformat()
        if state.get("last_bar_check_bucket") != bucket:
            bars_by_symbol = webull_bars(
                api, symbols, timespan=TIMEFRAME, count=600, now=now, days=2
            )
            today = now.date().isoformat()
            entered_today = state["entries_by_day"].setdefault(today, [])
            for symbol in symbols:
                bars, quality = bars_by_symbol[symbol]
                if not quality.passed or len(bars) <= 101:
                    log_event("signal_data_rejected", symbol=symbol, quality=quality.__dict__)
                    continue
                candle = bars[-1]
                candle_id = candle.time.isoformat()
                if state["last_processed_candle"].get(symbol) == candle_id:
                    continue
                decision = decision_snapshot(bars)
                signal = decision["signal"]
                position = positions.get(symbol)
                pending = symbol in state["pending_orders"]
                action = "none"
                if signal == "SELL" and position and not pending:
                    snapshot = snapshots[symbol]
                    bid = Decimal(str(snapshot.get("bid") or snapshot.get("price")))
                    cost_price = Decimal(str(position["cost_price"]))
                    _submit_order(
                        api,
                        account_id,
                        state,
                        symbol=symbol,
                        side="SELL",
                        quantity=Decimal(str(position["quantity"])),
                        event_time=candle.time,
                        reason="trend_exit",
                        estimated_loss=bid < cost_price,
                    )
                    action = "sell"
                elif (
                    signal == "BUY"
                    and not positions
                    and not state["pending_orders"]
                    and not state["paused"]
                    and state["daily_losses"] < MAX_DAILY_LOSSES
                    and len(entered_today) < MAX_DAILY_ENTRIES
                    and symbol not in entered_today
                ):
                    ask = Decimal(str(snapshots[symbol].get("ask") or snapshots[symbol].get("price")))
                    lot_size, min_quantity, min_amount = _instrument_rule(api, symbol)
                    quantity = quantity_for_notional(_buying_power(api, account_id) * ALLOCATION, ask, lot_size)
                    if quantity < min_quantity or quantity * ask < min_amount:
                        raise RuntimeError(f"Calculated {symbol} order is below the instrument minimum")
                    submission = _submit_order(
                        api,
                        account_id,
                        state,
                        symbol=symbol,
                        side="BUY",
                        quantity=quantity,
                        event_time=candle.time,
                        reason="m5_breakout",
                        reference_price=ask,
                    )
                    if submission.get("blocked"):
                        action = "blocked"
                    else:
                        entered_today.append(symbol)
                        write_state(state)
                        action = "buy"
                state["last_processed_candle"][symbol] = candle_id
                log_event(
                    "signal_evaluated",
                    symbol=symbol,
                    candle=candle_id,
                    signal=signal,
                    action=action,
                    paused=state["paused"],
                    daily_losses=state["daily_losses"],
                    decision=decision,
                )
            state["last_bar_check_bucket"] = bucket
        write_state(state)
        return {
            "status": "running",
            "symbols": symbols,
            "paused": state["paused"],
            "daily_losses": state["daily_losses"],
            "ends_at": state["ends_at"],
            "pending_orders": state["pending_orders"],
        }


def run_forever(api: WebullAPI, *, confirmed: bool) -> dict[str, Any]:
    if not confirmed:
        raise ValueError("Automatic Sandbox day trading requires --yes")
    ensure_sandbox()
    while True:
        try:
            result = run_once(api, confirmed=True)
            if result["status"] == "completed":
                return result
            time.sleep(ERROR_BACKOFF_SECONDS if result["status"] == "halted" else POLL_SECONDS)
        except KeyboardInterrupt:
            return {"status": "stopped"}
        except Exception as exc:
            log_event("cycle_error", error=type(exc).__name__, message=redact_secrets(exc))
            time.sleep(ERROR_BACKOFF_SECONDS)


def status() -> dict[str, Any]:
    state = read_state()
    try:
        report = load_report()
        deployment_symbols = list(report.get("deployment_symbols", []))
    except (FileNotFoundError, KeyError, json.JSONDecodeError):
        deployment_symbols = []
    arguments = []
    if LAUNCH_PLIST.exists():
        try:
            with LAUNCH_PLIST.open("rb") as handle:
                arguments = plistlib.load(handle).get("ProgramArguments", [])
        except (OSError, plistlib.InvalidFileException):
            arguments = []
    installed = "daytrade-run" in arguments
    runtime_status = "not_started"
    if state:
        runtime_status = "completed" if state["completed"] else ("halted" if state.get("halted") else "running")
    execution_authorization = authorization_status()
    return {
        "status": runtime_status,
        "decision": (
            "TRADE"
            if deployment_symbols and execution_authorization["new_entries_authorized"]
            else "NO_TRADE"
        ),
        "deployment_symbols": deployment_symbols,
        "mode": "HTTP snapshot polling every 5 seconds; M5 close signals",
        "state": state,
        "execution_authorization": execution_authorization,
        "launch_agent": {
            "label": LAUNCH_LABEL,
            "plist": str(LAUNCH_PLIST),
            "installed": installed,
            "occupied_by": None if installed or not LAUNCH_PLIST.exists() else "another runner",
        },
    }


def runtime_report() -> Path:
    state = read_state()
    events = []
    if LOG_FILE.exists():
        events = [json.loads(line) for line in LOG_FILE.read_text(encoding="utf-8").splitlines() if line]
    counts: dict[str, int] = {}
    for event in events:
        counts[event["event"]] = counts.get(event["event"], 0) + 1
    lines = [
        "# Webull Crypto Sandbox 日内交易实验",
        "",
        f"生成时间：{utc_now().isoformat()}",
        f"状态：{'未启动' if not state else ('已完成' if state['completed'] else '运行中')}",
        f"标的：{'、'.join((state or {}).get('symbols', [])) or '无'}",
        f"开始时间：{(state or {}).get('started_at') or '—'}",
        f"计划结束：{(state or {}).get('ends_at') or '—'}",
        f"暂停新开仓：{'是' if (state or {}).get('paused') else '否'}",
        f"安全停机：{'是' if (state or {}).get('halted') else '否'}",
        f"今日亏损次数：{(state or {}).get('daily_losses', 0)}",
        "",
        "## 事件统计",
        "",
    ]
    lines.extend(f"- {name}: {count}" for name, count in sorted(counts.items()))
    if not counts:
        lines.append("- 暂无事件")
    EXPERIMENT_REPORT.parent.mkdir(parents=True, exist_ok=True)
    EXPERIMENT_REPORT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return EXPERIMENT_REPORT


def _deploy_runtime() -> None:
    DEPLOY_DIR.mkdir(parents=True, exist_ok=True)
    for name in (
        "config.py",
        "crypto_runtime.py",
        "crypto_strategy.py",
        "daytrader_runtime.py",
        "daytrader_strategy.py",
        "execution_guard.py",
        "supertrend_strategy.py",
        "webull_api.py",
        "webull_cli.py",
        "webull_orders.py",
        "webull_streams.py",
        "requirements.txt",
        "runtime_environment.py",
    ):
        shutil.copy2(ROOT / name, DEPLOY_DIR / name)
    report_dir = DEPLOY_DIR / "reports"
    report_dir.mkdir(exist_ok=True)
    for name in (
        "crypto-backtest-90d.json",
        "crypto-backtest-90d.md",
        "daytrader-backtest-90d.json",
        "daytrader-backtest-90d.md",
        "sandbox-execution-authorization.json",
    ):
        source = ROOT / "reports" / name
        if source.exists():
            shutil.copy2(source, report_dir / name)
    python = ensure_runtime_venv(DEPLOY_VENV)
    subprocess.run(
        [str(python), "-m", "pip", "install", "-q", "-r", str(DEPLOY_DIR / "requirements.txt")],
        check=True,
    )


def install_launch_agent() -> Path:
    ensure_sandbox()
    state = initialize_state()
    if not state["symbols"]:
        raise RuntimeError("No validated symbols; LaunchAgent was not installed")
    _deploy_runtime()
    try:
        from crypto_runtime import read_state as read_old_state, write_state as write_old_state

        old_state = read_old_state()
        if old_state:
            old_state["paused"] = True
            write_old_state(old_state)
    except Exception:
        pass
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    LAUNCH_PLIST.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "Label": LAUNCH_LABEL,
        "ProgramArguments": [
            str(DEPLOY_VENV / "bin" / "python"),
            str(DEPLOY_DIR / "webull_cli.py"),
            "crypto-strategy",
            "daytrade-run",
            "--yes",
        ],
        "WorkingDirectory": str(DEPLOY_DIR),
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 10,
        "ProcessType": "Background",
        "StandardOutPath": str(STATE_DIR / "daytrader.out.log"),
        "StandardErrorPath": str(STATE_DIR / "daytrader.err.log"),
    }
    domain = f"gui/{os.getuid()}"
    subprocess.run(["launchctl", "bootout", domain, str(LAUNCH_PLIST)], capture_output=True)
    with LAUNCH_PLIST.open("wb") as handle:
        plistlib.dump(payload, handle)
    subprocess.run(["launchctl", "bootstrap", domain, str(LAUNCH_PLIST)], check=True)
    log_event("launch_agent_installed", plist=str(LAUNCH_PLIST))
    return LAUNCH_PLIST


def uninstall_launch_agent() -> None:
    domain = f"gui/{os.getuid()}"
    if LAUNCH_PLIST.exists():
        subprocess.run(["launchctl", "bootout", domain, str(LAUNCH_PLIST)], capture_output=True)
        LAUNCH_PLIST.unlink()
    log_event("launch_agent_uninstalled")

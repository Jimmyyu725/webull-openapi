from __future__ import annotations

import fcntl
import json
import os
import plistlib
import shutil
import subprocess
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterator, Optional

from config import API_ENDPOINT
from crypto_strategy import (
    ALLOCATION,
    STOP_LOSS,
    deterministic_order_id,
    load_report,
    quantity_for_notional,
    signals,
    webull_bars,
)
from execution_guard import authorization_status, authorize_automated_order
from runtime_environment import ensure_runtime_venv
from webull_api import WebullAPI, normalize_result
from webull_orders import build_order


ROOT = Path(__file__).parent
APP_SUPPORT_DIR = Path.home() / "Library" / "Application Support" / "WebullCryptoSandbox"
DEPLOY_DIR = APP_SUPPORT_DIR / "app"
DEPLOY_VENV = APP_SUPPORT_DIR / "venv"
STATE_DIR = APP_SUPPORT_DIR / "state"
STATE_FILE = STATE_DIR / "crypto_strategy.json"
LOG_FILE = STATE_DIR / "crypto_strategy.jsonl"
LOCK_FILE = STATE_DIR / "crypto_strategy.lock"
EXPERIMENT_REPORT = ROOT / "reports" / "crypto-experiment.md"
LAUNCH_LABEL = "com.jingtianyu.webull-crypto-sandbox"
LAUNCH_PLIST = Path.home() / "Library" / "LaunchAgents" / f"{LAUNCH_LABEL}.plist"
TERMINAL_ORDER_STATUSES = {"CANCELLED", "CANCELED", "FILLED", "FINAL_FILLED", "FAILED"}
STRATEGY_ID = "crypto-ema-ha-v1"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_datetime(value: Optional[str]) -> Optional[datetime]:
    return datetime.fromisoformat(value) if value else None


def ensure_sandbox() -> None:
    if API_ENDPOINT != "api.sandbox.webull.com":
        raise RuntimeError("Crypto strategy is locked to api.sandbox.webull.com")


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
            raise RuntimeError("Another crypto strategy run is still active") from exc
        yield


def initialize_state(now: Optional[datetime] = None) -> dict[str, Any]:
    existing = read_state()
    if existing:
        if "managed_positions" not in existing:
            existing["version"] = 2
            existing["managed_positions"] = {}
            write_state(existing)
            log_event("state_migrated", version=2, ownership_default="unmanaged")
        elif not isinstance(existing["managed_positions"], dict):
            raise RuntimeError("Managed position state is unreadable")
        return existing
    report = load_report()
    eligible = list(report.get("deployment_symbols", []))
    if not eligible:
        raise RuntimeError("No symbol passed the Webull M120 deployment gate")
    now = now or utc_now()
    state = {
        "version": 2,
        "started_at": now.isoformat(),
        "ends_at": (now + timedelta(days=30)).isoformat(),
        "eligible_symbols": eligible,
        "last_processed_candle": {},
        "pending_orders": {},
        "managed_positions": {},
        "submitted_order_ids": [],
        "consecutive_losses": 0,
        "cooldown_until": None,
        "paused": False,
        "completed": False,
    }
    write_state(state)
    log_event("experiment_started", symbols=eligible, ends_at=state["ends_at"])
    return state


def set_paused(paused: bool) -> dict[str, Any]:
    state = read_state()
    if not state:
        raise RuntimeError("The crypto experiment has not started")
    state["paused"] = paused
    write_state(state)
    log_event("paused" if paused else "resumed")
    return state


def _api_data(value: Any, description: str) -> Any:
    result = normalize_result(value)
    if not result.ok:
        raise RuntimeError(f"{description} failed with status {result.status}")
    return result.data


def _position_map(api: WebullAPI, account_id: str) -> dict[str, dict[str, Any]]:
    data = _api_data(api.trade.account_v2.get_account_position(account_id), "Position query")
    return {
        str(item.get("symbol")): item
        for item in data or []
        if Decimal(str(item.get("quantity", "0"))) > 0
    }


def _open_order_count(api: WebullAPI, account_id: str) -> int:
    data = _api_data(
        api.trade.order_v3.get_order_open(account_id, page_size=50),
        "Open order query",
    )
    if data is None:
        return 0
    if not isinstance(data, list):
        raise RuntimeError("Open order query returned unexpected data")
    return len(data)


def _buying_power(api: WebullAPI, account_id: str) -> Decimal:
    data = _api_data(api.trade.account_v2.get_account_balance(account_id), "Balance query")
    assets = data.get("account_currency_assets", []) if isinstance(data, dict) else []
    usd = next((item for item in assets if item.get("currency") == "USD"), None)
    if not usd:
        raise RuntimeError("USD buying power is unavailable")
    return Decimal(str(usd["buying_power"]))


def _managed_position_quantity(
    state: dict[str, Any], symbol: str
) -> Optional[Decimal]:
    try:
        return Decimal(str(state["managed_positions"][symbol]["quantity"]))
    except (ArithmeticError, KeyError, TypeError, ValueError):
        return None


def _snapshots(api: WebullAPI, symbols: list[str]) -> dict[str, dict[str, Any]]:
    data = _api_data(api.data.crypto_market_data.get_crypto_snapshot(symbols), "Snapshot query")
    return {str(item["symbol"]): item for item in data or []}


def _buy_market_state(
    snapshot: dict[str, Any], *, observed_at: Optional[datetime] = None
) -> dict[str, Optional[Decimal]]:
    def decimal_value(key: str) -> Optional[Decimal]:
        try:
            return Decimal(str(snapshot[key]))
        except (ArithmeticError, KeyError, TypeError, ValueError):
            return None

    quote_time = decimal_value("quote_time")
    quote_age_seconds = None
    if quote_time is not None and quote_time.is_finite():
        try:
            quoted_at = datetime.fromtimestamp(
                float(quote_time / Decimal("1000")), timezone.utc
            )
            quote_age_seconds = Decimal(
                str(((observed_at or utc_now()) - quoted_at).total_seconds())
            )
        except (OSError, OverflowError, ValueError):
            quote_age_seconds = None
    return {
        "current_best_bid": decimal_value("bid"),
        "current_best_ask": decimal_value("ask"),
        "current_ask_size": decimal_value("ask_size"),
        "quote_age_seconds": quote_age_seconds,
    }


def _instrument_rule(api: WebullAPI, symbol: str) -> tuple[Decimal, Decimal, Decimal]:
    data = _api_data(api.data.instrument.get_crypto_instrument([symbol]), "Instrument query")
    item = next((row for row in data or [] if row.get("symbol") == symbol), None)
    if not item:
        raise RuntimeError(f"Trading rules are unavailable for {symbol}")
    return (
        Decimal(str(item["lot_size"])),
        Decimal(str(item["min_trade_qty"])),
        Decimal(str(item["min_trade_amt"])),
    )


def _order_status(data: Any) -> str:
    if not isinstance(data, dict):
        return ""
    orders = data.get("orders")
    if isinstance(orders, list) and orders:
        data = orders[0]
    return str(data.get("order_status") or data.get("status") or "").upper()


def reconcile_pending(
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
            else:
                if position_quantity == 0:
                    state["managed_positions"].pop(symbol, None)
                else:
                    state["managed_positions"][symbol]["quantity"] = format(
                        position_quantity, "f"
                    )
                    state["managed_positions"][symbol]["last_exit_order_id"] = pending[
                        "order_id"
                    ]
                if pending.get("estimated_loss"):
                    state["consecutive_losses"] += 1
                else:
                    state["consecutive_losses"] = 0
                if state["consecutive_losses"] >= 3:
                    state["cooldown_until"] = (now + timedelta(hours=24)).isoformat()
                    log_event("cooldown_started", until=state["cooldown_until"])
            del state["pending_orders"][symbol]
            log_event("order_reconciled", symbol=symbol, side=side, order_id=pending["order_id"])
        elif status in TERMINAL_ORDER_STATUSES or age >= timedelta(minutes=10):
            del state["pending_orders"][symbol]
            log_event(
                "order_unfilled",
                symbol=symbol,
                side=side,
                order_id=pending["order_id"],
                status=status or "timeout",
            )


def _already_exists(api: WebullAPI, account_id: str, order_id: str) -> bool:
    try:
        result = normalize_result(api.trade.order_v3.get_order_detail(account_id, order_id))
    except Exception as exc:
        if getattr(exc, "http_status", None) == 404:
            return False
        raise RuntimeError("Unable to verify deterministic order ID; refusing to submit") from exc
    if result.ok:
        if isinstance(result.data, dict) and isinstance(result.data.get("orders"), list):
            return bool(result.data["orders"])
        return bool(result.data)
    if result.status == 404:
        return False
    raise RuntimeError(
        f"Unable to verify deterministic order ID (status {result.status}); refusing to submit"
    )


def submit_market_order(
    api: WebullAPI,
    account_id: str,
    state: dict[str, Any],
    *,
    symbol: str,
    side: str,
    quantity: Decimal,
    candle_time: datetime,
    reason: str,
    estimated_loss: bool = False,
    reference_price: Optional[Decimal] = None,
    market_state: Optional[dict[str, Optional[Decimal]]] = None,
) -> dict[str, Any]:
    current_position = _position_map(api, account_id).get(symbol)
    current_open_order_count = _open_order_count(api, account_id)
    capital = (
        {
            "current_buying_power": _buying_power(api, account_id),
            "order_reference_price": reference_price,
            **(market_state or {}),
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
    order_id = deterministic_order_id(symbol, candle_time, side)
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
    # Persist the deterministic ID before the network mutation. If the process
    # dies after Webull accepts the request, the next run will not submit it again.
    state["submitted_order_ids"].append(order_id)
    state["submitted_order_ids"] = state["submitted_order_ids"][-500:]
    write_state(state)
    try:
        result = normalize_result(api.trade.order_v3.place_order(account_id, [order]))
    except Exception as exc:
        log_event("order_uncertain", symbol=symbol, side=side, order_id=order_id)
        raise RuntimeError("Sandbox order outcome is uncertain; automatic retry was suppressed") from exc
    if not result.ok:
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


def _close_positions(
    api: WebullAPI,
    account_id: str,
    state: dict[str, Any],
    positions: dict[str, dict[str, Any]],
    now: datetime,
) -> None:
    for symbol in state["eligible_symbols"]:
        position = positions.get(symbol)
        if not position or symbol in state["pending_orders"]:
            continue
        submit_market_order(
            api,
            account_id,
            state,
            symbol=symbol,
            side="SELL",
            quantity=Decimal(str(position["quantity"])),
            candle_time=now.replace(second=0, microsecond=0),
            reason="experiment_end",
            estimated_loss=Decimal(str(position.get("unrealized_profit_loss", "0"))) < 0,
        )


def run_once(api: WebullAPI, *, confirmed: bool, now: Optional[datetime] = None) -> dict[str, Any]:
    if not confirmed:
        raise ValueError("Automatic Sandbox trading requires --yes")
    ensure_sandbox()
    now = now or utc_now()
    with strategy_lock():
        state = initialize_state(now)
        if state["completed"]:
            return {"status": "completed", "state": state}
        account_id = api.account_id("CRYPTO")
        positions = _position_map(api, account_id)
        reconcile_pending(api, account_id, state, positions, now)
        positions = _position_map(api, account_id)

        if now >= datetime.fromisoformat(state["ends_at"]):
            _close_positions(api, account_id, state, positions, now)
            positions = _position_map(api, account_id)
            if not positions and not state["pending_orders"]:
                state["completed"] = True
                log_event("experiment_completed")
            write_state(state)
            return {"status": "closing" if not state["completed"] else "completed", "state": state}

        symbols = list(state["eligible_symbols"])
        snapshots = _snapshots(api, symbols)
        quote_observed_at = utc_now()

        # Stop-loss checks run every minute, independently of the M120 signal cadence.
        for symbol, position in positions.items():
            if symbol not in symbols or symbol in state["pending_orders"]:
                continue
            snapshot = snapshots[symbol]
            executable_price = Decimal(str(snapshot.get("bid") or snapshot.get("price")))
            cost_price = Decimal(str(position["cost_price"]))
            if executable_price <= cost_price * (Decimal("1") - STOP_LOSS):
                submit_market_order(
                    api,
                    account_id,
                    state,
                    symbol=symbol,
                    side="SELL",
                    quantity=Decimal(str(position["quantity"])),
                    candle_time=now.replace(second=0, microsecond=0),
                    reason="stop_loss",
                    estimated_loss=True,
                )

        bars_by_symbol = webull_bars(api, symbols, timespan="M120", count=121, now=now, days=10)
        cooldown_until = _parse_datetime(state.get("cooldown_until"))
        cooldown_active = bool(cooldown_until and cooldown_until > now)
        if cooldown_until and cooldown_until <= now:
            state["cooldown_until"] = None
            state["consecutive_losses"] = 0
            cooldown_active = False
            log_event("cooldown_ended")
        buying_power: Optional[Decimal] = None

        for symbol in symbols:
            bars, quality = bars_by_symbol[symbol]
            if not quality.passed or len(bars) < 52:
                log_event("signal_data_rejected", symbol=symbol, quality=quality.__dict__)
                continue
            candle = bars[-1]
            candle_id = candle.time.isoformat()
            if state["last_processed_candle"].get(symbol) == candle_id:
                continue
            signal = signals(bars)[-1]
            position = positions.get(symbol)
            pending = symbol in state["pending_orders"]
            action = "none"
            if signal == "SELL" and position and not pending:
                submit_market_order(
                    api,
                    account_id,
                    state,
                    symbol=symbol,
                    side="SELL",
                    quantity=Decimal(str(position["quantity"])),
                    candle_time=candle.time,
                    reason="ema_exit",
                    estimated_loss=Decimal(str(position.get("unrealized_profit_loss", "0"))) < 0,
                )
                action = "sell"
            elif (
                signal == "BUY"
                and not position
                and not pending
                and not state["paused"]
                and not cooldown_active
            ):
                buying_power = buying_power or _buying_power(api, account_id)
                market_state = _buy_market_state(
                    snapshots[symbol], observed_at=quote_observed_at
                )
                ask = market_state["current_best_ask"]
                if ask is None or not ask.is_finite() or ask <= 0:
                    raise RuntimeError(f"Executable ask is unavailable for {symbol}")
                lot_size, min_quantity, min_amount = _instrument_rule(api, symbol)
                quantity = quantity_for_notional(buying_power * ALLOCATION, ask, lot_size)
                if quantity < min_quantity or quantity * ask < min_amount:
                    raise RuntimeError(f"Calculated {symbol} order is below the instrument minimum")
                submission = submit_market_order(
                    api,
                    account_id,
                    state,
                    symbol=symbol,
                    side="BUY",
                    quantity=quantity,
                    candle_time=candle.time,
                    reason="ema_entry",
                    reference_price=ask,
                    market_state=market_state,
                )
                action = "blocked" if submission.get("blocked") else "buy"
            state["last_processed_candle"][symbol] = candle_id
            log_event(
                "signal_evaluated",
                symbol=symbol,
                candle=candle_id,
                signal=signal,
                action=action,
                paused=state["paused"],
                cooldown=cooldown_active,
            )
        write_state(state)
        return {
            "status": "running",
            "eligible_symbols": symbols,
            "paused": state["paused"],
            "cooldown_until": state.get("cooldown_until"),
            "ends_at": state["ends_at"],
            "pending_orders": state["pending_orders"],
        }


def status() -> dict[str, Any]:
    state = read_state()
    report = load_report()
    deployment_symbols = list(report.get("deployment_symbols", []))
    execution_authorization = authorization_status()
    return {
        "status": "not_started" if not state else ("completed" if state["completed"] else "running"),
        "decision": (
            "TRADE"
            if deployment_symbols and execution_authorization["new_entries_authorized"]
            else "NO_TRADE"
        ),
        "deployment_symbols": deployment_symbols,
        "state": state,
        "execution_authorization": execution_authorization,
        "launch_agent": {
            "label": LAUNCH_LABEL,
            "plist": str(LAUNCH_PLIST),
            "installed": LAUNCH_PLIST.exists(),
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
        "# Webull Crypto Sandbox 30天实验",
        "",
        f"生成时间：{utc_now().isoformat()}",
        f"状态：{'未启动' if not state else ('已完成' if state['completed'] else '运行中')}",
        f"放行标的：{'、'.join((state or {}).get('eligible_symbols', [])) or '无'}",
        f"开始时间：{(state or {}).get('started_at') or '—'}",
        f"计划结束：{(state or {}).get('ends_at') or '—'}",
        f"暂停新开仓：{'是' if (state or {}).get('paused') else '否'}",
        f"连续亏损：{(state or {}).get('consecutive_losses', 0)}",
        f"冷却截止：{(state or {}).get('cooldown_until') or '—'}",
        "",
        "## 事件统计",
        "",
    ]
    if counts:
        lines.extend(f"- {name}: {count}" for name, count in sorted(counts.items()))
    else:
        lines.append("- 暂无事件")
    submitted = [event for event in events if event.get("event") == "order_submitted"]
    lines.extend([
        "",
        "## Sandbox订单",
        "",
        "| 时间 | 标的 | 方向 | 数量 | 原因 | 确定性订单ID |",
        "|---|---|---|---:|---|---|",
    ])
    if submitted:
        lines.extend(
            f"| {event.get('time', '—')} | {event.get('symbol', '—')} | {event.get('side', '—')} | "
            f"{event.get('quantity', '—')} | {event.get('reason', '—')} | {event.get('order_id', '—')} |"
            for event in submitted
        )
    else:
        lines.append("| — | — | — | — | 尚无订单 | — |")
    EXPERIMENT_REPORT.parent.mkdir(parents=True, exist_ok=True)
    EXPERIMENT_REPORT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return EXPERIMENT_REPORT


def _deploy_runtime() -> None:
    DEPLOY_DIR.mkdir(parents=True, exist_ok=True)
    source_files = (
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
    )
    for name in source_files:
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


def _migrate_legacy_state() -> None:
    legacy = ROOT / ".state"
    if STATE_FILE.exists() or not (legacy / "crypto_strategy.json").exists():
        return
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    for name in ("crypto_strategy.json", "crypto_strategy.jsonl"):
        source = legacy / name
        if source.exists():
            shutil.copy2(source, STATE_DIR / name)


def install_launch_agent() -> Path:
    ensure_sandbox()
    _deploy_runtime()
    _migrate_legacy_state()
    state = initialize_state()
    if not state["eligible_symbols"]:
        raise RuntimeError("No eligible symbol; LaunchAgent was not installed")
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    LAUNCH_PLIST.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "Label": LAUNCH_LABEL,
        "ProgramArguments": [
            str(DEPLOY_VENV / "bin" / "python"),
            str(DEPLOY_DIR / "webull_cli.py"),
            "crypto-strategy",
            "run-once",
            "--yes",
        ],
        "WorkingDirectory": str(DEPLOY_DIR),
        "RunAtLoad": True,
        "StartInterval": 60,
        "ProcessType": "Background",
        "StandardOutPath": str(STATE_DIR / "launchd.out.log"),
        "StandardErrorPath": str(STATE_DIR / "launchd.err.log"),
    }
    with LAUNCH_PLIST.open("wb") as handle:
        plistlib.dump(payload, handle)
    domain = f"gui/{os.getuid()}"
    subprocess.run(["launchctl", "bootout", domain, str(LAUNCH_PLIST)], capture_output=True)
    subprocess.run(["launchctl", "bootstrap", domain, str(LAUNCH_PLIST)], check=True)
    log_event("launch_agent_installed", plist=str(LAUNCH_PLIST))
    return LAUNCH_PLIST


def uninstall_launch_agent() -> None:
    domain = f"gui/{os.getuid()}"
    if LAUNCH_PLIST.exists():
        subprocess.run(["launchctl", "bootout", domain, str(LAUNCH_PLIST)], capture_output=True)
        LAUNCH_PLIST.unlink()
    log_event("launch_agent_uninstalled")

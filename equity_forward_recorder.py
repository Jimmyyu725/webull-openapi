from __future__ import annotations

import fcntl
import json
import os
import plistlib
import shutil
import sqlite3
import subprocess
import sys
from contextlib import closing, contextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from statistics import median
from typing import Any, Iterator, Optional
from zoneinfo import ZoneInfo

from config import API_ENDPOINT
from crypto_strategy import parse_time
from webull_api import WebullAPI, normalize_result


ROOT = Path(__file__).parent
SYMBOLS = ("SPY", "QQQ", "AAPL")
EASTERN = ZoneInfo("America/New_York")
APP_DIR = Path.home() / "Library" / "Application Support" / "WebullEquityForward"
DEPLOY_DIR = APP_DIR / "app"
DEPLOY_VENV = APP_DIR / "venv"
DATABASE = APP_DIR / "equity-forward.sqlite3"
LOCK_FILE = APP_DIR / "equity-forward.lock"
LAUNCH_LABEL = "com.jingtianyu.webull-equity-forward"
LAUNCH_PLIST = Path.home() / "Library" / "LaunchAgents" / f"{LAUNCH_LABEL}.plist"
TARGET_SESSIONS = 20
MIN_SAMPLES_PER_SESSION = 371
TCA_HORIZONS_MINUTES = (1, 5, 30)
TCA_OPERATIONAL_BUFFER_BPS = 2.0


SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
    symbol TEXT NOT NULL,
    bar_time TEXT NOT NULL,
    request_time TEXT NOT NULL,
    session_day TEXT NOT NULL,
    trading_session TEXT NOT NULL,
    open REAL NOT NULL,
    high REAL NOT NULL,
    low REAL NOT NULL,
    close REAL NOT NULL,
    volume REAL NOT NULL,
    last_price REAL,
    bid REAL,
    ask REAL,
    bid_size REAL,
    ask_size REAL,
    quote_time TEXT,
    mid REAL,
    spread_bps REAL,
    quote_age_seconds REAL,
    valid_bar INTEGER NOT NULL,
    valid_quote INTEGER NOT NULL,
    PRIMARY KEY (symbol, bar_time)
);
CREATE TABLE IF NOT EXISTS metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def _ensure_sandbox() -> None:
    if API_ENDPOINT != "api.sandbox.webull.com":
        raise RuntimeError("Equity forward recording is restricted to the Webull Sandbox endpoint")


@contextmanager
def _lock(lock_file: Path = LOCK_FILE) -> Iterator[None]:
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    with lock_file.open("w", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("Another equity forward-recording cycle is active") from exc
        yield


def _connect(database: Path = DATABASE, now: Optional[datetime] = None) -> sqlite3.Connection:
    database.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database)
    connection.executescript(SCHEMA)
    connection.execute(
        "INSERT OR IGNORE INTO metadata(key, value) VALUES ('started_at', ?)",
        ((now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat(),),
    )
    connection.execute(
        "UPDATE samples SET quote_age_seconds = ABS(quote_age_seconds) WHERE quote_age_seconds < 0"
    )
    connection.commit()
    return connection


def _is_regular_hours(now: datetime) -> bool:
    local = now.astimezone(EASTERN)
    return local.weekday() < 5 and time(9, 30) <= local.time() < time(16, 0)


def _latest_closed_bars(api: WebullAPI, now: datetime) -> dict[str, dict[str, Any]]:
    result = normalize_result(api.data.market_data.get_batch_history_bar(
        list(SYMBOLS), "US_STOCK", "M1", "5"
    ))
    if not result.ok or not isinstance(result.data, dict):
        raise RuntimeError(f"Unable to retrieve forward M1 bars: {result.status}")
    by_symbol = {
        str(item.get("symbol")): item.get("result", [])
        for item in result.data.get("result", [])
    }
    output: dict[str, dict[str, Any]] = {}
    for symbol in SYMBOLS:
        candidates = []
        for row in by_symbol.get(symbol, []):
            bar_time = parse_time(str(row["time"]))
            if row.get("trading_session") == "RTH" and bar_time + timedelta(minutes=1) <= now:
                candidates.append((bar_time, row))
        if candidates:
            bar_time, row = max(candidates, key=lambda item: item[0])
            if (
                bar_time.astimezone(EASTERN).date() == now.astimezone(EASTERN).date()
                and now - bar_time <= timedelta(minutes=3)
            ):
                output[symbol] = {**row, "parsed_time": bar_time}
    return output


def _snapshot_rows(api: WebullAPI) -> dict[str, dict[str, Any]]:
    result = normalize_result(api.data.market_data.get_snapshot(list(SYMBOLS), "US_STOCK"))
    if not result.ok or not isinstance(result.data, list):
        raise RuntimeError(f"Unable to retrieve forward snapshots: {result.status}")
    return {str(item.get("symbol")): item for item in result.data}


def _decimal(row: dict[str, Any], key: str) -> Optional[Decimal]:
    value = row.get(key)
    if value in (None, ""):
        return None
    return Decimal(str(value))


def _record(symbol: str, bar: dict[str, Any], quote: dict[str, Any], now: datetime) -> tuple[Any, ...]:
    bar_time = bar["parsed_time"]
    open_price = Decimal(str(bar["open"]))
    high = Decimal(str(bar["high"]))
    low = Decimal(str(bar["low"]))
    close = Decimal(str(bar["close"]))
    volume = Decimal(str(bar.get("volume", "0")))
    bid = _decimal(quote, "bid")
    ask = _decimal(quote, "ask")
    bid_size = _decimal(quote, "bid_size")
    ask_size = _decimal(quote, "ask_size")
    last_price = _decimal(quote, "price")
    quote_ms = quote.get("quote_time")
    quote_time = (
        datetime.fromtimestamp(int(quote_ms) / 1000, timezone.utc)
        if quote_ms not in (None, "")
        else None
    )
    valid_quote = bool(
        bid is not None
        and ask is not None
        and bid > 0
        and ask >= bid
        and bid_size is not None
        and ask_size is not None
        and bid_size >= 0
        and ask_size >= 0
        and quote_time is not None
    )
    mid = (bid + ask) / Decimal("2") if valid_quote else None
    spread_bps = (ask - bid) / mid * Decimal("10000") if mid else None
    quote_age = Decimal(str(abs((now - quote_time).total_seconds()))) if quote_time else None
    valid_bar = bool(
        open_price > 0
        and high > 0
        and low > 0
        and close > 0
        and low <= open_price <= high
        and low <= close <= high
        and volume >= 0
        and bar_time + timedelta(minutes=1) <= now
    )
    return (
        symbol,
        bar_time.isoformat(),
        now.isoformat(),
        bar_time.astimezone(EASTERN).date().isoformat(),
        str(bar.get("trading_session", "")),
        float(open_price),
        float(high),
        float(low),
        float(close),
        float(volume),
        float(last_price) if last_price is not None else None,
        float(bid) if bid is not None else None,
        float(ask) if ask is not None else None,
        float(bid_size) if bid_size is not None else None,
        float(ask_size) if ask_size is not None else None,
        quote_time.isoformat() if quote_time else None,
        float(mid) if mid is not None else None,
        float(spread_bps) if spread_bps is not None else None,
        float(quote_age) if quote_age is not None else None,
        int(valid_bar),
        int(valid_quote),
    )


INSERT_SAMPLE = """
INSERT OR IGNORE INTO samples (
    symbol, bar_time, request_time, session_day, trading_session,
    open, high, low, close, volume, last_price, bid, ask, bid_size,
    ask_size, quote_time, mid, spread_bps, quote_age_seconds,
    valid_bar, valid_quote
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def _complete_sessions(connection: sqlite3.Connection) -> list[str]:
    return [
        row[0]
        for row in connection.execute(
            """
            SELECT session_day
            FROM (
                SELECT session_day, bar_time
                FROM samples
                WHERE valid_bar = 1 AND symbol IN (?, ?, ?)
                GROUP BY session_day, bar_time
                HAVING COUNT(DISTINCT symbol) = ?
            )
            GROUP BY session_day
            HAVING COUNT(*) >= ?
            ORDER BY session_day
            """,
            (*SYMBOLS, len(SYMBOLS), MIN_SAMPLES_PER_SESSION),
        )
    ]


def session_coverage(database: Path = DATABASE) -> dict[str, Any]:
    """Report aligned valid RTH minutes without backfilling or creating orders."""
    output: dict[str, Any] = {
        "status": "not_started",
        "symbols": list(SYMBOLS),
        "expected_regular_minutes": 390,
        "protocol_minimum_aligned_minutes": MIN_SAMPLES_PER_SESSION,
        "complete_session_count": 0,
        "sessions": [],
        "database": str(database),
        "orders_enabled": False,
    }
    if not database.exists():
        return output

    with closing(sqlite3.connect(database)) as connection:
        rows = connection.execute(
            """
            SELECT symbol, session_day, bar_time, valid_bar, valid_quote
            FROM samples
            WHERE symbol IN (?, ?, ?)
            ORDER BY session_day, bar_time, symbol
            """,
            SYMBOLS,
        ).fetchall()

    grouped: dict[str, dict[str, dict[str, set[datetime]]]] = {}
    for symbol, session_day, bar_time, valid_bar, valid_quote in rows:
        values = grouped.setdefault(
            session_day,
            {
                item: {"observed": set(), "valid_bar": set(), "valid_quote": set()}
                for item in SYMBOLS
            },
        )[symbol]
        timestamp = parse_time(bar_time).astimezone(timezone.utc)
        values["observed"].add(timestamp)
        if valid_bar:
            values["valid_bar"].add(timestamp)
        if valid_quote:
            values["valid_quote"].add(timestamp)

    sessions: list[dict[str, Any]] = []
    for session_day in sorted(grouped):
        day = date.fromisoformat(session_day)
        start = datetime(day.year, day.month, day.day, 9, 30, tzinfo=EASTERN)
        expected = {
            (start + timedelta(minutes=index)).astimezone(timezone.utc)
            for index in range(390)
        }
        symbol_output: dict[str, Any] = {}
        aligned = set(expected)
        for symbol in SYMBOLS:
            values = grouped[session_day][symbol]
            observed = values["observed"] & expected
            valid_bars = values["valid_bar"] & expected
            valid_quotes = values["valid_quote"] & expected
            missing = sorted(expected - valid_bars)
            aligned &= valid_bars
            symbol_output[symbol] = {
                "observed_minutes": len(observed),
                "valid_bar_minutes": len(valid_bars),
                "valid_quote_minutes": len(valid_quotes),
                "first_bar": min(observed).isoformat() if observed else None,
                "last_bar": max(observed).isoformat() if observed else None,
                "missing_expected_minutes": len(missing),
                "missing_examples_et": [
                    value.astimezone(EASTERN).strftime("%H:%M") for value in missing[:10]
                ],
            }
        aligned_missing = sorted(expected - aligned)
        sessions.append({
            "session_day": session_day,
            "complete": len(aligned) >= MIN_SAMPLES_PER_SESSION,
            "aligned_valid_minutes": len(aligned),
            "aligned_missing_minutes": len(aligned_missing),
            "aligned_coverage": round(len(aligned) / len(expected), 6),
            "aligned_missing_examples_et": [
                value.astimezone(EASTERN).strftime("%H:%M")
                for value in aligned_missing[:10]
            ],
            "symbols": symbol_output,
        })

    output.update({
        "status": "recording" if sessions else "not_started",
        "complete_session_count": sum(item["complete"] for item in sessions),
        "sessions": sessions,
    })
    return output


def _percentile(values: list[float], fraction: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int(len(ordered) * fraction + 0.999999) - 1))
    return round(ordered[index], 6)


def _median(values: list[float]) -> Optional[float]:
    return round(float(median(values)), 6) if values else None


def status(database: Path = DATABASE) -> dict[str, Any]:
    if not database.exists():
        return {
            "status": "not_started",
            "decision": "PENDING",
            "symbols": list(SYMBOLS),
            "target_complete_sessions": TARGET_SESSIONS,
            "database": str(database),
            "installed": LAUNCH_PLIST.exists(),
            "launch_label": LAUNCH_LABEL,
            "orders_enabled": False,
        }
    with closing(_connect(database)) as connection:
        complete = _complete_sessions(connection)
        symbols = {}
        for symbol in SYMBOLS:
            summary = connection.execute(
                """
                SELECT COUNT(*), MIN(bar_time), MAX(bar_time), COUNT(DISTINCT session_day),
                       AVG(valid_bar), AVG(valid_quote)
                FROM samples WHERE symbol = ?
                """,
                (symbol,),
            ).fetchone()
            spreads = [
                row[0] for row in connection.execute(
                    "SELECT spread_bps FROM samples WHERE symbol = ? AND valid_quote = 1",
                    (symbol,),
                ) if row[0] is not None
            ]
            ages = [
                row[0] for row in connection.execute(
                    "SELECT quote_age_seconds FROM samples WHERE symbol = ? AND valid_quote = 1",
                    (symbol,),
                ) if row[0] is not None
            ]
            p95_spread = _percentile(spreads, 0.95)
            p95_age = _percentile(ages, 0.95)
            checks = {
                "complete_sessions": len(complete) >= TARGET_SESSIONS,
                "valid_bar_coverage": (summary[4] or 0.0) == 1.0,
                "valid_quote_coverage": (summary[5] or 0.0) >= 0.99,
                "p95_quote_age": p95_age is not None and p95_age <= 5.0,
                "p95_spread": p95_spread is not None and p95_spread <= 5.0,
            }
            symbols[symbol] = {
                "samples": summary[0],
                "first_bar": summary[1],
                "last_bar": summary[2],
                "observed_sessions": summary[3],
                "valid_bar_coverage": round(summary[4] or 0.0, 6),
                "valid_quote_coverage": round(summary[5] or 0.0, 6),
                "p95_spread_bps": p95_spread,
                "p95_quote_age_seconds": p95_age,
                "quality_checks": checks,
                "quality_passed": all(checks.values()),
            }
        started = connection.execute(
            "SELECT value FROM metadata WHERE key = 'started_at'"
        ).fetchone()
    complete_run = len(complete) >= TARGET_SESSIONS
    all_quality_passed = complete_run and all(item["quality_passed"] for item in symbols.values())
    return {
        "status": "completed" if complete_run else "recording",
        "decision": "DATA_USABLE" if all_quality_passed else ("DATA_REJECTED" if complete_run else "PENDING"),
        "started_at": started[0] if started else None,
        "symbols": symbols,
        "complete_sessions": complete,
        "complete_session_count": len(complete),
        "target_complete_sessions": TARGET_SESSIONS,
        "database": str(database),
        "installed": LAUNCH_PLIST.exists(),
        "launch_label": LAUNCH_LABEL,
        "orders_enabled": False,
    }


def execution_diagnostics(database: Path = DATABASE) -> dict[str, Any]:
    """Estimate top-of-book cost hurdles without creating signals or orders."""
    recorder = status(database)
    output: dict[str, Any] = {
        "status": "ready" if recorder["decision"] == "DATA_USABLE" else "collecting",
        "decision": recorder["decision"],
        "interpretation": "EXECUTION_DIAGNOSTIC_ONLY",
        "complete_session_count": recorder.get("complete_session_count", 0),
        "target_complete_sessions": TARGET_SESSIONS,
        "horizons_minutes": list(TCA_HORIZONS_MINUTES),
        "operational_buffer_bps": TCA_OPERATIONAL_BUFFER_BPS,
        "symbols": {},
        "orders_enabled": False,
        "limitations": [
            "Top-of-book quotes are a lower-bound cost proxy, not actual fills.",
            "Overlapping minute observations are not independent strategy trades.",
            "Results do not measure market impact, fill probability, or price improvement.",
        ],
    }
    if not database.exists():
        return output

    with closing(_connect(database)) as connection:
        for symbol in SYMBOLS:
            rows = connection.execute(
                """
                SELECT session_day, bar_time, bid, ask, mid
                FROM samples
                WHERE symbol = ? AND valid_bar = 1 AND valid_quote = 1
                ORDER BY bar_time
                """,
                (symbol,),
            ).fetchall()
            samples = {
                (row[0], parse_time(row[1])): {
                    "bid": Decimal(str(row[2])),
                    "ask": Decimal(str(row[3])),
                    "mid": Decimal(str(row[4])),
                }
                for row in rows
                if None not in row[2:5]
            }
            horizons: dict[str, Any] = {}
            for minutes in TCA_HORIZONS_MINUTES:
                entry_half_spreads: list[float] = []
                round_trip_costs: list[float] = []
                absolute_mid_moves: list[float] = []
                long_returns: list[float] = []
                short_returns: list[float] = []
                for (session_day, bar_time), current in samples.items():
                    future = samples.get((session_day, bar_time + timedelta(minutes=minutes)))
                    if future is None:
                        continue
                    entry_half = (
                        (current["ask"] - current["mid"]) / current["mid"] * Decimal("10000")
                    )
                    exit_half = (
                        (future["mid"] - future["bid"]) / future["mid"] * Decimal("10000")
                    )
                    mid_move = (
                        (future["mid"] / current["mid"] - Decimal("1")) * Decimal("10000")
                    )
                    long_return = (
                        (future["bid"] - current["ask"]) / current["ask"] * Decimal("10000")
                    )
                    short_return = (
                        (current["bid"] - future["ask"]) / current["bid"] * Decimal("10000")
                    )
                    entry_half_spreads.append(float(entry_half))
                    round_trip_costs.append(float(entry_half + exit_half))
                    absolute_mid_moves.append(float(abs(mid_move)))
                    long_returns.append(float(long_return))
                    short_returns.append(float(short_return))
                p95_cost = _percentile(round_trip_costs, 0.95)
                horizons[str(minutes)] = {
                    "paired_observations": len(round_trip_costs),
                    "median_entry_half_spread_bps": _median(entry_half_spreads),
                    "median_round_trip_quoted_cost_bps": _median(round_trip_costs),
                    "p95_round_trip_quoted_cost_bps": p95_cost,
                    "median_absolute_mid_move_bps": _median(absolute_mid_moves),
                    "median_long_executable_return_bps": _median(long_returns),
                    "median_short_executable_return_bps": _median(short_returns),
                    "minimum_required_gross_edge_bps": (
                        round(p95_cost + TCA_OPERATIONAL_BUFFER_BPS, 6)
                        if p95_cost is not None
                        else None
                    ),
                }
            output["symbols"][symbol] = {"horizons": horizons}
    return output


def record_once(
    api: WebullAPI,
    *,
    now: Optional[datetime] = None,
    database: Path = DATABASE,
    lock_file: Path = LOCK_FILE,
) -> dict[str, Any]:
    _ensure_sandbox()
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    with _lock(lock_file), closing(_connect(database, now)) as connection:
        complete = _complete_sessions(connection)
        if len(complete) >= TARGET_SESSIONS:
            return {"outcome": "complete", "recorded": 0, **status(database)}
        if not _is_regular_hours(now):
            return {"outcome": "outside_regular_hours", "recorded": 0, **status(database)}
        bars = _latest_closed_bars(api, now)
        if set(bars) != set(SYMBOLS):
            return {"outcome": "no_current_closed_bar", "recorded": 0, **status(database)}
        quotes = _snapshot_rows(api)
        if set(quotes) != set(SYMBOLS):
            raise RuntimeError("Webull snapshot response omitted a forward-recording symbol")
        recorded = 0
        for symbol in SYMBOLS:
            cursor = connection.execute(INSERT_SAMPLE, _record(symbol, bars[symbol], quotes[symbol], now))
            recorded += cursor.rowcount
        connection.commit()
    return {"outcome": "recorded" if recorded else "duplicate_bar", "recorded": recorded, **status(database)}


def _launch_payload() -> dict[str, Any]:
    return {
        "Label": LAUNCH_LABEL,
        "ProgramArguments": [
            str(DEPLOY_VENV / "bin" / "python"),
            str(DEPLOY_DIR / "webull_cli.py"),
            "equity-strategy",
            "forward-record-once",
        ],
        "WorkingDirectory": str(DEPLOY_DIR),
        "RunAtLoad": True,
        "StartInterval": 60,
        "ProcessType": "Background",
        "StandardOutPath": str(APP_DIR / "launchd.out.log"),
        "StandardErrorPath": str(APP_DIR / "launchd.err.log"),
    }


def _deploy_runtime() -> None:
    DEPLOY_DIR.mkdir(parents=True, exist_ok=True)
    for name in (
        "config.py",
        "crypto_strategy.py",
        "equity_desk.py",
        "equity_forward_recorder.py",
        "execution_guard.py",
        "webull_api.py",
        "webull_cli.py",
        "webull_orders.py",
        "webull_streams.py",
        "requirements.txt",
    ):
        shutil.copy2(ROOT / name, DEPLOY_DIR / name)
    deployed_reports = DEPLOY_DIR / "reports"
    deployed_reports.mkdir(parents=True, exist_ok=True)
    shutil.copy2(
        ROOT / "reports" / "research-attempt-ledger.json",
        deployed_reports / "research-attempt-ledger.json",
    )
    shutil.copy2(
        ROOT / "reports" / "sandbox-execution-authorization.json",
        deployed_reports / "sandbox-execution-authorization.json",
    )
    python = DEPLOY_VENV / "bin" / "python"
    if not python.exists():
        subprocess.run([sys.executable, "-m", "venv", str(DEPLOY_VENV)], check=True)
        subprocess.run(
            [str(python), "-m", "pip", "install", "-q", "-r", str(DEPLOY_DIR / "requirements.txt")],
            check=True,
        )


def install_launch_agent() -> Path:
    _ensure_sandbox()
    APP_DIR.mkdir(parents=True, exist_ok=True)
    _deploy_runtime()
    LAUNCH_PLIST.parent.mkdir(parents=True, exist_ok=True)
    domain = f"gui/{os.getuid()}"
    subprocess.run(["launchctl", "bootout", domain, str(LAUNCH_PLIST)], capture_output=True)
    with LAUNCH_PLIST.open("wb") as handle:
        plistlib.dump(_launch_payload(), handle)
    subprocess.run(["launchctl", "bootstrap", domain, str(LAUNCH_PLIST)], check=True)
    return LAUNCH_PLIST


def uninstall_launch_agent() -> None:
    domain = f"gui/{os.getuid()}"
    if LAUNCH_PLIST.exists():
        subprocess.run(["launchctl", "bootout", domain, str(LAUNCH_PLIST)], capture_output=True)
        LAUNCH_PLIST.unlink()

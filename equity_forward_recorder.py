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
MANDATORY_ANCHOR_MINUTES = 30
MAX_CONSECUTIVE_INTERNAL_GAP_MINUTES = 1
TCA_HORIZONS_MINUTES = (1, 5, 30)
TCA_OPERATIONAL_BUFFER_BPS = 2.0
MAX_P95_BAR_CLOSE_LAG_SECONDS = 30.0
MAX_CROSS_SYMBOL_CAPTURE_SKEW_SECONDS = 0.0
CAPTURE_PROTOCOL_VERSION = "2026-08-29-response-time-v1"
LEGACY_CAPTURE_PROTOCOL_VERSION = "LEGACY_UNVERSIONED"
NYSE_CALENDAR_SOURCE = "https://www.nyse.com/trade/hours-calendars"
NYSE_CALENDAR_VERSION = "NYSE-2026-2028-verified-2026-08-29"
NYSE_CALENDAR_SUPPORTED_YEARS = frozenset({2026, 2027, 2028})
NYSE_CLOSED_DAYS = frozenset(
    date.fromisoformat(value)
    for value in (
        "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03",
        "2026-05-25", "2026-06-19", "2026-07-03", "2026-09-07",
        "2026-11-26", "2026-12-25",
        "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26",
        "2027-05-31", "2027-06-18", "2027-07-05", "2027-09-06",
        "2027-11-25", "2027-12-24",
        "2028-01-17", "2028-02-21", "2028-04-14", "2028-05-29",
        "2028-06-19", "2028-07-04", "2028-09-04", "2028-11-23",
        "2028-12-25",
    )
)
NYSE_EARLY_CLOSE_DAYS = frozenset(
    date.fromisoformat(value)
    for value in (
        "2026-11-27", "2026-12-24", "2027-11-26",
        "2028-07-03", "2028-11-24",
    )
)


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
    capture_protocol_version TEXT,
    PRIMARY KEY (symbol, bar_time)
);
CREATE TABLE IF NOT EXISTS metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS capture_attempts (
    attempt_id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    completed_at TEXT NOT NULL,
    session_day TEXT NOT NULL,
    session_type TEXT NOT NULL,
    stage TEXT NOT NULL,
    outcome TEXT NOT NULL,
    bar_symbol_count INTEGER NOT NULL,
    quote_symbol_count INTEGER NOT NULL,
    recorded_rows INTEGER NOT NULL,
    error_type TEXT
);
"""


def _ensure_sandbox() -> None:
    if API_ENDPOINT != "api.sandbox.webull.com":
        raise RuntimeError("Equity forward recording is restricted to the Webull Sandbox endpoint")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


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
    columns = {
        row[1] for row in connection.execute("PRAGMA table_info(samples)").fetchall()
    }
    if "capture_protocol_version" not in columns:
        connection.execute(
            "ALTER TABLE samples ADD COLUMN capture_protocol_version TEXT"
        )
    connection.execute(
        "INSERT OR IGNORE INTO metadata(key, value) VALUES ('started_at', ?)",
        ((now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat(),),
    )
    connection.commit()
    return connection


def _read_connection(database: Path = DATABASE) -> sqlite3.Connection:
    return sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True)


def _session_schedule(day: date) -> tuple[str, Optional[time]]:
    if day.year not in NYSE_CALENDAR_SUPPORTED_YEARS:
        return "UNSUPPORTED", None
    if day.weekday() >= 5 or day in NYSE_CLOSED_DAYS:
        return "CLOSED", None
    if day in NYSE_EARLY_CLOSE_DAYS:
        return "EARLY_CLOSE", time(13, 0)
    return "FULL", time(16, 0)


def _is_regular_hours(now: datetime) -> bool:
    local = now.astimezone(EASTERN)
    _, close = _session_schedule(local.date())
    return close is not None and time(9, 30) <= local.time() < close


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


def _stored_quote_age(request_time: str, quote_time: Optional[str]) -> Optional[float]:
    if not quote_time:
        return None
    return (
        parse_time(request_time).astimezone(timezone.utc)
        - parse_time(quote_time).astimezone(timezone.utc)
    ).total_seconds()


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
    quote_age = Decimal(str((now - quote_time).total_seconds())) if quote_time else None
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
        and quote_age is not None
        and quote_age >= 0
    )
    mid = (bid + ask) / Decimal("2") if valid_quote else None
    spread_bps = (ask - bid) / mid * Decimal("10000") if mid else None
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
        CAPTURE_PROTOCOL_VERSION,
    )


INSERT_SAMPLE = """
INSERT OR IGNORE INTO samples (
    symbol, bar_time, request_time, session_day, trading_session,
    open, high, low, close, volume, last_price, bid, ask, bid_size,
    ask_size, quote_time, mid, spread_bps, quote_age_seconds,
    valid_bar, valid_quote, capture_protocol_version
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


INSERT_CAPTURE_ATTEMPT = """
INSERT INTO capture_attempts (
    started_at, completed_at, session_day, session_type, stage, outcome,
    bar_symbol_count, quote_symbol_count, recorded_rows, error_type
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def _record_attempt(
    connection: sqlite3.Connection,
    *,
    started_at: datetime,
    completed_at: datetime,
    session_type: str,
    stage: str,
    outcome: str,
    bar_symbol_count: int = 0,
    quote_symbol_count: int = 0,
    recorded_rows: int = 0,
    error_type: Optional[str] = None,
) -> None:
    connection.execute(
        INSERT_CAPTURE_ATTEMPT,
        (
            started_at.isoformat(),
            completed_at.isoformat(),
            started_at.astimezone(EASTERN).date().isoformat(),
            session_type,
            stage,
            outcome,
            bar_symbol_count,
            quote_symbol_count,
            recorded_rows,
            error_type,
        ),
    )
    connection.commit()


def _capture_attempt_summary(connection: sqlite3.Connection) -> dict[str, Any]:
    available = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'capture_attempts'"
    ).fetchone()
    if not available:
        return {
            "status": "not_available",
            "total_attempts": 0,
            "counts_by_outcome": {},
        }
    counts = {
        str(outcome): int(count)
        for outcome, count in connection.execute(
            "SELECT outcome, COUNT(*) FROM capture_attempts GROUP BY outcome ORDER BY outcome"
        )
    }
    first = connection.execute(
        "SELECT started_at FROM capture_attempts ORDER BY attempt_id LIMIT 1"
    ).fetchone()
    latest = connection.execute(
        """
        SELECT started_at, completed_at, stage, outcome, error_type
        FROM capture_attempts ORDER BY attempt_id DESC LIMIT 1
        """
    ).fetchone()
    return {
        "status": "available",
        "total_attempts": sum(counts.values()),
        "first_started_at": first[0] if first else None,
        "latest_started_at": latest[0] if latest else None,
        "latest_completed_at": latest[1] if latest else None,
        "latest_stage": latest[2] if latest else None,
        "latest_outcome": latest[3] if latest else None,
        "latest_error_type": latest[4] if latest else None,
        "counts_by_outcome": counts,
        "api_error_attempts": sum(
            counts.get(item, 0) for item in ("BAR_API_ERROR", "SNAPSHOT_API_ERROR")
        ),
        "partial_response_attempts": sum(
            counts.get(item, 0)
            for item in ("PARTIAL_BAR_RESPONSE", "PARTIAL_SNAPSHOT_RESPONSE")
        ),
        "no_current_bar_attempts": counts.get("NO_CURRENT_CLOSED_BAR", 0),
        "partial_record_attempts": counts.get("PARTIAL_RECORD", 0),
    }


def session_coverage(database: Path = DATABASE) -> dict[str, Any]:
    """Report aligned valid RTH minutes without backfilling or creating orders."""
    output: dict[str, Any] = {
        "status": "not_started",
        "symbols": list(SYMBOLS),
        "expected_regular_minutes": 390,
        "expected_early_close_minutes": 210,
        "nyse_calendar_source": NYSE_CALENDAR_SOURCE,
        "nyse_calendar_version": NYSE_CALENDAR_VERSION,
        "nyse_calendar_supported_years": sorted(NYSE_CALENDAR_SUPPORTED_YEARS),
        "protocol_minimum_aligned_minutes": MIN_SAMPLES_PER_SESSION,
        "mandatory_anchor_minutes": MANDATORY_ANCHOR_MINUTES,
        "max_consecutive_internal_gap_minutes": MAX_CONSECUTIVE_INTERNAL_GAP_MINUTES,
        "max_p95_bar_close_lag_seconds": MAX_P95_BAR_CLOSE_LAG_SECONDS,
        "max_cross_symbol_capture_skew_seconds": MAX_CROSS_SYMBOL_CAPTURE_SKEW_SECONDS,
        "required_capture_protocol_version": CAPTURE_PROTOCOL_VERSION,
        "capture_attempt_audit_available": False,
        "capture_attempt_count": 0,
        "complete_session_count": 0,
        "qualified_session_count": 0,
        "target_qualified_sessions": TARGET_SESSIONS,
        "complete_sessions": [],
        "qualified_sessions": [],
        "rejected_complete_sessions": [],
        "excluded_early_close_sessions": [],
        "sessions": [],
        "database": str(database),
        "orders_enabled": False,
    }
    if not database.exists():
        return output

    with closing(_read_connection(database)) as connection:
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(samples)").fetchall()
        }
        capture_protocol_select = (
            "capture_protocol_version"
            if "capture_protocol_version" in columns
            else "NULL AS capture_protocol_version"
        )
        rows = connection.execute(
            f"""
            SELECT symbol, session_day, bar_time, request_time, valid_bar, valid_quote,
                   quote_time, spread_bps, {capture_protocol_select}
            FROM samples
            WHERE symbol IN (?, ?, ?)
            ORDER BY session_day, bar_time, symbol
            """,
            SYMBOLS,
        ).fetchall()
        attempt_summary = _capture_attempt_summary(connection)
        attempt_rows = (
            connection.execute(
                """
                SELECT session_day, outcome, COUNT(*)
                FROM capture_attempts
                GROUP BY session_day, outcome
                ORDER BY session_day, outcome
                """
            ).fetchall()
            if attempt_summary["status"] == "available"
            else []
        )

    grouped: dict[str, dict[str, dict[str, Any]]] = {}
    for (
        symbol,
        session_day,
        bar_time,
        request_time,
        valid_bar,
        valid_quote,
        quote_time,
        spread_bps,
        capture_protocol_version,
    ) in rows:
        values = grouped.setdefault(
            session_day,
            {
                item: {
                    "observed": set(),
                    "valid_bar": set(),
                    "valid_quote": set(),
                    "bar_close_lags": {},
                    "request_times": {},
                    "quote_ages": {},
                    "spreads": {},
                    "capture_protocol_versions": set(),
                }
                for item in SYMBOLS
            },
        )[symbol]
        timestamp = parse_time(bar_time).astimezone(timezone.utc)
        request_timestamp = parse_time(request_time).astimezone(timezone.utc)
        quote_age_seconds = _stored_quote_age(request_time, quote_time)
        values["observed"].add(timestamp)
        values["capture_protocol_versions"].add(
            capture_protocol_version or LEGACY_CAPTURE_PROTOCOL_VERSION
        )
        values["bar_close_lags"][timestamp] = (
            request_timestamp - timestamp - timedelta(minutes=1)
        ).total_seconds()
        values["request_times"][timestamp] = request_timestamp
        if valid_bar:
            values["valid_bar"].add(timestamp)
        if quote_age_seconds is not None:
            values["quote_ages"][timestamp] = float(quote_age_seconds)
        if valid_quote and quote_age_seconds is not None and quote_age_seconds >= 0:
            values["valid_quote"].add(timestamp)
            if spread_bps is not None:
                values["spreads"][timestamp] = float(spread_bps)

    attempts_by_session: dict[str, dict[str, int]] = {}
    for session_day, outcome, count in attempt_rows:
        attempts_by_session.setdefault(str(session_day), {})[str(outcome)] = int(count)
        grouped.setdefault(
            str(session_day),
            {
                item: {
                    "observed": set(),
                    "valid_bar": set(),
                    "valid_quote": set(),
                    "bar_close_lags": {},
                    "request_times": {},
                    "quote_ages": {},
                    "spreads": {},
                    "capture_protocol_versions": set(),
                }
                for item in SYMBOLS
            },
        )

    sessions: list[dict[str, Any]] = []
    for session_day in sorted(grouped):
        day = date.fromisoformat(session_day)
        session_type, scheduled_close = _session_schedule(day)
        scheduled_minutes = (
            scheduled_close.hour * 60 + scheduled_close.minute - (9 * 60 + 30)
            if scheduled_close
            else 0
        )
        eligible_full_session = session_type == "FULL"
        minimum_aligned_minutes = (
            min(MIN_SAMPLES_PER_SESSION, scheduled_minutes)
            if scheduled_minutes
            else MIN_SAMPLES_PER_SESSION
        )
        start = datetime(day.year, day.month, day.day, 9, 30, tzinfo=EASTERN)
        expected = {
            (start + timedelta(minutes=index)).astimezone(timezone.utc)
            for index in range(scheduled_minutes)
        }
        symbol_output: dict[str, Any] = {}
        aligned = set(expected)
        session_lags: list[float] = []
        session_capture_protocol_versions: set[str] = set()
        for symbol in SYMBOLS:
            values = grouped[session_day][symbol]
            observed = values["observed"] & expected
            valid_bars = values["valid_bar"] & expected
            valid_quotes = values["valid_quote"] & expected
            valid_quote_bars = valid_quotes & valid_bars
            lags = [values["bar_close_lags"][value] for value in valid_bars]
            quote_ages = [
                values["quote_ages"][value]
                for value in valid_quote_bars
                if value in values["quote_ages"]
            ]
            all_quote_ages = [
                values["quote_ages"][value]
                for value in valid_bars
                if value in values["quote_ages"]
            ]
            spreads = [
                values["spreads"][value]
                for value in valid_quote_bars
                if value in values["spreads"]
            ]
            session_lags.extend(lags)
            p95_lag = _percentile(lags, 0.95)
            negative_lags = sum(value < 0 for value in lags)
            valid_bar_coverage = len(valid_bars) / len(observed) if observed else 0.0
            valid_quote_coverage = (
                len(valid_quote_bars) / len(valid_bars) if valid_bars else 0.0
            )
            p95_quote_age = _percentile(quote_ages, 0.95)
            p95_spread = _percentile(spreads, 0.95)
            negative_quote_ages = sum(value < 0 for value in all_quote_ages)
            capture_protocol_versions = values["capture_protocol_versions"]
            session_capture_protocol_versions.update(capture_protocol_versions)
            checks = {
                "valid_bar_coverage": valid_bar_coverage == 1.0,
                "valid_quote_coverage": valid_quote_coverage >= 0.99,
                "quote_time_order": negative_quote_ages == 0,
                "p95_quote_age": p95_quote_age is not None and p95_quote_age <= 5.0,
                "p95_spread": p95_spread is not None and p95_spread <= 5.0,
                "bar_close_time_order": negative_lags == 0,
                "p95_bar_close_lag": (
                    p95_lag is not None
                    and p95_lag <= MAX_P95_BAR_CLOSE_LAG_SECONDS
                ),
            }
            missing = sorted(expected - valid_bars)
            aligned &= valid_bars
            symbol_output[symbol] = {
                "observed_minutes": len(observed),
                "valid_bar_minutes": len(valid_bars),
                "valid_quote_minutes": len(valid_quote_bars),
                "valid_bar_coverage": round(valid_bar_coverage, 6),
                "valid_quote_coverage": round(valid_quote_coverage, 6),
                "p95_quote_age_seconds": p95_quote_age,
                "negative_quote_age_count": negative_quote_ages,
                "quote_timeliness": "PASS" if negative_quote_ages == 0 else "FAIL",
                "p95_spread_bps": p95_spread,
                "bar_timeliness": (
                    "PASS"
                    if negative_lags == 0
                    and p95_lag is not None
                    and p95_lag <= MAX_P95_BAR_CLOSE_LAG_SECONDS
                    else "FAIL"
                ),
                "p95_bar_close_lag_seconds": p95_lag,
                "negative_bar_close_lag_count": negative_lags,
                "capture_protocol_versions": sorted(capture_protocol_versions),
                "quality_checks": checks,
                "quality_failures": [
                    check for check, passed in checks.items() if not passed
                ],
                "quality_passed": all(checks.values()),
                "first_bar": min(observed).isoformat() if observed else None,
                "last_bar": max(observed).isoformat() if observed else None,
                "missing_expected_minutes": len(missing),
                "missing_examples_et": [
                    value.astimezone(EASTERN).strftime("%H:%M") for value in missing[:10]
                ],
            }
        cross_symbol_capture_skews = []
        for timestamp in aligned:
            capture_times = [
                grouped[session_day][symbol]["request_times"][timestamp]
                for symbol in SYMBOLS
            ]
            cross_symbol_capture_skews.append(
                (max(capture_times) - min(capture_times)).total_seconds()
            )
        maximum_cross_symbol_capture_skew = (
            max(cross_symbol_capture_skews) if cross_symbol_capture_skews else None
        )
        p95_cross_symbol_capture_skew = _percentile(
            cross_symbol_capture_skews, 0.95
        )
        capture_sync_passed = (
            maximum_cross_symbol_capture_skew is not None
            and maximum_cross_symbol_capture_skew
            <= MAX_CROSS_SYMBOL_CAPTURE_SKEW_SECONDS
        )
        aligned_missing = sorted(expected - aligned)
        internal_missing: list[datetime] = []
        maximum_internal_gap = 0
        if aligned:
            first_aligned = min(aligned)
            last_aligned = max(aligned)
            span_minutes = int((last_aligned - first_aligned).total_seconds() // 60) + 1
            internal_expected = {
                first_aligned + timedelta(minutes=index) for index in range(span_minutes)
            }
            internal_missing = sorted(internal_expected - aligned)
            run = 0
            previous: Optional[datetime] = None
            for value in internal_missing:
                run = run + 1 if previous and value - previous == timedelta(minutes=1) else 1
                maximum_internal_gap = max(maximum_internal_gap, run)
                previous = value
        session_p95_lag = _percentile(session_lags, 0.95)
        session_negative_lags = sum(value < 0 for value in session_lags)
        aligned_minimum_passed = (
            bool(expected) and len(aligned) >= minimum_aligned_minutes
        )
        complete = eligible_full_session and aligned_minimum_passed
        opening_anchor = {
            start.astimezone(timezone.utc) + timedelta(minutes=index)
            for index in range(min(MANDATORY_ANCHOR_MINUTES, scheduled_minutes))
        }
        closing_anchor = {
            start.astimezone(timezone.utc)
            + timedelta(
                minutes=scheduled_minutes
                - min(MANDATORY_ANCHOR_MINUTES, scheduled_minutes)
                + index
            )
            for index in range(min(MANDATORY_ANCHOR_MINUTES, scheduled_minutes))
        }
        opening_anchor_missing = sorted(opening_anchor - aligned)
        closing_anchor_missing = sorted(closing_anchor - aligned)
        session_checks = {
            "full_session_required": eligible_full_session,
            "aligned_minimum": aligned_minimum_passed,
            "capture_protocol_consistent": (
                session_capture_protocol_versions == {CAPTURE_PROTOCOL_VERSION}
            ),
            "cross_symbol_capture_sync": capture_sync_passed,
            "opening_anchor_complete": not opening_anchor_missing,
            "closing_anchor_complete": not closing_anchor_missing,
            "maximum_consecutive_internal_gap": (
                maximum_internal_gap <= MAX_CONSECUTIVE_INTERNAL_GAP_MINUTES
            ),
        }
        session_failures = [
            check for check, passed in session_checks.items() if not passed
        ]
        quality_passed = all(session_checks.values()) and all(
            item["quality_passed"] for item in symbol_output.values()
        )
        quality_failures = {
            symbol: item["quality_failures"]
            for symbol, item in symbol_output.items()
            if item["quality_failures"]
        }
        if session_failures:
            quality_failures["SESSION"] = session_failures
        sessions.append({
            "session_day": session_day,
            "session_type": session_type,
            "capture_attempt_count": sum(attempts_by_session.get(session_day, {}).values()),
            "capture_attempt_outcomes": attempts_by_session.get(session_day, {}),
            "eligible_full_session": eligible_full_session,
            "scheduled_close_et": (
                scheduled_close.strftime("%H:%M") if scheduled_close else None
            ),
            "scheduled_regular_minutes": scheduled_minutes,
            "minimum_aligned_minutes": minimum_aligned_minutes,
            "complete": complete,
            "qualified": quality_passed,
            "quality_passed": quality_passed,
            "quality_failures": quality_failures,
            "session_quality_checks": session_checks,
            "session_quality_failures": session_failures,
            "capture_protocol_versions": sorted(session_capture_protocol_versions),
            "capture_protocol_consistency": (
                "PASS"
                if session_capture_protocol_versions == {CAPTURE_PROTOCOL_VERSION}
                else "FAIL"
            ),
            "cross_symbol_capture_sync": (
                "PASS" if capture_sync_passed else "FAIL"
            ),
            "maximum_cross_symbol_capture_skew_seconds": (
                round(maximum_cross_symbol_capture_skew, 6)
                if maximum_cross_symbol_capture_skew is not None
                else None
            ),
            "p95_cross_symbol_capture_skew_seconds": p95_cross_symbol_capture_skew,
            "opening_anchor": "PASS" if not opening_anchor_missing else "FAIL",
            "opening_anchor_missing_minutes": len(opening_anchor_missing),
            "opening_anchor_missing_examples_et": [
                value.astimezone(EASTERN).strftime("%H:%M")
                for value in opening_anchor_missing[:10]
            ],
            "closing_anchor": "PASS" if not closing_anchor_missing else "FAIL",
            "closing_anchor_missing_minutes": len(closing_anchor_missing),
            "closing_anchor_missing_examples_et": [
                value.astimezone(EASTERN).strftime("%H:%M")
                for value in closing_anchor_missing[:10]
            ],
            "gap_tolerance": (
                "PASS"
                if maximum_internal_gap <= MAX_CONSECUTIVE_INTERNAL_GAP_MINUTES
                else "FAIL"
            ),
            "internal_continuity": "PASS" if not internal_missing else "FAIL",
            "aligned_valid_minutes": len(aligned),
            "aligned_missing_minutes": len(aligned_missing),
            "aligned_coverage": (
                round(len(aligned) / len(expected), 6) if expected else 0.0
            ),
            "aligned_missing_examples_et": [
                value.astimezone(EASTERN).strftime("%H:%M")
                for value in aligned_missing[:10]
            ],
            "internal_missing_minutes": len(internal_missing),
            "maximum_internal_gap_minutes": maximum_internal_gap,
            "internal_missing_examples_et": [
                value.astimezone(EASTERN).strftime("%H:%M")
                for value in internal_missing[:10]
            ],
            "bar_timeliness": (
                "PASS"
                if session_negative_lags == 0
                and session_p95_lag is not None
                and session_p95_lag <= MAX_P95_BAR_CLOSE_LAG_SECONDS
                else "FAIL"
            ),
            "p95_bar_close_lag_seconds": session_p95_lag,
            "negative_bar_close_lag_count": session_negative_lags,
            "symbols": symbol_output,
        })

    complete_sessions = [item["session_day"] for item in sessions if item["complete"]]
    qualified_sessions = [item["session_day"] for item in sessions if item["qualified"]]
    output.update({
        "status": "recording" if sessions else "not_started",
        "capture_attempt_audit_available": attempt_summary["status"] == "available",
        "capture_attempt_count": attempt_summary["total_attempts"],
        "complete_session_count": len(complete_sessions),
        "qualified_session_count": len(qualified_sessions),
        "complete_sessions": complete_sessions,
        "qualified_sessions": qualified_sessions,
        "rejected_complete_sessions": [
            item for item in complete_sessions if item not in qualified_sessions
        ],
        "excluded_early_close_sessions": [
            item["session_day"]
            for item in sessions
            if item["session_type"] == "EARLY_CLOSE"
        ],
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
            "complete_session_count": 0,
            "qualified_session_count": 0,
            "target_qualified_sessions": TARGET_SESSIONS,
            "target_complete_sessions": TARGET_SESSIONS,
            "nyse_calendar_source": NYSE_CALENDAR_SOURCE,
            "nyse_calendar_version": NYSE_CALENDAR_VERSION,
            "nyse_calendar_supported_years": sorted(NYSE_CALENDAR_SUPPORTED_YEARS),
            "excluded_early_close_sessions": [],
            "max_p95_bar_close_lag_seconds": MAX_P95_BAR_CLOSE_LAG_SECONDS,
            "max_cross_symbol_capture_skew_seconds": MAX_CROSS_SYMBOL_CAPTURE_SKEW_SECONDS,
            "required_capture_protocol_version": CAPTURE_PROTOCOL_VERSION,
            "capture_attempt_audit": {
                "status": "not_available",
                "total_attempts": 0,
                "counts_by_outcome": {},
            },
            "database": str(database),
            "installed": LAUNCH_PLIST.exists(),
            "launch_label": LAUNCH_LABEL,
            "orders_enabled": False,
        }
    coverage = session_coverage(database)
    complete = coverage["complete_sessions"]
    qualified = coverage["qualified_sessions"]
    with closing(_read_connection(database)) as connection:
        symbols = {}
        for symbol in SYMBOLS:
            summary = connection.execute(
                """
                SELECT COUNT(*), MIN(bar_time), MAX(bar_time), COUNT(DISTINCT session_day),
                       AVG(valid_bar)
                FROM samples WHERE symbol = ?
                """,
                (symbol,),
            ).fetchone()
            quote_rows = connection.execute(
                "SELECT request_time, quote_time, valid_quote, spread_bps "
                "FROM samples WHERE symbol = ?",
                (symbol,),
            ).fetchall()
            all_ages = [
                age
                for row in quote_rows
                if (age := _stored_quote_age(row[0], row[1])) is not None
            ]
            valid_quote_rows = [
                row
                for row in quote_rows
                if row[2]
                and (age := _stored_quote_age(row[0], row[1])) is not None
                and age >= 0
            ]
            ages = [
                _stored_quote_age(row[0], row[1])
                for row in valid_quote_rows
            ]
            spreads = [row[3] for row in valid_quote_rows if row[3] is not None]
            bar_close_lags = [
                (
                    parse_time(row[1]).astimezone(timezone.utc)
                    - parse_time(row[0]).astimezone(timezone.utc)
                    - timedelta(minutes=1)
                ).total_seconds()
                for row in connection.execute(
                    "SELECT bar_time, request_time FROM samples WHERE symbol = ?",
                    (symbol,),
                )
            ]
            p95_spread = _percentile(spreads, 0.95)
            p95_age = _percentile(ages, 0.95)
            p95_bar_close_lag = _percentile(bar_close_lags, 0.95)
            negative_bar_close_lags = sum(value < 0 for value in bar_close_lags)
            negative_quote_ages = sum(value < 0 for value in all_ages)
            valid_quote_coverage = (
                len(valid_quote_rows) / summary[0] if summary[0] else 0.0
            )
            checks = {
                "qualified_sessions": len(qualified) >= TARGET_SESSIONS,
                "valid_bar_coverage": (summary[4] or 0.0) == 1.0,
                "valid_quote_coverage": valid_quote_coverage >= 0.99,
                "quote_time_order": negative_quote_ages == 0,
                "p95_quote_age": p95_age is not None and p95_age <= 5.0,
                "p95_spread": p95_spread is not None and p95_spread <= 5.0,
                "bar_close_time_order": negative_bar_close_lags == 0,
                "p95_bar_close_lag": (
                    p95_bar_close_lag is not None
                    and p95_bar_close_lag <= MAX_P95_BAR_CLOSE_LAG_SECONDS
                ),
            }
            symbols[symbol] = {
                "samples": summary[0],
                "first_bar": summary[1],
                "last_bar": summary[2],
                "observed_sessions": summary[3],
                "valid_bar_coverage": round(summary[4] or 0.0, 6),
                "valid_quote_coverage": round(valid_quote_coverage, 6),
                "p95_spread_bps": p95_spread,
                "p95_quote_age_seconds": p95_age,
                "negative_quote_age_count": negative_quote_ages,
                "p95_bar_close_lag_seconds": p95_bar_close_lag,
                "negative_bar_close_lag_count": negative_bar_close_lags,
                "quality_checks": checks,
                "quality_passed": all(checks.values()),
            }
        started = connection.execute(
            "SELECT value FROM metadata WHERE key = 'started_at'"
        ).fetchone()
        attempt_summary = _capture_attempt_summary(connection)
    complete_run = len(qualified) >= TARGET_SESSIONS
    all_quality_passed = complete_run
    return {
        "status": "completed" if complete_run else "recording",
        "decision": "DATA_USABLE" if all_quality_passed else "PENDING",
        "started_at": started[0] if started else None,
        "symbols": symbols,
        "complete_sessions": complete,
        "complete_session_count": len(complete),
        "qualified_sessions": qualified,
        "qualified_session_count": len(qualified),
        "rejected_complete_sessions": coverage["rejected_complete_sessions"],
        "excluded_early_close_sessions": coverage["excluded_early_close_sessions"],
        "target_qualified_sessions": TARGET_SESSIONS,
        "target_complete_sessions": TARGET_SESSIONS,
        "nyse_calendar_source": NYSE_CALENDAR_SOURCE,
        "nyse_calendar_version": NYSE_CALENDAR_VERSION,
        "nyse_calendar_supported_years": sorted(NYSE_CALENDAR_SUPPORTED_YEARS),
        "max_p95_bar_close_lag_seconds": MAX_P95_BAR_CLOSE_LAG_SECONDS,
        "max_cross_symbol_capture_skew_seconds": MAX_CROSS_SYMBOL_CAPTURE_SKEW_SECONDS,
        "required_capture_protocol_version": CAPTURE_PROTOCOL_VERSION,
        "capture_attempt_audit": attempt_summary,
        "database": str(database),
        "installed": LAUNCH_PLIST.exists(),
        "launch_label": LAUNCH_LABEL,
        "orders_enabled": False,
    }


def execution_diagnostics(database: Path = DATABASE) -> dict[str, Any]:
    """Estimate top-of-book cost hurdles without creating signals or orders."""
    recorder = status(database)
    qualified_sessions = set(recorder.get("qualified_sessions", []))
    output: dict[str, Any] = {
        "status": "ready" if recorder["decision"] == "DATA_USABLE" else "collecting",
        "decision": recorder["decision"],
        "interpretation": "EXECUTION_DIAGNOSTIC_ONLY",
        "sample_scope": "QUALIFIED_SESSIONS_ONLY",
        "qualified_sessions": sorted(qualified_sessions),
        "complete_session_count": recorder.get("complete_session_count", 0),
        "qualified_session_count": recorder.get("qualified_session_count", 0),
        "target_qualified_sessions": TARGET_SESSIONS,
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

    with closing(_read_connection(database)) as connection:
        for symbol in SYMBOLS:
            rows = connection.execute(
                """
                SELECT session_day, bar_time, bid, ask, mid, request_time, quote_time
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
                if row[0] in qualified_sessions
                and None not in row[2:5]
                and (age := _stored_quote_age(row[5], row[6])) is not None
                and age >= 0
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
    fixed_clock = now is not None
    now = (now or _utc_now()).astimezone(timezone.utc)
    with _lock(lock_file):
        coverage = session_coverage(database)
        if coverage["qualified_session_count"] >= TARGET_SESSIONS:
            return {"outcome": "complete", "recorded": 0, **status(database)}
        session_type, _ = _session_schedule(now.astimezone(EASTERN).date())
        if session_type == "UNSUPPORTED":
            return {
                "outcome": "calendar_unsupported",
                "session_type": session_type,
                "recorded": 0,
                **status(database),
            }
        if session_type == "CLOSED":
            return {
                "outcome": "market_closed",
                "session_type": session_type,
                "recorded": 0,
                **status(database),
            }
        if not _is_regular_hours(now):
            return {
                "outcome": "outside_regular_hours",
                "session_type": session_type,
                "recorded": 0,
                **status(database),
            }
        with closing(_connect(database, now)) as connection:
            completed_at = lambda: now if fixed_clock else _utc_now().astimezone(timezone.utc)
            try:
                bars = _latest_closed_bars(api, now)
            except Exception as exc:
                _record_attempt(
                    connection,
                    started_at=now,
                    completed_at=completed_at(),
                    session_type=session_type,
                    stage="BARS",
                    outcome="BAR_API_ERROR",
                    error_type=type(exc).__name__,
                )
                raise
            if set(bars) != set(SYMBOLS):
                audit_outcome = (
                    "PARTIAL_BAR_RESPONSE" if bars else "NO_CURRENT_CLOSED_BAR"
                )
                _record_attempt(
                    connection,
                    started_at=now,
                    completed_at=completed_at(),
                    session_type=session_type,
                    stage="BARS",
                    outcome=audit_outcome,
                    bar_symbol_count=len(bars),
                )
                return {
                    "outcome": audit_outcome.lower(),
                    "session_type": session_type,
                    "recorded": 0,
                    **status(database),
                }
            try:
                quotes = _snapshot_rows(api)
            except Exception as exc:
                _record_attempt(
                    connection,
                    started_at=now,
                    completed_at=completed_at(),
                    session_type=session_type,
                    stage="SNAPSHOTS",
                    outcome="SNAPSHOT_API_ERROR",
                    bar_symbol_count=len(bars),
                    error_type=type(exc).__name__,
                )
                raise
            if set(quotes) != set(SYMBOLS):
                _record_attempt(
                    connection,
                    started_at=now,
                    completed_at=completed_at(),
                    session_type=session_type,
                    stage="SNAPSHOTS",
                    outcome="PARTIAL_SNAPSHOT_RESPONSE",
                    bar_symbol_count=len(bars),
                    quote_symbol_count=len(quotes),
                )
                raise RuntimeError("Webull snapshot response omitted a forward-recording symbol")
            received_at = completed_at()
            recorded = 0
            try:
                for symbol in SYMBOLS:
                    cursor = connection.execute(
                        INSERT_SAMPLE,
                        _record(symbol, bars[symbol], quotes[symbol], received_at),
                    )
                    recorded += cursor.rowcount
                audit_outcome = (
                    "RECORDED"
                    if recorded == len(SYMBOLS)
                    else "DUPLICATE_BAR"
                    if recorded == 0
                    else "PARTIAL_RECORD"
                )
                _record_attempt(
                    connection,
                    started_at=now,
                    completed_at=received_at,
                    session_type=session_type,
                    stage="COMMIT",
                    outcome=audit_outcome,
                    bar_symbol_count=len(bars),
                    quote_symbol_count=len(quotes),
                    recorded_rows=recorded,
                )
            except Exception as exc:
                connection.rollback()
                _record_attempt(
                    connection,
                    started_at=now,
                    completed_at=received_at,
                    session_type=session_type,
                    stage="COMMIT",
                    outcome="RECORD_ERROR",
                    bar_symbol_count=len(bars),
                    quote_symbol_count=len(quotes),
                    error_type=type(exc).__name__,
                )
                raise
    return {
        "outcome": audit_outcome.lower(),
        "session_type": session_type,
        "recorded": recorded,
        **status(database),
    }


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
        "StartCalendarInterval": [{"Minute": minute} for minute in range(60)],
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

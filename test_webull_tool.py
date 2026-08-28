import json
import signal
import sqlite3
import sys
import tempfile
import unittest
import urllib.parse
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from config import APP_KEY, APP_SECRET
from classic_sector_momentum import (
    DATA_SYMBOLS as CLASSIC_SECTOR_SYMBOLS,
    DEVELOPMENT_FETCH_END,
    SECTORS as CLASSIC_SECTORS,
    ZERO_COST as CLASSIC_ZERO_COST,
    _stage as classic_sector_stage,
    _target_weights as classic_target_weights,
    run_classic_sector_momentum,
    simulate as simulate_classic_sector_momentum,
)
from crypto_strategy import (
    Bar,
    DataQuality,
    backtest,
    coinbase_bars,
    ema,
    heikin_ashi_bullish,
    normalize_bars,
    parse_time,
    quantity_for_notional,
    signals,
    webull_bars,
)
from daytrader_strategy import (
    _buy_and_hold,
    _professional_gate,
    allocation_for_risk,
    backtest_daytrader,
    daytrade_signals,
)
from equity_orb_strategy import (
    BASE_COST as ORB_BASE_COST,
    Session,
    _features as orb_features,
    _opening_bar,
    _quantity as orb_quantity,
    _trade_session as trade_orb_session,
    build_sessions as build_stock_sessions,
    webull_stock_bars,
)
from equity_forward_recorder import (
    CAPTURE_PROTOCOL_VERSION as EQUITY_FORWARD_CAPTURE_PROTOCOL_VERSION,
    EASTERN as EQUITY_FORWARD_EASTERN,
    INSERT_SAMPLE as EQUITY_FORWARD_INSERT_SAMPLE,
    LAUNCH_LABEL as EQUITY_FORWARD_LAUNCH_LABEL,
    SCHEMA as EQUITY_FORWARD_SCHEMA,
    SYMBOLS as EQUITY_FORWARD_SYMBOLS,
    _awake_guard_duration as equity_forward_awake_guard_duration,
    _ensure_awake_guard as ensure_equity_forward_awake_guard,
    _stop_awake_guard as stop_equity_forward_awake_guard,
    _is_regular_hours as equity_forward_is_regular_hours,
    _launch_payload as equity_forward_launch_payload,
    _read_connection as equity_forward_read_connection,
    _session_schedule as equity_forward_session_schedule,
    execution_diagnostics as equity_execution_diagnostics,
    record_once as record_equity_forward_once,
    session_coverage as equity_session_coverage,
    status as equity_forward_status,
)
from equity_desk import (
    RESEARCH_LEDGER,
    _session_phase as equity_desk_session_phase,
    desk_journal_status,
    desk_status as equity_desk_status,
    record_desk_snapshot,
    research_status as equity_research_status,
)
from execution_guard import authorization_status, authorize_automated_order
from intraday_momentum_strategy import (
    Observation,
    _execute as execute_intraday_momentum,
    backtest as backtest_intraday_momentum,
    observations as intraday_momentum_observations,
)
from intermediate_sector_momentum import (
    DEVELOPMENT_FETCH_END as INTERMEDIATE_DEVELOPMENT_FETCH_END,
    _aligned_close as align_intermediate_close,
    run_intermediate_sector_momentum,
    simulate as simulate_intermediate_sector_momentum,
)
from noise_area_strategy import (
    DIVIDENDS as NOISE_DIVIDENDS,
    checkpoints as noise_checkpoints,
    execute_targets as execute_noise_targets,
)
from opening_pressure_strategy import (
    DATA_SYMBOLS as PRESSURE_SYMBOLS,
    OpeningSignal,
    opening_signal,
    trade_day as trade_pressure_day,
)
from opening_momentum_strategy import run_opening_momentum_backtest
from relative_value_strategy import (
    checkpoint_z_scores as pair_checkpoint_z_scores,
    trade_day as trade_pair_day,
)
from runtime_environment import base_python, ensure_runtime_venv
from webull_api import is_mutating_call, redact_secrets
from webull_cli import cmd_forward_record_once, replace_account_placeholder
from webull_orders import (
    build_order,
    order_instrument_type,
    validate_batch_orders,
    validate_order,
)
from supertrend_strategy import supertrend_signals, wilder_atr


OPTION_LEG = {
    "side": "BUY",
    "quantity": "1",
    "symbol": "AAPL",
    "strike_price": "220",
    "option_expire_date": "2026-12-18",
    "instrument_type": "OPTION",
    "option_type": "CALL",
    "market": "US",
}


class OrderBuilderTests(unittest.TestCase):
    def test_builds_equity_limit_order(self):
        order = build_order(
            symbol="aapl",
            instrument_type="equity",
            side="buy",
            quantity="0.5",
            order_type="limit",
            tif="day",
            limit_price="100",
            session="core",
        )
        self.assertEqual(order["symbol"], "AAPL")
        self.assertEqual(order["instrument_type"], "EQUITY")
        self.assertEqual(order["quantity"], "0.5")
        self.assertEqual(order["support_trading_session"], "CORE")

    def test_builds_current_option_schema(self):
        order = build_order(
            symbol="AAPL",
            instrument_type="OPTION",
            side="BUY",
            quantity="1",
            order_type="LIMIT",
            tif="GTC",
            limit_price="2.5",
            option_strategy="SINGLE",
            legs=[OPTION_LEG],
        )
        self.assertEqual(order["instrument_type"], "OPTION")
        self.assertEqual(order["symbol"], "AAPL")
        self.assertEqual(order_instrument_type(order), "OPTION")

    def test_rejects_option_sell_gtc(self):
        with self.assertRaisesRegex(ValueError, "DAY"):
            build_order(
                symbol="AAPL",
                instrument_type="OPTION",
                side="SELL",
                quantity="1",
                order_type="LIMIT",
                tif="GTC",
                limit_price="2.5",
                option_strategy="SINGLE",
                legs=[{**OPTION_LEG, "side": "SELL"}],
            )

    def test_accepts_covered_stock_mixed_legs(self):
        equity_leg = {
            "side": "BUY",
            "quantity": "100",
            "symbol": "AAPL",
            "instrument_type": "EQUITY",
            "market": "US",
        }
        order = build_order(
            symbol="AAPL",
            instrument_type="OPTION",
            side="BUY",
            quantity="1",
            order_type="MARKET",
            tif="DAY",
            option_strategy="COVERED_STOCK",
            legs=[equity_leg, {**OPTION_LEG, "side": "SELL"}],
        )
        self.assertEqual(order_instrument_type(order), "OPTION")

    def test_rejects_invalid_event_price(self):
        with self.assertRaisesRegex(ValueError, "0.01"):
            build_order(
                symbol="TEST-EVENT",
                instrument_type="EVENT",
                side="BUY",
                quantity="1",
                order_type="LIMIT",
                tif="DAY",
                limit_price="1.10",
                event_outcome="yes",
            )

    def test_requires_ioc_for_crypto_market(self):
        with self.assertRaisesRegex(ValueError, "IOC"):
            build_order(
                symbol="BTCUSD",
                instrument_type="CRYPTO",
                side="BUY",
                quantity="0.001",
                order_type="MARKET",
                tif="DAY",
            )

    def test_requires_trailing_fields(self):
        with self.assertRaisesRegex(ValueError, "trailing_type"):
            build_order(
                symbol="AAPL",
                instrument_type="EQUITY",
                side="SELL",
                quantity="1",
                order_type="TRAILING_STOP_LOSS",
                tif="DAY",
            )

    def test_validates_batch_shape(self):
        order = build_order(
            symbol="AAPL",
            instrument_type="EQUITY",
            side="BUY",
            quantity="1",
            order_type="LIMIT",
            tif="DAY",
            limit_price="10",
        )
        validate_batch_orders([order])
        with self.assertRaisesRegex(ValueError, "equities only"):
            validate_batch_orders([{**order, "instrument_type": "CRYPTO"}])

    def test_raw_validation_requires_symbol(self):
        order = build_order(
            symbol="AAPL",
            instrument_type="EQUITY",
            side="BUY",
            quantity="1",
            order_type="MARKET",
            tif="DAY",
        )
        del order["symbol"]
        with self.assertRaisesRegex(ValueError, "symbol"):
            validate_order(order)


class SafetyTests(unittest.TestCase):
    def test_detects_mutating_sdk_calls(self):
        self.assertTrue(is_mutating_call("trade.orders_v3.place_order"))
        self.assertTrue(is_mutating_call("data.watchlist.delete_watchlist"))
        self.assertFalse(is_mutating_call("trade.orders_v3.preview_order"))
        self.assertFalse(is_mutating_call("data.market.get_snapshot"))

    def test_replaces_nested_account_placeholder(self):
        value = {"account": "@account", "items": ["x", "@account"]}
        self.assertEqual(
            replace_account_placeholder(value, "paper-id"),
            {"account": "paper-id", "items": ["x", "paper-id"]},
        )

    def test_redacts_credentials(self):
        message = redact_secrets(f"key={APP_KEY}; secret={APP_SECRET}")
        self.assertNotIn(APP_KEY, message)
        self.assertNotIn(APP_SECRET, message)
        self.assertEqual(message.count("[redacted]"), 2)


def make_bars(count: int, *, start: datetime = None, interval_seconds: int = 7200) -> list[Bar]:
    start = start or datetime(2026, 1, 1, tzinfo=timezone.utc)
    return [
        Bar(
            time=start + timedelta(seconds=index * interval_seconds),
            open=Decimal("100"),
            high=Decimal("101"),
            low=Decimal("99"),
            close=Decimal("100"),
        )
        for index in range(count)
    ]


class CryptoStrategyTests(unittest.TestCase):
    def test_parses_webull_compact_utc_offset(self):
        self.assertEqual(
            parse_time("2026-08-27T10:00:00.000+0000"),
            datetime(2026, 8, 27, 10, tzinfo=timezone.utc),
        )

    def test_ema_uses_seeded_simple_average(self):
        result = ema([Decimal("1"), Decimal("2"), Decimal("3"), Decimal("4")], 3)
        self.assertEqual(result, [None, None, Decimal("2"), Decimal("3")])

    def test_heikin_ashi_direction(self):
        bars = [
            Bar(datetime(2026, 1, 1, tzinfo=timezone.utc), Decimal("10"), Decimal("14"), Decimal("9"), Decimal("12")),
            Bar(datetime(2026, 1, 2, tzinfo=timezone.utc), Decimal("8"), Decimal("9"), Decimal("6"), Decimal("7")),
        ]
        self.assertEqual(heikin_ashi_bullish(bars), [True, False])

    def test_cross_signals_require_bullish_entry_confirmation(self):
        bars = make_bars(52)
        fast = [None] * 49 + [Decimal("9"), Decimal("11"), Decimal("8")]
        slow = [None] * 49 + [Decimal("10"), Decimal("10"), Decimal("10")]
        with mock.patch("crypto_strategy.ema", side_effect=[fast, slow]), mock.patch(
            "crypto_strategy.heikin_ashi_bullish", return_value=[True] * 52
        ):
            result = signals(bars)
        self.assertEqual(result[50], "BUY")
        self.assertEqual(result[51], "SELL")

    def test_signal_executes_at_next_open_with_spread_and_no_lookahead(self):
        bars = make_bars(53)
        bars[52] = Bar(bars[52].time, Decimal("110"), Decimal("111"), Decimal("109"), Decimal("110"))
        generated = ["HOLD"] * 53
        generated[50], generated[51] = "BUY", "SELL"
        with mock.patch("crypto_strategy.signals", return_value=generated):
            result = backtest(bars, spread=Decimal("0.01"))
        trade = result["trades"][0]
        self.assertEqual(trade["entry_time"], bars[51].time.isoformat())
        self.assertEqual(trade["entry_price"], 101.0)
        self.assertEqual(trade["exit_time"], bars[52].time.isoformat())
        self.assertEqual(trade["exit_price"], 108.9)

    def test_gap_stop_uses_worse_open_price(self):
        bars = make_bars(53)
        bars[52] = Bar(bars[52].time, Decimal("90"), Decimal("91"), Decimal("89"), Decimal("90"))
        generated = ["HOLD"] * 53
        generated[50] = "BUY"
        with mock.patch("crypto_strategy.signals", return_value=generated):
            result = backtest(bars, spread=Decimal("0.01"))
        trade = result["trades"][0]
        self.assertEqual(trade["reason"], "stop")
        self.assertEqual(trade["exit_price"], 89.1)

    def test_backtest_can_disable_extra_stop(self):
        bars = make_bars(53)
        bars[52] = Bar(bars[52].time, Decimal("90"), Decimal("91"), Decimal("80"), Decimal("90"))
        generated = ["HOLD"] * 53
        generated[50] = "BUY"
        result = backtest(
            bars,
            spread=Decimal("0.01"),
            stop_loss=None,
            signal_values=generated,
        )
        self.assertEqual(result["trades"][0]["reason"], "end")

    def test_quantity_rounds_down_to_lot_size(self):
        self.assertEqual(
            quantity_for_notional(Decimal("10"), Decimal("3"), Decimal("0.01")),
            Decimal("3.33"),
        )

    def test_wilder_atr_uses_recursive_smoothing(self):
        bars = make_bars(4)
        bars[2] = Bar(bars[2].time, Decimal("100"), Decimal("104"), Decimal("98"), Decimal("103"))
        bars[3] = Bar(bars[3].time, Decimal("103"), Decimal("104"), Decimal("102"), Decimal("103"))
        result = wilder_atr(bars, period=2)
        self.assertEqual(result[1], Decimal("2"))
        self.assertEqual(result[2], Decimal("4"))
        self.assertEqual(result[3], Decimal("3"))

    def test_supertrend_emits_reversal_signals(self):
        closes = (Decimal("10"), Decimal("11"), Decimal("12"), Decimal("4"), Decimal("3"), Decimal("12"))
        bars = [
            Bar(
                datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(hours=index),
                close,
                close + Decimal("0.5"),
                close - Decimal("0.5"),
                close,
            )
            for index, close in enumerate(closes)
        ]
        result = supertrend_signals(bars, period=2, multiplier=Decimal("1"))
        self.assertIn("SELL", result)
        self.assertIn("BUY", result)

    def test_normalize_sorts_deduplicates_and_drops_open_candle(self):
        now = datetime(2026, 1, 2, tzinfo=timezone.utc)
        bars = make_bars(12, start=now - timedelta(days=1))
        raw = list(reversed(bars)) + [bars[3], make_bars(1, start=now)[0]]
        result, duplicates = normalize_bars(raw, interval_seconds=7200, now=now)
        self.assertEqual(result, bars)
        self.assertEqual(duplicates, 1)

    def test_webull_reversed_bars_have_full_coverage(self):
        now = datetime(2026, 1, 2, tzinfo=timezone.utc)
        bars = make_bars(12, start=now - timedelta(days=1))
        rows = [
            {
                "time": bar.time.isoformat(),
                "open": str(bar.open),
                "high": str(bar.high),
                "low": str(bar.low),
                "close": str(bar.close),
            }
            for bar in reversed(bars)
        ]
        api = SimpleNamespace(data=SimpleNamespace(crypto_market_data=SimpleNamespace(
            get_crypto_history_bar=lambda *_: [{"symbol": "BTCUSD", "result": rows}]
        )))
        result, quality = webull_bars(api, ["BTCUSD"], now=now, days=1, count=20)["BTCUSD"]
        self.assertEqual(result, bars)
        self.assertTrue(quality.passed)

    def test_coinbase_paginates_deduplicates_and_checks_gaps(self):
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        end = start + timedelta(minutes=5 * 600)
        calls = []

        def request_json(url):
            calls.append(url)
            query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
            page_start = datetime.fromisoformat(query["start"][0])
            page_end = datetime.fromisoformat(query["end"][0])
            rows = []
            cursor = page_start
            while cursor <= page_end:
                rows.append([int(cursor.timestamp()), 99, 101, 100, 100, 1])
                cursor += timedelta(minutes=5)
            return list(reversed(rows))

        with tempfile.TemporaryDirectory() as directory:
            bars, quality = coinbase_bars(
                "BTCUSD",
                start=start,
                end=end,
                request_json=request_json,
                sleep=lambda _: None,
                cache_dir=Path(directory),
            )
        self.assertEqual(len(calls), 3)
        self.assertEqual(len(bars), 600)
        self.assertEqual(quality.duplicates, 2)
        self.assertTrue(quality.passed)

    def test_daytrader_breakout_uses_only_prior_bars(self):
        bars = make_bars(102, interval_seconds=300)
        bars[-1] = Bar(
            bars[-1].time,
            Decimal("100"),
            Decimal("103"),
            Decimal("99"),
            Decimal("102"),
        )
        fast = [None] * 100 + [Decimal("101"), Decimal("101")]
        slow = [None] * 100 + [Decimal("100"), Decimal("100")]
        with mock.patch("daytrader_strategy.ema", side_effect=[fast, slow]):
            result = daytrade_signals(bars)
        self.assertEqual(result[-1], "BUY")

    def test_daytrader_executes_next_open_and_applies_target_after_cost(self):
        bars = make_bars(103, interval_seconds=300)
        bars[101] = Bar(bars[101].time, Decimal("100"), Decimal("102"), Decimal("100"), Decimal("101"))
        bars[102] = Bar(bars[102].time, Decimal("107"), Decimal("110"), Decimal("106"), Decimal("108"))
        generated = ["HOLD"] * 103
        generated[100] = "BUY"
        with mock.patch("daytrader_strategy.daytrade_signals", return_value=generated):
            result = backtest_daytrader(bars, cost_per_side=Decimal("0.01"))
        trade = result["trades"][0]
        self.assertEqual(trade["entry_time"], bars[101].time.isoformat())
        self.assertEqual(trade["entry_price"], 101.0)
        self.assertEqual(trade["exit_time"], bars[102].time.isoformat())
        self.assertEqual(trade["exit_price"], 106.05)
        self.assertEqual(trade["reason"], "target")

    def test_daytrader_sizes_from_total_loss_budget(self):
        self.assertEqual(allocation_for_risk(), Decimal("0.0025"))

    def test_buy_and_hold_uses_same_allocation_and_both_side_costs(self):
        bars = [
            Bar(datetime(2026, 1, 1, tzinfo=timezone.utc), Decimal("100"), Decimal("100"), Decimal("100"), Decimal("100")),
            Bar(datetime(2026, 1, 2, tzinfo=timezone.utc), Decimal("100"), Decimal("100"), Decimal("100"), Decimal("100")),
        ]
        benchmark = _buy_and_hold(bars, Decimal("0.01"))
        self.assertAlmostEqual(benchmark["net_profit"], -49.5049505)

    def test_professional_gate_requires_positive_oos_excess_return(self):
        dataset = {
            "quality": {"passed": True},
            "costs": {"0.0100": {"net_profit": 10}},
            "out_of_sample": {
                "costs": {
                    "0.0100": {"net_profit": 10, "trade_count": 20, "profit_factor": 1.5, "max_drawdown": 0.0005},
                    "0.0125": {"net_profit": 1},
                },
                "buy_and_hold": {"0.0100": {"net_profit": 5}},
            },
            "walk_forward": {"0.0100": [{"net_profit": 1}, {"net_profit": 1}, {"net_profit": -1}]},
        }
        self.assertTrue(_professional_gate(dataset)["passed"])
        dataset["out_of_sample"]["buy_and_hold"]["0.0100"]["net_profit"] = 11
        gate = _professional_gate(dataset)
        self.assertFalse(gate["passed"])
        self.assertFalse(gate["checks"]["oos_beats_buy_and_hold"])


class EquityForwardRecorderTests(unittest.TestCase):
    def _api(self, now: datetime) -> SimpleNamespace:
        api = SimpleNamespace(data=SimpleNamespace(market_data=mock.Mock()))
        bar_time = now - timedelta(minutes=1, seconds=30)
        bars = []
        snapshots = []
        for index, symbol in enumerate(EQUITY_FORWARD_SYMBOLS, start=1):
            price = Decimal(index * 100)
            bars.append({
                "symbol": symbol,
                "result": [{
                    "time": bar_time.isoformat(),
                    "open": str(price),
                    "high": str(price + 1),
                    "low": str(price - 1),
                    "close": str(price + Decimal("0.25")),
                    "volume": "1000",
                    "trading_session": "RTH",
                }],
            })
            snapshots.append({
                "symbol": symbol,
                "price": str(price + Decimal("0.25")),
                "bid": str(price),
                "ask": str(price + Decimal("0.01")),
                "bid_size": "100",
                "ask_size": "80",
                "quote_time": int((now - timedelta(seconds=1)).timestamp() * 1000),
            })
        api.data.market_data.get_batch_history_bar.return_value = SimpleNamespace(
            status_code=200,
            json=lambda: {"result": bars},
        )
        api.data.market_data.get_snapshot.return_value = SimpleNamespace(
            status_code=200,
            json=lambda: snapshots,
        )
        return api

    def test_forward_recorder_is_idempotent_and_read_only(self):
        now = datetime(2026, 8, 28, 15, 16, 30, tzinfo=timezone.utc)
        api = self._api(now)
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "forward.sqlite3"
            lock = Path(directory) / "forward.lock"
            first = record_equity_forward_once(api, now=now, database=database, lock_file=lock)
            second = record_equity_forward_once(api, now=now, database=database, lock_file=lock)
            current = equity_forward_status(database)
            with sqlite3.connect(database) as connection:
                attempts = connection.execute(
                    """
                    SELECT stage, outcome, bar_symbol_count, quote_symbol_count,
                           recorded_rows, error_type
                    FROM capture_attempts ORDER BY attempt_id
                    """
                ).fetchall()
        self.assertEqual(first["recorded"], 3)
        self.assertEqual(second["outcome"], "duplicate_bar")
        self.assertEqual(second["recorded"], 0)
        self.assertEqual(
            attempts,
            [
                ("COMMIT", "RECORDED", 3, 3, 3, None),
                ("COMMIT", "DUPLICATE_BAR", 3, 3, 0, None),
            ],
        )
        self.assertEqual(current["capture_attempt_audit"]["total_attempts"], 2)
        self.assertEqual(
            current["capture_attempt_audit"]["counts_by_outcome"],
            {"DUPLICATE_BAR": 1, "RECORDED": 1},
        )
        self.assertEqual(current["current_capture_health"]["status"], "healthy")
        self.assertEqual(
            current["current_capture_health"]["sample_scope"],
            "LATEST_SESSION_CURRENT_PROTOCOL_ONLY",
        )
        self.assertEqual(
            current["current_capture_health"]["qualification_effect"], "NONE"
        )
        self.assertEqual(
            current["current_capture_health"]["cross_symbol_capture_sync"], "PASS"
        )
        self.assertTrue(all(
            item["samples"] == 1 and item["quality_passed"]
            for item in current["current_capture_health"]["symbols"].values()
        ))
        self.assertTrue(all(item["samples"] == 1 for item in current["symbols"].values()))
        self.assertTrue(all(item["p95_quote_age_seconds"] >= 0 for item in current["symbols"].values()))
        self.assertTrue(all(
            item["p95_bar_close_lag_seconds"] == 30.0
            for item in current["symbols"].values()
        ))
        self.assertFalse(current["orders_enabled"])
        self.assertEqual(api.data.market_data.get_snapshot.call_count, 2)
        self.assertFalse(hasattr(api, "trade"))

    def test_forward_diagnostics_use_database_read_only_mode(self):
        now = datetime(2026, 8, 28, 15, 16, 30, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "forward.sqlite3"
            record_equity_forward_once(
                self._api(now),
                now=now,
                database=database,
                lock_file=Path(directory) / "forward.lock",
            )
            with mock.patch(
                "equity_forward_recorder._connect",
                side_effect=AssertionError("diagnostic opened a writable connection"),
            ):
                equity_session_coverage(database)
                equity_forward_status(database)
                equity_execution_diagnostics(database)
            connection = equity_forward_read_connection(database)
            try:
                with self.assertRaises(sqlite3.OperationalError):
                    connection.execute("UPDATE samples SET valid_bar = 0")
            finally:
                connection.close()

    def test_forward_recorder_anchors_time_after_snapshot_response(self):
        cycle_time = datetime(2026, 8, 28, 15, 16, 30, tzinfo=timezone.utc)
        received_at = cycle_time + timedelta(seconds=2)
        api = self._api(cycle_time)
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "forward.sqlite3"
            with mock.patch(
                "equity_forward_recorder._utc_now",
                side_effect=[cycle_time, received_at],
            ), mock.patch(
                "equity_forward_recorder._ensure_awake_guard",
                side_effect=AssertionError("test started a system process"),
            ):
                record_equity_forward_once(
                    api,
                    database=database,
                    lock_file=Path(directory) / "forward.lock",
                )
            with sqlite3.connect(database) as connection:
                rows = connection.execute(
                    "SELECT request_time, quote_age_seconds FROM samples"
                ).fetchall()

        self.assertEqual({row[0] for row in rows}, {received_at.isoformat()})
        self.assertEqual({row[1] for row in rows}, {3.0})

    def test_forward_recorder_migrates_legacy_schema_without_relabeling_history(self):
        now = datetime(2026, 8, 28, 15, 16, 30, tzinfo=timezone.utc)
        legacy_schema = EQUITY_FORWARD_SCHEMA.replace(
            "    capture_protocol_version TEXT,\n", ""
        )
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "forward.sqlite3"
            with sqlite3.connect(database) as connection:
                connection.executescript(legacy_schema)
                connection.execute("DROP TABLE capture_attempts")
                legacy_columns = [
                    row[1] for row in connection.execute("PRAGMA table_info(samples)")
                ]
                legacy_bar_time = now - timedelta(minutes=2, seconds=30)
                connection.execute(
                    f"INSERT INTO samples ({', '.join(legacy_columns)}) "
                    f"VALUES ({', '.join('?' for _ in legacy_columns)})",
                    (
                        "SPY",
                        legacy_bar_time.isoformat(),
                        now.isoformat(),
                        now.astimezone(EQUITY_FORWARD_EASTERN).date().isoformat(),
                        "RTH",
                        100.0,
                        101.0,
                        99.0,
                        100.0,
                        1000.0,
                        100.0,
                        99.99,
                        100.01,
                        100.0,
                        100.0,
                        (now - timedelta(seconds=1)).isoformat(),
                        100.0,
                        2.0,
                        1.0,
                        1,
                        1,
                    ),
                )
            legacy_coverage = equity_session_coverage(database)["sessions"][0]
            legacy_status = equity_forward_status(database)
            result = record_equity_forward_once(
                self._api(now),
                now=now,
                database=database,
                lock_file=Path(directory) / "forward.lock",
            )
            current = equity_forward_status(database)
            with sqlite3.connect(database) as connection:
                columns = {
                    row[1]
                    for row in connection.execute("PRAGMA table_info(samples)")
                }
                versions = {
                    row[0]
                    for row in connection.execute(
                        "SELECT capture_protocol_version FROM samples"
                    )
                }
                attempt_tables = connection.execute(
                    "SELECT COUNT(*) FROM sqlite_master "
                    "WHERE type = 'table' AND name = 'capture_attempts'"
                ).fetchone()[0]

        self.assertEqual(result["recorded"], 3)
        self.assertEqual(
            legacy_coverage["capture_protocol_versions"], ["LEGACY_UNVERSIONED"]
        )
        self.assertFalse(legacy_coverage["qualified"])
        self.assertEqual(
            legacy_status["current_capture_health"]["status"], "not_available"
        )
        self.assertIn("capture_protocol_version", columns)
        self.assertEqual(versions, {None, EQUITY_FORWARD_CAPTURE_PROTOCOL_VERSION})
        self.assertEqual(attempt_tables, 1)
        self.assertEqual(current["current_capture_health"]["status"], "healthy")
        self.assertTrue(all(
            item["samples"] == 1
            for item in current["current_capture_health"]["symbols"].values()
        ))

    def test_forward_recorder_skips_outside_regular_hours_without_market_call(self):
        now = datetime(2026, 8, 28, 22, 0, tzinfo=timezone.utc)
        api = mock.Mock(spec=[])
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "forward.sqlite3"
            result = record_equity_forward_once(
                api,
                now=now,
                database=database,
                lock_file=Path(directory) / "forward.lock",
            )
        self.assertEqual(result["outcome"], "outside_regular_hours")
        self.assertEqual(result["recorded"], 0)
        self.assertFalse(database.exists())

    def test_nyse_calendar_skips_closed_and_unsupported_days(self):
        self.assertEqual(
            equity_forward_session_schedule(date(2026, 9, 7)),
            ("CLOSED", None),
        )
        self.assertEqual(
            equity_forward_session_schedule(date(2026, 11, 27)),
            ("EARLY_CLOSE", time(13, 0)),
        )
        self.assertEqual(
            equity_forward_session_schedule(date(2026, 8, 28)),
            ("FULL", time(16, 0)),
        )
        self.assertEqual(
            equity_forward_session_schedule(date(2029, 1, 2)),
            ("UNSUPPORTED", None),
        )

        api = mock.Mock(spec=[])
        with tempfile.TemporaryDirectory() as directory:
            holiday_database = Path(directory) / "holiday.sqlite3"
            unsupported_database = Path(directory) / "unsupported.sqlite3"
            holiday = record_equity_forward_once(
                api,
                now=datetime(2026, 9, 7, 15, 0, tzinfo=timezone.utc),
                database=holiday_database,
                lock_file=Path(directory) / "holiday.lock",
            )
            unsupported = record_equity_forward_once(
                api,
                now=datetime(2029, 1, 2, 15, 0, tzinfo=timezone.utc),
                database=unsupported_database,
                lock_file=Path(directory) / "unsupported.lock",
            )

        self.assertEqual(holiday["outcome"], "market_closed")
        self.assertEqual(unsupported["outcome"], "calendar_unsupported")
        self.assertFalse(holiday_database.exists())
        self.assertFalse(unsupported_database.exists())

    def test_early_close_is_recorded_but_excluded_from_full_session_target(self):
        now = datetime(2026, 11, 27, 17, 59, 30, tzinfo=timezone.utc)
        self.assertTrue(equity_forward_is_regular_hours(now))
        self.assertFalse(
            equity_forward_is_regular_hours(
                datetime(2026, 11, 27, 18, 0, tzinfo=timezone.utc)
            )
        )
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "forward.sqlite3"
            result = record_equity_forward_once(
                self._api(now),
                now=now,
                database=database,
                lock_file=Path(directory) / "forward.lock",
            )
            coverage = equity_session_coverage(database)

        session = coverage["sessions"][0]
        self.assertEqual(result["recorded"], 3)
        self.assertEqual(result["session_type"], "EARLY_CLOSE")
        self.assertEqual(session["session_type"], "EARLY_CLOSE")
        self.assertEqual(session["scheduled_close_et"], "13:00")
        self.assertEqual(session["scheduled_regular_minutes"], 210)
        self.assertFalse(session["eligible_full_session"])
        self.assertFalse(session["complete"])
        self.assertFalse(session["qualified"])
        self.assertIn("full_session_required", session["session_quality_failures"])
        self.assertEqual(coverage["excluded_early_close_sessions"], ["2026-11-27"])
        self.assertEqual(
            coverage["nyse_calendar_version"],
            "NYSE-2026-2028-verified-2026-08-29",
        )

    def test_forward_recorder_rejects_stale_regular_hours_bar(self):
        now = datetime(2026, 8, 28, 15, 16, 30, tzinfo=timezone.utc)
        api = self._api(now)
        payload = api.data.market_data.get_batch_history_bar.return_value.json()
        for item in payload["result"]:
            item["result"][0]["time"] = (now - timedelta(minutes=4)).isoformat()
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "forward.sqlite3"
            result = record_equity_forward_once(
                api,
                now=now,
                database=database,
                lock_file=Path(directory) / "forward.lock",
            )
            with sqlite3.connect(database) as connection:
                attempt = connection.execute(
                    """
                    SELECT stage, outcome, bar_symbol_count, quote_symbol_count,
                           recorded_rows, error_type
                    FROM capture_attempts
                    """
                ).fetchone()
            coverage = equity_session_coverage(database)
        self.assertEqual(result["outcome"], "no_current_closed_bar")
        self.assertEqual(
            attempt, ("BARS", "NO_CURRENT_CLOSED_BAR", 0, 0, 0, None)
        )
        self.assertTrue(coverage["capture_attempt_audit_available"])
        self.assertEqual(coverage["capture_attempt_count"], 1)
        self.assertEqual(coverage["sessions"][0]["capture_attempt_count"], 1)
        self.assertEqual(
            coverage["sessions"][0]["capture_attempt_outcomes"],
            {"NO_CURRENT_CLOSED_BAR": 1},
        )
        api.data.market_data.get_snapshot.assert_not_called()

    def test_forward_recorder_audits_partial_snapshot_without_guessing_halt(self):
        now = datetime(2026, 8, 28, 15, 16, 30, tzinfo=timezone.utc)
        api = self._api(now)
        api.data.market_data.get_snapshot.return_value.json().pop()
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "forward.sqlite3"
            with self.assertRaisesRegex(RuntimeError, "omitted"):
                record_equity_forward_once(
                    api,
                    now=now,
                    database=database,
                    lock_file=Path(directory) / "forward.lock",
                )
            with sqlite3.connect(database) as connection:
                attempt = connection.execute(
                    """
                    SELECT stage, outcome, bar_symbol_count, quote_symbol_count,
                           recorded_rows, error_type
                    FROM capture_attempts
                    """
                ).fetchone()

        self.assertEqual(
            attempt, ("SNAPSHOTS", "PARTIAL_SNAPSHOT_RESPONSE", 3, 2, 0, None)
        )

    def test_forward_recorder_audits_api_error_type_without_message(self):
        now = datetime(2026, 8, 28, 15, 16, 30, tzinfo=timezone.utc)
        api = self._api(now)
        api.data.market_data.get_batch_history_bar.side_effect = TimeoutError(
            "provider detail must not enter audit storage"
        )
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "forward.sqlite3"
            with self.assertRaises(TimeoutError):
                record_equity_forward_once(
                    api,
                    now=now,
                    database=database,
                    lock_file=Path(directory) / "forward.lock",
                )
            with sqlite3.connect(database) as connection:
                columns = {
                    row[1] for row in connection.execute(
                        "PRAGMA table_info(capture_attempts)"
                    )
                }
                attempt = connection.execute(
                    """
                    SELECT stage, outcome, bar_symbol_count, quote_symbol_count,
                           recorded_rows, error_type
                    FROM capture_attempts
                    """
                ).fetchone()

        self.assertNotIn("error_message", columns)
        self.assertEqual(
            attempt, ("BARS", "BAR_API_ERROR", 0, 0, 0, "TimeoutError")
        )

    def test_forward_recorder_preserves_and_rejects_future_quote_time(self):
        now = datetime(2026, 8, 28, 15, 16, 30, tzinfo=timezone.utc)
        api = self._api(now)
        snapshots = api.data.market_data.get_snapshot.return_value.json()
        for item in snapshots:
            item["quote_time"] = int((now + timedelta(seconds=1)).timestamp() * 1000)
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "forward.sqlite3"
            result = record_equity_forward_once(
                api,
                now=now,
                database=database,
                lock_file=Path(directory) / "forward.lock",
            )
            current = equity_forward_status(database)
            coverage = equity_session_coverage(database)
            with sqlite3.connect(database) as connection:
                ages = [
                    row[0]
                    for row in connection.execute(
                        "SELECT quote_age_seconds FROM samples ORDER BY symbol"
                    )
                ]

        self.assertEqual(result["recorded"], 3)
        self.assertEqual(ages, [-1.0, -1.0, -1.0])
        self.assertEqual(current["current_capture_health"]["status"], "degraded")
        self.assertTrue(all(
            item["negative_quote_age_count"] == 1
            and not item["quality_checks"]["quote_time_order"]
            for item in current["current_capture_health"]["symbols"].values()
        ))
        self.assertTrue(all(
            item["valid_quote_coverage"] == 0.0
            and item["negative_quote_age_count"] == 1
            and not item["quality_checks"]["quote_time_order"]
            for item in current["symbols"].values()
        ))
        self.assertTrue(all(
            "quote_time_order" in item["quality_failures"]
            for item in coverage["sessions"][0]["symbols"].values()
        ))

    def test_forward_quality_rederives_legacy_quote_age_and_excludes_tca(self):
        start = datetime(2026, 8, 28, 15, 16, 30, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "forward.sqlite3"
            lock = Path(directory) / "forward.lock"
            for offset in (0, 1):
                now = start + timedelta(minutes=offset)
                api = self._api(now)
                for item in api.data.market_data.get_snapshot.return_value.json():
                    item["quote_time"] = int(
                        (now + timedelta(seconds=1)).timestamp() * 1000
                    )
                record_equity_forward_once(
                    api, now=now, database=database, lock_file=lock
                )
            with sqlite3.connect(database) as connection:
                connection.execute(
                    "UPDATE samples SET valid_quote = 1, quote_age_seconds = 1, "
                    "mid = (bid + ask) / 2, spread_bps = 1"
                )
            current = equity_forward_status(database)
            coverage = equity_session_coverage(database)
            with mock.patch(
                "equity_forward_recorder.status",
                return_value={
                    "decision": "PENDING",
                    "complete_session_count": 1,
                    "qualified_session_count": 1,
                    "qualified_sessions": [start.date().isoformat()],
                },
            ):
                diagnostics = equity_execution_diagnostics(database)

        self.assertTrue(all(
            item["negative_quote_age_count"] == 2
            and item["valid_quote_coverage"] == 0.0
            for item in current["symbols"].values()
        ))
        self.assertTrue(all(
            item["negative_quote_age_count"] == 2
            for item in coverage["sessions"][0]["symbols"].values()
        ))
        self.assertTrue(all(
            item["horizons"]["1"]["paired_observations"] == 0
            for item in diagnostics["symbols"].values()
        ))

    def test_forward_quality_rejects_late_or_future_bar_timestamps(self):
        now = datetime(2026, 8, 28, 15, 16, 30, tzinfo=timezone.utc)
        api = self._api(now)
        bar_time = now - timedelta(minutes=1, seconds=31)
        payload = api.data.market_data.get_batch_history_bar.return_value.json()
        for item in payload["result"]:
            item["result"][0]["time"] = bar_time.isoformat()
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "forward.sqlite3"
            record_equity_forward_once(
                api,
                now=now,
                database=database,
                lock_file=Path(directory) / "forward.lock",
            )
            delayed = equity_forward_status(database)
            self.assertTrue(all(
                item["p95_bar_close_lag_seconds"] == 31.0
                and not item["quality_checks"]["p95_bar_close_lag"]
                for item in delayed["symbols"].values()
            ))

            with sqlite3.connect(database) as connection:
                connection.execute(
                    "UPDATE samples SET request_time = ?",
                    ((bar_time + timedelta(seconds=30)).isoformat(),),
                )
            future = equity_forward_status(database)

        self.assertTrue(all(
            item["negative_bar_close_lag_count"] == 1
            and not item["quality_checks"]["bar_close_time_order"]
            for item in future["symbols"].values()
        ))

    def test_forward_launch_agent_has_no_order_command(self):
        payload = equity_forward_launch_payload()
        self.assertEqual(payload["Label"], EQUITY_FORWARD_LAUNCH_LABEL)
        command = " ".join(payload["ProgramArguments"])
        self.assertIn("forward-record-once", command)
        self.assertNotIn("order", command)
        self.assertNotIn("StartInterval", payload)
        self.assertEqual(
            {item["Minute"] for item in payload["StartCalendarInterval"]},
            set(range(60)),
        )

    def test_forward_awake_guard_is_ac_only_and_bounded_by_session(self):
        start = datetime(2026, 8, 28, 12, 25, tzinfo=timezone.utc)
        self.assertEqual(
            equity_forward_awake_guard_duration(start, time(16, 0)), 27900
        )
        self.assertEqual(
            equity_forward_awake_guard_duration(start, time(13, 0)), 17100
        )
        self.assertIsNone(
            equity_forward_awake_guard_duration(
                datetime(2026, 8, 28, 12, 24, tzinfo=timezone.utc), time(16, 0)
            )
        )
        with tempfile.TemporaryDirectory() as directory:
            pid_file = Path(directory) / "awake.pid"
            process = SimpleNamespace(pid=12345)
            with mock.patch(
                "equity_forward_recorder._awake_guard_status",
                return_value={"status": "inactive"},
            ), mock.patch(
                "equity_forward_recorder.subprocess.Popen", return_value=process
            ) as popen:
                started = ensure_equity_forward_awake_guard(
                    start, time(16, 0), pid_file
                )
                self.assertEqual(pid_file.read_text(encoding="utf-8"), "12345")

        self.assertTrue(started)
        command = popen.call_args.args[0]
        self.assertEqual(command, ["/usr/bin/caffeinate", "-s", "-t", "27900"])
        self.assertNotIn("-i", command)

    def test_forward_awake_guard_stop_only_targets_verified_pid(self):
        with tempfile.TemporaryDirectory() as directory:
            pid_file = Path(directory) / "awake.pid"
            pid_file.write_text("12345", encoding="utf-8")
            with mock.patch(
                "equity_forward_recorder._awake_guard_status",
                return_value={"status": "active", "pid": 12345},
            ), mock.patch("equity_forward_recorder.os.kill") as kill:
                stopped = stop_equity_forward_awake_guard(pid_file)

            self.assertTrue(stopped)
            kill.assert_called_once_with(12345, signal.SIGTERM)
            self.assertFalse(pid_file.exists())

    def test_forward_recorder_source_has_no_trade_mutation(self):
        source = (Path(__file__).parent / "equity_forward_recorder.py").read_text(encoding="utf-8")
        self.assertNotIn(".trade.", source)
        self.assertNotIn("place_order", source)
        self.assertNotIn("cancel_order", source)

    def test_complete_session_requires_aligned_valid_minutes(self):
        day = date(2026, 8, 28)
        start = datetime(day.year, day.month, day.day, 9, 30, tzinfo=EQUITY_FORWARD_EASTERN)
        rows = []
        timestamps = []
        for index in range(390):
            bar_time = (start + timedelta(minutes=index)).astimezone(timezone.utc)
            timestamps.append(bar_time.isoformat())
            for symbol in EQUITY_FORWARD_SYMBOLS:
                rows.append((
                    symbol,
                    bar_time.isoformat(),
                    (bar_time + timedelta(minutes=1)).isoformat(),
                    day.isoformat(),
                    "RTH",
                    100.0,
                    101.0,
                    99.0,
                    100.0,
                    1000.0,
                    100.0,
                    99.99,
                    100.01,
                    100.0,
                    100.0,
                    (bar_time + timedelta(seconds=59)).isoformat(),
                    100.0,
                    2.0,
                    1.0,
                    1,
                    1,
                    EQUITY_FORWARD_CAPTURE_PROTOCOL_VERSION,
                ))
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "forward.sqlite3"
            with sqlite3.connect(database) as connection:
                connection.executescript(EQUITY_FORWARD_SCHEMA)
                connection.executemany(EQUITY_FORWARD_INSERT_SAMPLE, rows)
            initial = equity_session_coverage(database)
            self.assertTrue(initial["sessions"][0]["complete"])
            self.assertTrue(initial["sessions"][0]["qualified"])
            self.assertEqual(initial["sessions"][0]["bar_timeliness"], "PASS")
            self.assertEqual(initial["sessions"][0]["p95_bar_close_lag_seconds"], 0.0)
            initial_status = equity_forward_status(database)
            self.assertEqual(initial_status["complete_session_count"], 1)
            self.assertEqual(initial_status["qualified_session_count"], 1)
            self.assertEqual(
                initial["sessions"][0]["capture_protocol_versions"],
                [EQUITY_FORWARD_CAPTURE_PROTOCOL_VERSION],
            )
            self.assertEqual(
                initial["sessions"][0]["capture_protocol_consistency"], "PASS"
            )
            self.assertEqual(
                initial["sessions"][0]["cross_symbol_capture_sync"], "PASS"
            )
            self.assertEqual(
                initial["sessions"][0]["maximum_cross_symbol_capture_skew_seconds"],
                0.0,
            )
            self.assertEqual(initial["max_cross_symbol_capture_skew_seconds"], 0.0)

            skewed_time = (
                datetime.fromisoformat(timestamps[100])
                + timedelta(minutes=1, milliseconds=500)
            ).isoformat()
            with sqlite3.connect(database) as connection:
                connection.execute(
                    "UPDATE samples SET request_time = ? "
                    "WHERE symbol = 'AAPL' AND bar_time = ?",
                    (skewed_time, timestamps[100]),
                )
            skewed_capture = equity_session_coverage(database)["sessions"][0]
            self.assertTrue(skewed_capture["complete"])
            self.assertFalse(skewed_capture["qualified"])
            self.assertEqual(skewed_capture["cross_symbol_capture_sync"], "FAIL")
            self.assertEqual(
                skewed_capture["maximum_cross_symbol_capture_skew_seconds"], 0.5
            )
            self.assertEqual(
                skewed_capture["session_quality_failures"],
                ["cross_symbol_capture_sync"],
            )

            with sqlite3.connect(database) as connection:
                connection.execute(
                    "UPDATE samples SET request_time = ? "
                    "WHERE symbol = 'AAPL' AND bar_time = ?",
                    (
                        (
                            datetime.fromisoformat(timestamps[100])
                            + timedelta(minutes=1)
                        ).isoformat(),
                        timestamps[100],
                    ),
                )

            with sqlite3.connect(database) as connection:
                connection.execute(
                    "UPDATE samples SET capture_protocol_version = NULL "
                    "WHERE symbol = 'AAPL' AND bar_time = ?",
                    (timestamps[0],),
                )
            mixed_protocol = equity_session_coverage(database)["sessions"][0]
            self.assertTrue(mixed_protocol["complete"])
            self.assertFalse(mixed_protocol["qualified"])
            self.assertEqual(mixed_protocol["capture_protocol_consistency"], "FAIL")
            self.assertEqual(
                set(mixed_protocol["capture_protocol_versions"]),
                {EQUITY_FORWARD_CAPTURE_PROTOCOL_VERSION, "LEGACY_UNVERSIONED"},
            )
            self.assertEqual(
                mixed_protocol["session_quality_failures"],
                ["capture_protocol_consistent"],
            )

            with sqlite3.connect(database) as connection:
                connection.execute(
                    "UPDATE samples SET capture_protocol_version = ?",
                    (EQUITY_FORWARD_CAPTURE_PROTOCOL_VERSION,),
                )

            with sqlite3.connect(database) as connection:
                connection.executemany(
                    "UPDATE samples SET valid_quote = 0 "
                    "WHERE symbol = 'AAPL' AND bar_time = ?",
                    [(timestamp,) for timestamp in timestamps[:4]],
                )
            quote_rejected = equity_session_coverage(database)
            quote_rejected_status = equity_forward_status(database)
            rejected_session = quote_rejected["sessions"][0]
            self.assertTrue(rejected_session["complete"])
            self.assertFalse(rejected_session["qualified"])
            self.assertEqual(quote_rejected["complete_session_count"], 1)
            self.assertEqual(quote_rejected["qualified_session_count"], 0)
            self.assertEqual(
                quote_rejected["rejected_complete_sessions"], [day.isoformat()]
            )
            self.assertEqual(
                rejected_session["quality_failures"],
                {"AAPL": ["valid_quote_coverage"]},
            )
            self.assertEqual(quote_rejected_status["status"], "recording")
            self.assertEqual(quote_rejected_status["decision"], "PENDING")
            self.assertEqual(quote_rejected_status["qualified_session_count"], 0)

            with sqlite3.connect(database) as connection:
                connection.execute("UPDATE samples SET valid_quote = 1")

            with sqlite3.connect(database) as connection:
                connection.executemany(
                    "UPDATE samples SET valid_bar = 0 WHERE symbol = 'AAPL' AND bar_time = ?",
                    [(timestamp,) for timestamp in timestamps[:20]],
                )
            invalid = equity_session_coverage(database)
            self.assertEqual(invalid["sessions"][0]["aligned_valid_minutes"], 370)
            self.assertFalse(invalid["sessions"][0]["complete"])
            self.assertEqual(equity_forward_status(database)["complete_session_count"], 0)
            self.assertEqual(equity_forward_status(database)["qualified_session_count"], 0)

            with sqlite3.connect(database) as connection:
                connection.execute("UPDATE samples SET valid_bar = 1")
                connection.executemany(
                    "DELETE FROM samples WHERE bar_time = ?",
                    [(timestamps[200],)],
                )
            isolated_gap = equity_session_coverage(database)["sessions"][0]
            self.assertTrue(isolated_gap["complete"])
            self.assertTrue(isolated_gap["qualified"])
            self.assertEqual(isolated_gap["maximum_internal_gap_minutes"], 1)
            self.assertEqual(isolated_gap["gap_tolerance"], "PASS")

            with sqlite3.connect(database) as connection:
                connection.execute(
                    "DELETE FROM samples WHERE bar_time = ?", (timestamps[201],)
                )
            internal_gap = equity_session_coverage(database)["sessions"][0]
            self.assertEqual(internal_gap["internal_continuity"], "FAIL")
            self.assertEqual(internal_gap["internal_missing_minutes"], 2)
            self.assertEqual(internal_gap["maximum_internal_gap_minutes"], 2)
            self.assertTrue(internal_gap["complete"])
            self.assertFalse(internal_gap["qualified"])
            self.assertEqual(internal_gap["gap_tolerance"], "FAIL")
            self.assertEqual(
                internal_gap["session_quality_failures"],
                ["maximum_consecutive_internal_gap"],
            )

            with sqlite3.connect(database) as connection:
                connection.executemany(
                    EQUITY_FORWARD_INSERT_SAMPLE,
                    [row for row in rows if row[1] in timestamps[200:202]],
                )
                connection.executemany(
                    "DELETE FROM samples WHERE bar_time = ?",
                    [(timestamps[0],), (timestamps[-1],)],
                )
            anchor_gap = equity_session_coverage(database)["sessions"][0]
            self.assertTrue(anchor_gap["complete"])
            self.assertFalse(anchor_gap["qualified"])
            self.assertEqual(anchor_gap["opening_anchor"], "FAIL")
            self.assertEqual(anchor_gap["closing_anchor"], "FAIL")
            self.assertEqual(anchor_gap["opening_anchor_missing_examples_et"], ["09:30"])
            self.assertEqual(anchor_gap["closing_anchor_missing_examples_et"], ["15:59"])

            with sqlite3.connect(database) as connection:
                connection.executemany(
                    EQUITY_FORWARD_INSERT_SAMPLE,
                    [row for row in rows if row[1] in (timestamps[0], timestamps[-1])],
                )
                for symbol, removed in zip(
                    EQUITY_FORWARD_SYMBOLS,
                    (timestamps[0:19], timestamps[19:38], timestamps[38:57]),
                ):
                    connection.executemany(
                        "DELETE FROM samples WHERE symbol = ? AND bar_time = ?",
                        [(symbol, timestamp) for timestamp in removed],
                    )
            coverage = equity_session_coverage(database)
            session = coverage["sessions"][0]
            current = equity_forward_status(database)

        self.assertTrue(all(
            item["observed_minutes"] == 371 for item in session["symbols"].values()
        ))
        self.assertEqual(session["aligned_valid_minutes"], 333)
        self.assertFalse(session["complete"])
        self.assertEqual(coverage["complete_session_count"], 0)
        self.assertEqual(coverage["qualified_session_count"], 0)
        self.assertEqual(current["complete_session_count"], 0)
        self.assertEqual(current["qualified_session_count"], 0)
        self.assertFalse(coverage["orders_enabled"])

    def test_forward_recorder_stops_only_after_qualified_target(self):
        now = datetime(2026, 8, 28, 15, 16, 30, tzinfo=timezone.utc)
        api = mock.Mock(spec=[])
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "forward.sqlite3"
            with (
                mock.patch(
                    "equity_forward_recorder.session_coverage",
                    return_value={"qualified_session_count": 20},
                ),
                mock.patch(
                    "equity_forward_recorder.status",
                    return_value={
                        "decision": "DATA_USABLE",
                        "complete_session_count": 21,
                        "qualified_session_count": 20,
                    },
                ),
            ):
                result = record_equity_forward_once(
                    api,
                    now=now,
                    database=database,
                    lock_file=Path(directory) / "forward.lock",
                )
        self.assertEqual(result["outcome"], "complete")
        self.assertEqual(result["recorded"], 0)
        self.assertEqual(result["qualified_session_count"], 20)

    def test_forward_tca_uses_exact_same_session_horizons(self):
        start = datetime(2026, 8, 28, 15, 16, 30, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "forward.sqlite3"
            lock = Path(directory) / "forward.lock"
            for offset in (0, 1, 5):
                now = start + timedelta(minutes=offset)
                record_equity_forward_once(
                    self._api(now), now=now, database=database, lock_file=lock
                )
            unqualified = equity_execution_diagnostics(database)
            with mock.patch(
                "equity_forward_recorder.status",
                return_value={
                    "decision": "PENDING",
                    "complete_session_count": 1,
                    "qualified_session_count": 1,
                    "qualified_sessions": [start.date().isoformat()],
                },
            ):
                diagnostics = equity_execution_diagnostics(database)
        self.assertTrue(all(
            metrics["paired_observations"] == 0
            for item in unqualified["symbols"].values()
            for metrics in item["horizons"].values()
        ))
        self.assertEqual(unqualified["sample_scope"], "QUALIFIED_SESSIONS_ONLY")
        spy = diagnostics["symbols"]["SPY"]["horizons"]
        self.assertEqual(spy["1"]["paired_observations"], 1)
        self.assertEqual(spy["5"]["paired_observations"], 1)
        self.assertEqual(spy["30"]["paired_observations"], 0)
        self.assertGreater(spy["1"]["p95_round_trip_quoted_cost_bps"], 0)
        self.assertEqual(
            spy["1"]["minimum_required_gross_edge_bps"],
            round(spy["1"]["p95_round_trip_quoted_cost_bps"] + 2.0, 6),
        )
        self.assertLess(spy["1"]["median_long_executable_return_bps"], 0)
        self.assertEqual(diagnostics["qualified_sessions"], [start.date().isoformat()])
        self.assertEqual(diagnostics["interpretation"], "EXECUTION_DIAGNOSTIC_ONLY")
        self.assertFalse(diagnostics["orders_enabled"])


class EquityDeskTests(unittest.TestCase):
    @staticmethod
    def _response(data, status=200):
        return SimpleNamespace(status_code=status, json=lambda: data)

    def _api(self):
        accounts = [
            {
                "account_id": "margin-id",
                "account_number": "DEA7TE64",
                "account_class": "INDIVIDUAL_MARGIN",
            },
            {
                "account_id": "crypto-id",
                "account_number": "DEA7TE86",
                "account_class": "CRYPTO",
            },
        ]
        balances = {
            "margin-id": {
                "total_net_liquidation_value": "1000010",
                "total_market_value": "320",
                "account_currency_assets": [{"day_buying_power": "3999000"}],
            },
            "crypto-id": {
                "total_net_liquidation_value": "0",
                "total_market_value": "0",
                "account_currency_assets": [{"buying_power": "1000000"}],
            },
        }
        positions = {
            "margin-id": [{
                "symbol": "AAPL",
                "instrument_type": "EQUITY",
                "quantity": "1",
                "cost_price": "310.52",
                "market_value": "320",
                "unrealized_profit_loss": "9.48",
            }],
            "crypto-id": [],
        }
        account_v2 = SimpleNamespace(
            get_account_balance=mock.Mock(
                side_effect=lambda account_id: self._response(balances[account_id])
            ),
            get_account_position=mock.Mock(
                side_effect=lambda account_id: self._response(positions[account_id])
            ),
        )
        order_v3 = SimpleNamespace(
            get_order_open=mock.Mock(side_effect=lambda *_args, **_kwargs: self._response([]))
        )
        return SimpleNamespace(
            accounts=mock.Mock(return_value=accounts),
            trade=SimpleNamespace(account_v2=account_v2, order_v3=order_v3),
        )

    def test_desk_status_blocks_unmanaged_position_and_remains_read_only(self):
        api = self._api()
        crypto = {
            "status": "running",
            "state": {"paused": True, "pending_orders": {}, "submitted_order_ids": []},
            "launch_agent": {"installed": True},
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = equity_desk_status(
                api,
                database=root / "missing.sqlite3",
                journal=root / "missing.jsonl",
                now=datetime(2026, 8, 28, 15, 0, tzinfo=timezone.utc),
                crypto_automation=crypto,
            )
        authorization = result["automatic_equity_trading"]
        self.assertEqual(result["authorization_level"], "DATA_COLLECTION")
        self.assertEqual(authorization["status"], "BLOCKED")
        self.assertIn("forward_data_pending", authorization["reasons"])
        self.assertIn("unmanaged_equity_positions", authorization["reasons"])
        self.assertNotIn("desk_journal_unreadable", authorization["reasons"])
        self.assertNotIn("desk_journal_incomplete", authorization["reasons"])
        self.assertEqual(authorization["blocked_symbols"], ["AAPL"])
        self.assertTrue(result["legacy_crypto_automation"]["paused"])
        self.assertFalse(result["execution_authorization"]["new_entries_authorized"])
        self.assertFalse(result["orders_enabled"])
        self.assertEqual(api.trade.account_v2.get_account_position.call_count, 2)
        self.assertEqual(api.trade.order_v3.get_order_open.call_count, 2)

    def test_desk_status_fails_closed_when_account_read_raises(self):
        api = self._api()
        api.trade.account_v2.get_account_balance.side_effect = RuntimeError("unavailable")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = equity_desk_status(
                api,
                database=root / "missing.sqlite3",
                journal=root / "missing.jsonl",
                now=datetime(2026, 8, 28, 15, 0, tzinfo=timezone.utc),
                crypto_automation={
                    "state_readable": False,
                    "launch_agent": {"installed": True},
                },
            )
        self.assertIn(
            "account_state_unreadable", result["automatic_equity_trading"]["reasons"]
        )
        self.assertIn(
            "legacy_crypto_state_unreadable",
            result["automatic_equity_trading"]["reasons"],
        )
        self.assertTrue(all(not account["readable"] for account in result["accounts"]))

    def test_desk_status_blocks_incomplete_or_unreadable_journal(self):
        api = self._api()
        now = datetime(2026, 11, 27, 18, 3, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as directory, mock.patch(
            "equity_desk.DESK_JOURNAL_REQUIRED_FROM", date(2026, 11, 27)
        ):
            root = Path(directory)
            journal = root / "desk.jsonl"
            journal.write_text("not-json\n", encoding="utf-8")
            unreadable = equity_desk_status(
                api,
                database=root / "missing.sqlite3",
                journal=journal,
                now=now,
            )
            journal.write_text("", encoding="utf-8")
            incomplete = equity_desk_status(
                api,
                database=root / "missing.sqlite3",
                journal=journal,
                now=now,
            )
            records = [
                {
                    "record_key": f"2026-11-27:{phase}",
                    "recorded_at": now.isoformat(),
                    "session_day": "2026-11-27",
                    "phase": phase,
                    "desk": {
                        "authorization_level": "DATA_COLLECTION",
                        "orders_enabled": False,
                    },
                }
                for phase in ("pre_open", "post_close")
            ]
            journal.write_text(
                "".join(json.dumps(record) + "\n" for record in records),
                encoding="utf-8",
            )
            complete = equity_desk_status(
                api,
                database=root / "missing.sqlite3",
                journal=journal,
                now=now,
            )

        self.assertIn(
            "desk_journal_unreadable",
            unreadable["automatic_equity_trading"]["reasons"],
        )
        self.assertIn(
            "desk_journal_incomplete",
            incomplete["automatic_equity_trading"]["reasons"],
        )
        self.assertEqual(
            incomplete["automatic_equity_trading"]["next_gate"],
            "repair_desk_journal",
        )
        self.assertEqual(incomplete["desk_journal"]["decision"], "BLOCKED")
        complete_reasons = complete["automatic_equity_trading"]["reasons"]
        self.assertNotIn("desk_journal_unreadable", complete_reasons)
        self.assertNotIn("desk_journal_incomplete", complete_reasons)
        self.assertEqual(
            complete["automatic_equity_trading"]["next_gate"],
            "qualified_forward_data",
        )
        self.assertEqual(complete["desk_journal"]["decision"], "AUDIT_OK")

    def test_desk_source_has_no_mutating_order_calls(self):
        source = (Path(__file__).parent / "equity_desk.py").read_text(encoding="utf-8")
        self.assertNotIn("place_order", source)
        self.assertNotIn("cancel_order", source)
        self.assertNotIn("replace_order", source)

    def test_research_ledger_counts_every_attempt_and_report(self):
        result = equity_research_status()
        self.assertTrue(result["readable"])
        self.assertEqual(result["attempt_count"], 12)
        self.assertEqual(result["strategy_family_count"], 9)
        self.assertEqual(result["deployable_count"], 0)
        self.assertEqual(result["consumed_holdout_count"], 7)
        self.assertEqual(result["non_independent_count"], 2)
        self.assertEqual(result["unrequested_holdout_count"], 3)
        self.assertEqual(result["next_candidate_budget"], 1)
        self.assertFalse(result["parameter_search_allowed"])
        ledger = json.loads(RESEARCH_LEDGER.read_text(encoding="utf-8"))
        root = Path(__file__).parent
        self.assertTrue(all((root / item["report"]).exists() for item in ledger["attempts"]))

    def test_research_ledger_fails_closed_when_missing(self):
        with tempfile.TemporaryDirectory() as directory:
            result = equity_research_status(Path(directory) / "missing.json")
        self.assertFalse(result["readable"])
        self.assertEqual(result["decision"], "BLOCKED")

    def test_desk_journal_is_idempotent_per_session_phase(self):
        api = self._api()
        crypto = {
            "status": "running",
            "state": {"paused": True, "pending_orders": {}, "submitted_order_ids": []},
            "launch_agent": {"installed": True},
        }
        now = datetime(2026, 8, 28, 15, 45, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            arguments = {
                "now": now,
                "database": root / "missing.sqlite3",
                "journal": root / "desk.jsonl",
                "lock_file": root / "desk.lock",
                "crypto_automation": crypto,
            }
            first = record_desk_snapshot(api, **arguments)
            second = record_desk_snapshot(api, **arguments)
            summary = desk_journal_status(arguments["journal"])
            lines = arguments["journal"].read_text(encoding="utf-8").splitlines()
        self.assertEqual(first["outcome"], "recorded")
        self.assertEqual(second["outcome"], "duplicate_phase")
        self.assertEqual(first["record"]["record_key"], "2026-08-28:regular_hours")
        self.assertEqual(len(lines), 1)
        self.assertTrue(summary["readable"])
        self.assertEqual(summary["entry_count"], 1)
        self.assertEqual(summary["phase_counts"], {"regular_hours": 1})
        self.assertEqual(summary["authorization_counts"], {"DATA_COLLECTION": 1})
        self.assertEqual(api.trade.account_v2.get_account_balance.call_count, 2)
        self.assertEqual(api.trade.account_v2.get_account_position.call_count, 2)
        self.assertEqual(api.trade.order_v3.get_order_open.call_count, 2)

    def test_desk_journal_phase_uses_frozen_nyse_calendar(self):
        self.assertEqual(
            equity_desk_session_phase(
                datetime(2026, 9, 7, 15, 0, tzinfo=timezone.utc)
            ),
            ("2026-09-07", "closed"),
        )
        self.assertEqual(
            equity_desk_session_phase(
                datetime(2026, 11, 27, 17, 59, tzinfo=timezone.utc)
            ),
            ("2026-11-27", "regular_hours"),
        )
        self.assertEqual(
            equity_desk_session_phase(
                datetime(2026, 11, 27, 18, 0, tzinfo=timezone.utc)
            ),
            ("2026-11-27", "post_close"),
        )

    def test_forward_command_records_market_data_and_desk_journal(self):
        recorder = {
            "outcome": "recorded",
            "recorded": 3,
            "decision": "PENDING",
            "complete_session_count": 0,
            "qualified_session_count": 0,
        }
        journal = {
            "outcome": "recorded",
            "record": {"record_key": "2026-08-28:regular_hours"},
        }
        with mock.patch(
            "equity_forward_recorder.record_once", return_value=recorder
        ) as record, mock.patch(
            "equity_desk.record_desk_snapshot", return_value=journal
        ) as journal_record, mock.patch(
            "webull_cli.emit", side_effect=lambda output: output
        ):
            output = cmd_forward_record_once(SimpleNamespace(), SimpleNamespace())

        record.assert_called_once()
        journal_record.assert_called_once()
        self.assertEqual(output["desk_journal_outcome"], "recorded")
        self.assertEqual(
            output["desk_journal_record_key"], "2026-08-28:regular_hours"
        )

    def test_desk_journal_corruption_blocks_status(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = Path(directory) / "desk.jsonl"
            self.assertEqual(desk_journal_status(journal)["decision"], "NOT_STARTED")
            journal.write_text("not-json\n", encoding="utf-8")
            result = desk_journal_status(journal)
        self.assertFalse(result["readable"])
        self.assertEqual(result["decision"], "BLOCKED")
        self.assertEqual(result["invalid_lines"], [1])

    def test_desk_journal_blocks_missing_early_close_phases(self):
        def record(phase):
            return {
                "record_key": f"2026-11-27:{phase}",
                "recorded_at": "2026-11-27T18:03:00+00:00",
                "session_day": "2026-11-27",
                "phase": phase,
                "desk": {
                    "authorization_level": "DATA_COLLECTION",
                    "orders_enabled": False,
                },
            }

        now = datetime(2026, 11, 27, 18, 3, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as directory, mock.patch(
            "equity_desk.DESK_JOURNAL_REQUIRED_FROM", date(2026, 11, 27)
        ):
            journal = Path(directory) / "desk.jsonl"
            journal.write_text(
                json.dumps(record("pre_open")) + "\n", encoding="utf-8"
            )
            incomplete = desk_journal_status(journal, now=now)
            with journal.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record("post_close")) + "\n")
            complete = desk_journal_status(journal, now=now)

        self.assertEqual(incomplete["decision"], "BLOCKED")
        self.assertEqual(incomplete["checked_session_count"], 1)
        self.assertEqual(
            incomplete["missing_required_phase_sessions"][0]["missing_phases"],
            ["post_close"],
        )
        self.assertEqual(complete["decision"], "AUDIT_OK")
        self.assertEqual(complete["compliant_session_count"], 1)

    def test_desk_journal_does_not_require_closed_holiday(self):
        now = datetime(2026, 9, 7, 21, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as directory, mock.patch(
            "equity_desk.DESK_JOURNAL_REQUIRED_FROM", date(2026, 9, 7)
        ):
            result = desk_journal_status(Path(directory) / "missing.jsonl", now=now)

        self.assertEqual(result["decision"], "NOT_STARTED")
        self.assertEqual(result["checked_session_count"], 0)
        self.assertEqual(result["missing_required_phase_sessions"], [])

    def test_desk_status_fails_closed_beyond_frozen_nyse_calendar(self):
        api = self._api()
        now = datetime(2029, 1, 2, 15, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as directory, mock.patch(
            "equity_desk.DESK_JOURNAL_REQUIRED_FROM", date(2029, 1, 2)
        ):
            root = Path(directory)
            result = equity_desk_status(
                api,
                database=root / "missing.sqlite3",
                journal=root / "missing.jsonl",
                now=now,
            )

        journal = result["desk_journal"]
        automatic = result["automatic_equity_trading"]
        self.assertEqual(journal["decision"], "BLOCKED")
        self.assertEqual(journal["calendar_unsupported_day_count"], 1)
        self.assertEqual(journal["first_calendar_unsupported_day"], "2029-01-02")
        self.assertIn("desk_journal_calendar_unsupported", automatic["reasons"])
        self.assertEqual(automatic["next_gate"], "extend_nyse_calendar")

    def test_desk_journal_last_supported_year_is_not_calendar_error(self):
        now = datetime(2028, 12, 26, 15, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as directory, mock.patch(
            "equity_desk.DESK_JOURNAL_REQUIRED_FROM", date(2028, 12, 26)
        ):
            result = desk_journal_status(
                Path(directory) / "missing.jsonl", now=now
            )

        self.assertEqual(result["calendar_unsupported_day_count"], 0)
        self.assertIsNone(result["first_calendar_unsupported_day"])
        self.assertEqual(
            result["missing_required_phase_sessions"][0]["missing_phases"],
            ["pre_open"],
        )


class EquityOrbStrategyTests(unittest.TestCase):
    def _session(self, day: date, opening_volume: int, *, bars: int = 78) -> Session:
        start = datetime(day.year, day.month, day.day, 14, 30, tzinfo=timezone.utc)
        values = tuple(
            Bar(
                start + timedelta(minutes=5 * index),
                Decimal("100"),
                Decimal("101"),
                Decimal("99"),
                Decimal("100"),
                Decimal(opening_volume if index == 0 else 1000),
            )
            for index in range(bars)
        )
        return Session(day, values)

    def test_orb_features_use_only_prior_opening_volume(self):
        sessions = [
            self._session(date(2026, 1, 1) + timedelta(days=index), 100 if index < 14 else 10000)
            for index in range(15)
        ]
        feature = orb_features(sessions)[sessions[-1].day]
        self.assertEqual(feature["average_opening_volume"], Decimal("100"))

    def test_m1_session_aggregates_first_five_minutes(self):
        start = datetime(2026, 1, 5, 14, 30, tzinfo=timezone.utc)
        bars = [
            Bar(
                start + timedelta(minutes=index),
                Decimal("100"), Decimal(str(101 + index)), Decimal("99"), Decimal("100"), Decimal("10"),
            )
            for index in range(390)
        ]
        sessions, quality = build_stock_sessions(bars, interval_seconds=60)
        opening, count = _opening_bar(sessions[0])
        self.assertTrue(quality.passed is False)
        self.assertEqual(count, 5)
        self.assertEqual(opening.high, Decimal("105"))
        self.assertEqual(opening.volume, Decimal("50"))

    def test_orb_same_bar_entry_and_stop_uses_conservative_stop(self):
        opening = Bar(
            datetime(2026, 1, 5, 14, 30, tzinfo=timezone.utc),
            Decimal("100"), Decimal("102"), Decimal("99"), Decimal("101"), Decimal("1000"),
        )
        breakout = Bar(
            opening.time + timedelta(minutes=5),
            Decimal("101"), Decimal("103"), Decimal("100.9"), Decimal("102"), Decimal("1000"),
        )
        close = Bar(
            opening.time + timedelta(hours=6, minutes=25),
            Decimal("102"), Decimal("102"), Decimal("102"), Decimal("102"), Decimal("1000"),
        )
        trade = trade_orb_session(
            "AAPL", Session(date(2026, 1, 5), (opening, breakout, close)),
            Decimal("2"), Decimal("10"), Decimal("1000000"), Decimal("0"),
        )
        self.assertEqual(trade.reason, "stop")
        self.assertEqual(trade.entry_price, Decimal("102"))
        self.assertEqual(trade.exit_price, Decimal("101"))
        self.assertEqual(trade.r_multiple, Decimal("-1"))

    def test_orb_quantity_respects_risk_and_notional_caps(self):
        self.assertEqual(orb_quantity(Decimal("1000000"), Decimal("100"), Decimal("1")), 500)
        self.assertEqual(orb_quantity(Decimal("1000000"), Decimal("100"), Decimal("0.01")), 1000)

    def test_stock_sessions_accept_complete_rth_and_drop_partial_boundary(self):
        complete = self._session(date(2026, 1, 5), 100).bars
        partial = self._session(date(2026, 1, 6), 100, bars=2).bars
        sessions, quality = build_stock_sessions([*complete, *partial])
        self.assertEqual(len(sessions), 1)
        self.assertEqual(quality.dropped_boundary_sessions, 1)

    def test_stock_sessions_trim_early_close_postmarket_bar(self):
        from equity_orb_strategy import EASTERN

        start = datetime(2023, 7, 3, 9, 30, tzinfo=EASTERN)
        bars = [
            Bar(
                (start + timedelta(minutes=index * 5)).astimezone(timezone.utc),
                Decimal("100"), Decimal("100"), Decimal("100"), Decimal("100"), Decimal("1"),
            )
            for index in range(43)
        ]
        sessions, quality = build_stock_sessions(bars)
        self.assertEqual(len(sessions), 1)
        self.assertEqual(len(sessions[0].bars), 42)
        self.assertEqual(sessions[0].bars[-1].time.astimezone(EASTERN).strftime("%H:%M"), "12:55")
        self.assertEqual(quality.incomplete_middle_sessions, ())

    def test_webull_stock_bars_uses_bounded_paginated_request(self):
        class MarketData:
            def __init__(self):
                self.calls = []

            def get_batch_history_bar(self, symbols, category, timespan, count, **kwargs):
                self.calls.append((symbols, category, timespan, count, kwargs))
                day = "2026-01-02" if len(self.calls) == 1 else "2026-01-01"
                rows = [{"time": f"{day}T00:00:00.000+0000", "open": "100", "high": "101", "low": "99", "close": "100", "volume": "1"}]
                return FakeResponse(200, {"result": [{"symbol": "QQQ", "result": rows}]})

        market = MarketData()
        api = SimpleNamespace(data=SimpleNamespace(market_data=market))
        with tempfile.TemporaryDirectory() as directory:
            bars = webull_stock_bars(
                api,
                symbols=("QQQ",),
                days=2,
                now=datetime(2026, 1, 3, tzinfo=timezone.utc),
                cache_dir=Path(directory),
            )
        self.assertEqual(len(market.calls), 2)
        self.assertEqual(market.calls[0][1:4], ("US_STOCK", "M5", "1200"))
        self.assertEqual(market.calls[1][4]["end_time"], 1767312000000)
        self.assertEqual(len(bars["QQQ"]), 2)
        self.assertEqual(ORB_BASE_COST, Decimal("0.0005"))

    def test_stock_pagination_uses_latest_symbol_page_tail(self):
        class MarketData:
            def __init__(self):
                self.calls = []

            def get_batch_history_bar(self, symbols, category, timespan, count, **kwargs):
                self.calls.append(kwargs["end_time"])
                if len(self.calls) == 1:
                    rows = {
                        "QQQ": [{"time": "2026-01-02T00:00:00.000+0000", "open": "100", "high": "101", "low": "99", "close": "100", "volume": "1"}],
                        "XOM": [{"time": "2026-01-01T00:00:00.000+0000", "open": "100", "high": "101", "low": "99", "close": "100", "volume": "1"}],
                    }
                else:
                    rows = {
                        symbol: [{"time": "2026-01-01T00:00:00.000+0000", "open": "100", "high": "101", "low": "99", "close": "100", "volume": "1"}]
                        for symbol in symbols
                    }
                return FakeResponse(200, {"result": [{"symbol": symbol, "result": rows[symbol]} for symbol in symbols]})

        market = MarketData()
        api = SimpleNamespace(data=SimpleNamespace(market_data=market))
        with tempfile.TemporaryDirectory() as directory:
            webull_stock_bars(
                api,
                symbols=("QQQ", "XOM"),
                days=2,
                now=datetime(2026, 1, 3, tzinfo=timezone.utc),
                cache_dir=Path(directory),
            )
        self.assertEqual(len(market.calls), 2)
        self.assertEqual(market.calls[1], 1767312000000)

    def test_stock_bars_can_require_an_existing_cache(self):
        market = mock.Mock()
        api = SimpleNamespace(data=SimpleNamespace(market_data=market))
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(FileNotFoundError):
                webull_stock_bars(
                    api,
                    symbols=("QQQ",),
                    days=2,
                    now=datetime(2026, 1, 3, tzinfo=timezone.utc),
                    cache_dir=Path(directory),
                    require_cache=True,
                )
        market.get_batch_history_bar.assert_not_called()

    def test_stock_bars_support_forward_adjusted_daily_data(self):
        market = mock.Mock()
        market.get_batch_history_bar.return_value = FakeResponse(200, {
            "result": [{
                "symbol": "QQQ",
                "result": [{
                    "time": "2026-01-01T00:00:00.000+0000",
                    "open": "100",
                    "high": "101",
                    "low": "99",
                    "close": "100",
                    "volume": "1",
                }],
            }],
        })
        api = SimpleNamespace(data=SimpleNamespace(market_data=market))
        with tempfile.TemporaryDirectory() as directory:
            bars = webull_stock_bars(
                api,
                symbols=("QQQ",),
                days=2,
                now=datetime(2026, 1, 3, tzinfo=timezone.utc),
                timespan="D",
                cache_dir=Path(directory),
            )
        self.assertEqual(len(bars["QQQ"]), 1)
        self.assertEqual(market.get_batch_history_bar.call_args.args[2], "D")


class IntradayMomentumStrategyTests(unittest.TestCase):
    def _session(
        self,
        day: date,
        *,
        first_end: Decimal = Decimal("100"),
        entry: Decimal = Decimal("100"),
        final: Decimal = Decimal("100"),
        bars: int = 78,
    ) -> Session:
        from equity_orb_strategy import EASTERN

        start = datetime(day.year, day.month, day.day, 9, 30, tzinfo=EASTERN)
        values = []
        for index in range(bars):
            price = Decimal("100")
            open_price = entry if index == 72 else price
            close_price = first_end if index == 5 else final if index == 77 else price
            values.append(Bar(
                (start + timedelta(minutes=5 * index)).astimezone(timezone.utc),
                open_price,
                max(open_price, close_price),
                min(open_price, close_price),
                close_price,
                Decimal("1000"),
            ))
        return Session(day, tuple(values))

    def test_signal_uses_previous_close_and_fixed_half_hours(self):
        previous = self._session(date(2026, 1, 5), final=Decimal("100"))
        current = self._session(
            date(2026, 1, 6),
            first_end=Decimal("102"),
            entry=Decimal("101"),
            final=Decimal("103"),
        )
        item = intraday_momentum_observations([previous, current])[0]
        self.assertEqual(item.first_return, Decimal("0.02"))
        self.assertEqual(item.entry, Decimal("101"))
        self.assertEqual(item.exit, Decimal("103"))

    def test_early_close_day_does_not_trade(self):
        previous = self._session(date(2026, 1, 5))
        early = self._session(date(2026, 1, 6), bars=42)
        self.assertEqual(intraday_momentum_observations([previous, early]), [])

    def test_long_and_short_pay_both_sides_of_cost(self):
        long_item = Observation(
            date(2026, 1, 6), Decimal("0.01"), Decimal("0.01"), Decimal("100"), Decimal("101")
        )
        short_item = Observation(
            date(2026, 1, 7), Decimal("-0.01"), Decimal("-0.01"), Decimal("100"), Decimal("99")
        )
        long_trade = execute_intraday_momentum(long_item, Decimal("1000000"), Decimal("0.001"))
        short_trade = execute_intraday_momentum(short_item, Decimal("1000000"), Decimal("0.001"))
        self.assertEqual(long_trade.quantity, 1000)
        self.assertEqual(long_trade.pnl, Decimal("799.000"))
        self.assertEqual(short_trade.pnl, Decimal("801.000"))

    def test_backtest_reports_predictive_slope_and_costed_expectancy(self):
        items = [
            Observation(date(2026, 1, 5 + index), signal, outcome, Decimal("100"), Decimal("100") * (1 + outcome))
            for index, (signal, outcome) in enumerate((
                (Decimal("-0.02"), Decimal("-0.01")),
                (Decimal("-0.01"), Decimal("-0.005")),
                (Decimal("0.01"), Decimal("0.005")),
                (Decimal("0.02"), Decimal("0.01")),
            ))
        ]
        result = backtest_intraday_momentum(items, cost=Decimal("0.0001"), include_trades=False)
        self.assertGreater(result["net_profit"], 0)
        self.assertGreater(result["average_net_bps"], 0)
        self.assertGreater(result["regression_slope"], 0)
        self.assertNotIn("trades", result)


class NoiseAreaStrategyTests(unittest.TestCase):
    def _session(
        self,
        day: date,
        *,
        open_price: Decimal = Decimal("100"),
        checkpoint_close: Decimal = Decimal("100"),
        execution_prices: dict[int, Decimal] = None,
        final_close: Decimal = Decimal("100"),
    ) -> Session:
        from equity_orb_strategy import EASTERN

        execution_prices = execution_prices or {}
        start = datetime(day.year, day.month, day.day, 9, 30, tzinfo=EASTERN)
        values = []
        for index in range(78):
            bar_open = execution_prices.get(index, open_price)
            close = checkpoint_close if index == 5 else final_close if index == 77 else open_price
            values.append(Bar(
                (start + timedelta(minutes=index * 5)).astimezone(timezone.utc),
                bar_open,
                max(bar_open, close),
                min(bar_open, close),
                close,
                Decimal("1000"),
            ))
        return Session(day, tuple(values))

    def test_noise_checkpoint_uses_prior_14_sessions_and_next_open(self):
        history = [
            self._session(date(2024, 1, 2) + timedelta(days=index), checkpoint_close=Decimal("101"))
            for index in range(14)
        ]
        current = self._session(date(2024, 2, 1), checkpoint_close=Decimal("102"))
        first = noise_checkpoints(history, history[-1], current)[0]
        self.assertEqual(first.signal_index, 5)
        self.assertEqual(first.execution_index, 6)
        self.assertEqual(first.upper, Decimal("101.00"))
        self.assertEqual(first.target, 1)

    def test_dividend_adjusts_previous_close_anchor(self):
        history = [
            self._session(date(2024, 2, 20) + timedelta(days=index), open_price=Decimal("98.405"), checkpoint_close=Decimal("98.405"), final_close=Decimal("100"))
            for index in range(14)
        ]
        current = self._session(
            date(2024, 3, 15),
            open_price=Decimal("98.405"),
            checkpoint_close=Decimal("98.600"),
        )
        first = noise_checkpoints(history, history[-1], current)[0]
        self.assertEqual(NOISE_DIVIDENDS[current.day], Decimal("1.595"))
        self.assertLess(first.upper, Decimal("99"))
        self.assertEqual(first.target, 1)

    def test_target_changes_execute_at_next_bar_and_charge_each_order(self):
        session = self._session(
            date(2024, 2, 1),
            execution_prices={6: Decimal("101"), 12: Decimal("99")},
            final_close=Decimal("98"),
        )
        trades = execute_noise_targets(
            session,
            [(6, 1), (12, -1)],
            quantity=100,
            cost_per_share=Decimal("0.10"),
        )
        self.assertEqual([(item.side, item.entry_price, item.exit_price) for item in trades], [
            ("LONG", Decimal("101"), Decimal("99")),
            ("SHORT", Decimal("99"), Decimal("98")),
        ])
        self.assertEqual([item.pnl for item in trades], [Decimal("-220.00"), Decimal("80.00")])
        self.assertEqual([item.holding_minutes for item in trades], [30, 330])


class RelativeValueStrategyTests(unittest.TestCase):
    def _session(
        self,
        day: date,
        *,
        closes: dict[int, Decimal] = None,
        opens: dict[int, Decimal] = None,
    ) -> Session:
        from equity_orb_strategy import EASTERN

        closes = closes or {}
        opens = opens or {}
        start = datetime(day.year, day.month, day.day, 9, 30, tzinfo=EASTERN)
        bars = []
        for index in range(78):
            bar_open = opens.get(index, Decimal("100"))
            close = closes.get(index, Decimal("100"))
            bars.append(Bar(
                (start + timedelta(minutes=index * 5)).astimezone(timezone.utc),
                bar_open,
                max(bar_open, close),
                min(bar_open, close),
                close,
                Decimal("1000"),
            ))
        return Session(day, tuple(bars))

    def test_pair_z_score_uses_prior_twenty_sessions(self):
        history = []
        for index in range(20):
            day = date(2023, 1, 2) + timedelta(days=index)
            history.append((
                self._session(day, closes={5: Decimal("100") + Decimal(index) / Decimal("100")}),
                self._session(day),
            ))
        current = (
            self._session(date(2023, 2, 1), closes={5: Decimal("101")}),
            self._session(date(2023, 2, 1)),
        )
        signal_index, execution_index, z_score = pair_checkpoint_z_scores(history, current)[0]
        self.assertEqual((signal_index, execution_index), (5, 6))
        self.assertGreater(z_score, 2)

    def test_pair_trade_executes_next_open_and_charges_four_orders(self):
        history = [(self._session(date(2023, 1, 2) + timedelta(days=index)), self._session(date(2023, 1, 2) + timedelta(days=index))) for index in range(20)]
        current = (
            self._session(date(2023, 2, 1), opens={6: Decimal("101"), 12: Decimal("100")}),
            self._session(date(2023, 2, 1)),
        )
        with mock.patch(
            "relative_value_strategy.checkpoint_z_scores",
            return_value=[(5, 6, 2.5), (11, 12, -0.1)],
        ):
            trade = trade_pair_day(
                history,
                current,
                equity=Decimal("1000000"),
                cost_per_share=Decimal("0.10"),
            )
        self.assertIsNotNone(trade)
        self.assertEqual(trade.direction, "SHORT_SPY_LONG_IVV")
        self.assertEqual((trade.spy_quantity, trade.ivv_quantity), (495, 500))
        self.assertEqual((trade.spy_entry, trade.spy_exit), (Decimal("101"), Decimal("100")))
        self.assertEqual(trade.pnl, Decimal("296.00"))
        self.assertEqual(trade.exit_reason, "convergence")

    def test_pair_eod_close_uses_end_of_final_bar(self):
        history = [(self._session(date(2023, 1, 2) + timedelta(days=index)), self._session(date(2023, 1, 2) + timedelta(days=index))) for index in range(20)]
        current = (
            self._session(date(2023, 2, 1), opens={6: Decimal("101")}),
            self._session(date(2023, 2, 1)),
        )
        with mock.patch(
            "relative_value_strategy.checkpoint_z_scores",
            return_value=[(5, 6, 2.5)],
        ):
            trade = trade_pair_day(
                history,
                current,
                equity=Decimal("1000000"),
                cost_per_share=Decimal("0"),
            )
        self.assertEqual(trade.exit_reason, "eod")
        self.assertEqual(trade.holding_minutes, 360)
        self.assertEqual(trade.exit_time.astimezone().minute, 0)


class ClassicSectorMomentumTests(unittest.TestCase):
    def _bars(self) -> dict[str, list[Bar]]:
        start = datetime(2019, 1, 1, tzinfo=timezone.utc)
        slopes = {symbol: Decimal("0.01") for symbol in CLASSIC_SECTOR_SYMBOLS}
        slopes.update({"XLB": Decimal("0.09"), "XLE": Decimal("0.08"), "XLF": Decimal("0.07")})
        output = {symbol: [] for symbol in CLASSIC_SECTOR_SYMBOLS}
        for index in range(450):
            timestamp = start + timedelta(days=index)
            for symbol in CLASSIC_SECTOR_SYMBOLS:
                close = Decimal("100") + slopes[symbol] * Decimal(index)
                open_price = Decimal("123") if symbol == "XLB" and timestamp.date() == date(2020, 2, 1) else close
                if symbol == "XLY" and timestamp.date() == date(2020, 2, 1):
                    close = Decimal("1000")
                output[symbol].append(Bar(
                    timestamp,
                    open_price,
                    max(open_price, close),
                    min(open_price, close),
                    close,
                    Decimal("1000"),
                ))
        return output

    def test_six_cohorts_allocate_thirty_percent(self):
        cohorts = [
            ("XLB", "XLE", "XLF"),
            ("XLB", "XLE", "XLI"),
            ("XLB", "XLK", "XLP"),
            ("XLE", "XLF", "XLU"),
            ("XLB", "XLV", "XLY"),
            ("XLE", "XLF", "XLI"),
        ]
        weights = classic_target_weights("momentum", cohorts)
        self.assertEqual(sum(weights.values(), Decimal("0")), Decimal("0.30"))
        self.assertEqual(weights["XLB"], (Decimal("0.30") / Decimal("18")) * Decimal("4"))

    def test_signal_uses_prior_closes_and_executes_at_current_open(self):
        result = simulate_classic_sector_momentum(
            self._bars(),
            start_day=date(2020, 2, 1),
            end_day=date(2020, 2, 1),
            mode="momentum",
            cost_model=CLASSIC_ZERO_COST,
        )
        selection = next(item for item in result["selection_history"] if item["day"] == "2020-02-01")
        self.assertEqual(selection["winners"], ["XLB", "XLE", "XLF"])
        xlb_buy = next(
            item for item in result["orders"]
            if item["symbol"] == "XLB" and item["quantity_delta"] > 0
        )
        self.assertEqual(xlb_buy["price"], 123.0)

    def test_failed_development_gate_preserves_sector_holdout(self):
        with (
            mock.patch("classic_sector_momentum.webull_stock_bars", return_value={}) as bars,
            mock.patch("classic_sector_momentum._stage", return_value={}),
            mock.patch(
                "classic_sector_momentum._gate",
                return_value={"passed": False, "checks": {}},
            ),
        ):
            report = run_classic_sector_momentum(mock.Mock())
        self.assertEqual(bars.call_count, 1)
        self.assertEqual(bars.call_args.kwargs["now"], DEVELOPMENT_FETCH_END)
        self.assertFalse(report["holdout_requested"])
        self.assertEqual(report["decision"], "REJECT_BEFORE_HOLDOUT")

    def test_failed_data_quality_stops_before_performance_calculation(self):
        with (
            mock.patch(
                "classic_sector_momentum._aligned",
                return_value=({}, {"passed": False}),
            ),
            mock.patch("classic_sector_momentum.simulate") as simulation,
        ):
            stage = classic_sector_stage(
                {},
                start_day=date(2000, 1, 3),
                end_day=date(2014, 12, 31),
                minimum_sessions=3750,
            )
        self.assertIsNone(stage["strategy"])
        self.assertIsNone(stage["benchmarks"])
        simulation.assert_not_called()


class IntermediateSectorMomentumTests(unittest.TestCase):
    def _bars(self) -> dict[str, list[Bar]]:
        start = datetime(2019, 1, 1, tzinfo=timezone.utc)
        output = {symbol: [] for symbol in CLASSIC_SECTOR_SYMBOLS}
        intermediate = {"XLB": Decimal("120"), "XLE": Decimal("119"), "XLF": Decimal("118")}
        for index in range(260):
            timestamp = start + timedelta(days=index)
            for symbol in CLASSIC_SECTOR_SYMBOLS:
                close = intermediate.get(symbol, Decimal("100")) if index == 126 else Decimal("100")
                if symbol == "XLY" and index == 252:
                    close = Decimal("1000")
                if symbol == "XLB" and index == 253:
                    close = Decimal("123")
                output[symbol].append(Bar(
                    timestamp,
                    close,
                    close,
                    close,
                    close,
                    Decimal("1000"),
                ))
        return output

    def test_intermediate_signal_skips_recent_returns_and_executes_at_close(self):
        bars = self._bars()
        result = simulate_intermediate_sector_momentum(
            bars,
            start_day=bars["SPY"][253].time.date(),
            end_day=bars["SPY"][254].time.date(),
            mode="momentum",
            cost_model=CLASSIC_ZERO_COST,
        )
        self.assertEqual(result["selections"][0]["winners"], ["XLB", "XLE", "XLF"])
        xlb_buy = next(
            item for item in result["orders"]
            if item["symbol"] == "XLB" and item["quantity_delta"] > 0
        )
        self.assertEqual(xlb_buy["close_proxy"], 123.0)

    def test_close_quality_ignores_open_but_rejects_invalid_close(self):
        bars = self._bars()
        xly = bars["XLY"][252]
        bars["XLY"][252] = Bar(
            xly.time,
            Decimal("100"),
            Decimal("100"),
            Decimal("100"),
            Decimal("100"),
            xly.volume,
        )
        original = bars["XLB"][10]
        bars["XLB"][10] = Bar(
            original.time,
            Decimal("999"),
            Decimal("101"),
            Decimal("99"),
            Decimal("100"),
            original.volume,
        )
        _, quality = align_intermediate_close(
            bars,
            quality_start=bars["SPY"][0].time.date(),
            minimum_sessions=250,
        )
        self.assertTrue(quality["passed"])
        bars["XLB"][10] = Bar(
            original.time,
            Decimal("999"),
            Decimal("101"),
            Decimal("99"),
            Decimal("200"),
            original.volume,
        )
        _, quality = align_intermediate_close(
            bars,
            quality_start=bars["SPY"][0].time.date(),
            minimum_sessions=250,
        )
        self.assertFalse(quality["passed"])
        self.assertEqual(quality["invalid_close"], [f"XLB:{original.time.date().isoformat()}"])

    def test_failed_development_gate_preserves_intermediate_holdout(self):
        with (
            mock.patch("intermediate_sector_momentum.webull_stock_bars", return_value={}) as bars,
            mock.patch("intermediate_sector_momentum._stage", return_value={}),
            mock.patch(
                "intermediate_sector_momentum._gate",
                return_value={"passed": False, "checks": {}},
            ),
        ):
            report = run_intermediate_sector_momentum(mock.Mock())
        self.assertEqual(bars.call_count, 1)
        self.assertTrue(bars.call_args.kwargs["require_cache"])
        self.assertEqual(bars.call_args.kwargs["now"], INTERMEDIATE_DEVELOPMENT_FETCH_END)
        self.assertFalse(report["holdout_requested"])
        self.assertEqual(report["decision"], "REJECT_BEFORE_HOLDOUT")


class OpeningPressureStrategyTests(unittest.TestCase):
    def _session(
        self,
        day: date,
        *,
        first_close: Decimal = Decimal("100"),
        opens: dict[int, Decimal] = None,
        closes: dict[int, Decimal] = None,
    ) -> Session:
        from equity_orb_strategy import EASTERN

        opens = opens or {}
        closes = closes or {}
        start = datetime(day.year, day.month, day.day, 9, 30, tzinfo=EASTERN)
        bars = []
        for index in range(78):
            bar_open = opens.get(index, Decimal("100"))
            close = closes.get(index, first_close if index == 0 else Decimal("100"))
            bars.append(Bar(
                (start + timedelta(minutes=index * 5)).astimezone(timezone.utc),
                bar_open,
                max(bar_open, close),
                min(bar_open, close),
                close,
                Decimal("1000"),
            ))
        return Session(day, tuple(bars))

    def test_opening_pressure_selects_cross_sectional_winner_and_loser(self):
        day = date(2022, 3, 2)
        sessions = {symbol: self._session(day) for symbol in PRESSURE_SYMBOLS}
        sessions["XLE"] = self._session(day, first_close=Decimal("101"))
        sessions["XLU"] = self._session(day, first_close=Decimal("99"))
        signal = opening_signal(sessions)
        self.assertEqual((signal.short_symbol, signal.long_symbol), ("XLE", "XLU"))
        self.assertEqual(signal.dispersion, Decimal("0.02"))

    def test_opening_pressure_does_not_use_future_bars(self):
        day = date(2022, 3, 2)
        sessions = {symbol: self._session(day) for symbol in PRESSURE_SYMBOLS}
        sessions["XLE"] = self._session(
            day,
            first_close=Decimal("101"),
            closes={77: Decimal("1")},
        )
        sessions["XLU"] = self._session(
            day,
            first_close=Decimal("99"),
            closes={77: Decimal("1000")},
        )
        signal = opening_signal(sessions)
        self.assertEqual((signal.short_symbol, signal.long_symbol), ("XLE", "XLU"))

    def test_opening_pressure_stop_executes_next_open_and_costs_four_orders(self):
        day = date(2022, 3, 2)
        sessions = {
            "XLE": self._session(
                day,
                opens={1: Decimal("100"), 2: Decimal("102")},
                closes={1: Decimal("101")},
            ),
            "XLU": self._session(
                day,
                opens={1: Decimal("100"), 2: Decimal("99")},
                closes={1: Decimal("100")},
            ),
        }
        signal = OpeningSignal("XLE", "XLU", Decimal("0.01"), Decimal("-0.01"), Decimal("0.02"))
        with mock.patch("opening_pressure_strategy.opening_signal", return_value=signal):
            trade = trade_pressure_day(
                sessions,
                equity=Decimal("1000000"),
                cost_per_share=Decimal("0.10"),
            )
        self.assertEqual((trade.short_quantity, trade.long_quantity), (500, 500))
        self.assertEqual((trade.short_exit, trade.long_exit), (Decimal("102"), Decimal("99")))
        self.assertEqual(trade.transaction_cost, Decimal("200.00"))
        self.assertEqual(trade.pnl, Decimal("-1700.00"))
        self.assertEqual((trade.exit_reason, trade.holding_minutes), ("stop", 5))

    def test_opening_momentum_longs_winner_and_shorts_loser(self):
        day = date(2022, 3, 2)
        sessions = {
            "XLE": self._session(
                day,
                first_close=Decimal("101"),
                opens={1: Decimal("100")},
                closes={77: Decimal("110")},
            ),
            "XLU": self._session(
                day,
                first_close=Decimal("99"),
                opens={1: Decimal("100")},
                closes={77: Decimal("90")},
            ),
        }
        signal = OpeningSignal("XLE", "XLU", Decimal("0.01"), Decimal("-0.01"), Decimal("0.02"))
        with mock.patch("opening_pressure_strategy.opening_signal", return_value=signal):
            trade = trade_pressure_day(
                sessions,
                equity=Decimal("1000000"),
                cost_per_share=Decimal("0.10"),
                direction="momentum",
            )
        self.assertEqual((trade.long_symbol, trade.short_symbol), ("XLE", "XLU"))
        self.assertEqual((trade.long_exit, trade.short_exit), (Decimal("110"), Decimal("90")))
        self.assertEqual(trade.pnl, Decimal("9800.00"))

    def test_failed_development_gate_preserves_holdout(self):
        with (
            mock.patch("opening_momentum_strategy.webull_stock_bars", return_value={}) as bars,
            mock.patch("opening_momentum_strategy._stage", return_value={}),
            mock.patch(
                "opening_momentum_strategy.development_gate",
                return_value={"passed": False, "checks": {}},
            ),
        ):
            report = run_opening_momentum_backtest(mock.Mock())
        self.assertEqual(bars.call_count, 1)
        self.assertTrue(bars.call_args.kwargs["require_cache"])
        self.assertFalse(report["holdout_requested"])
        self.assertEqual(report["decision"], "REJECT_BEFORE_HOLDOUT")


class RuntimeEnvironmentTests(unittest.TestCase):
    def test_base_python_comes_from_base_prefix(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            candidate = (
                root
                / "bin"
                / f"python{sys.version_info.major}.{sys.version_info.minor}"
            )
            candidate.parent.mkdir()
            candidate.touch()
            with mock.patch("runtime_environment.sys.base_prefix", str(root)):
                result = base_python()
        self.assertEqual(result, candidate)

    def test_misconfigured_runtime_venv_is_upgraded_from_base_python(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "venv"
            python = target / "bin" / "python"
            python.parent.mkdir(parents=True)
            python.touch()
            (target / "pyvenv.cfg").write_text(
                "home = /project/.venv/bin\n", encoding="utf-8"
            )
            base = Path("/base/bin/python3.9")
            with mock.patch("runtime_environment.base_python", return_value=base), mock.patch(
                "runtime_environment.subprocess.run"
            ) as run:
                result = ensure_runtime_venv(target)

        self.assertEqual(result, python)
        run.assert_called_once_with(
            [str(base), "-m", "venv", "--upgrade", str(target)], check=True
        )

    def test_matching_runtime_venv_is_left_untouched(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "venv"
            python = target / "bin" / "python"
            python.parent.mkdir(parents=True)
            python.touch()
            (target / "pyvenv.cfg").write_text("home = /base/bin\n", encoding="utf-8")
            with mock.patch(
                "runtime_environment.base_python",
                return_value=Path("/base/bin/python3.9"),
            ), mock.patch("runtime_environment.subprocess.run") as run:
                result = ensure_runtime_venv(target)

        self.assertEqual(result, python)
        run.assert_not_called()


class ExecutionAuthorizationTests(unittest.TestCase):
    def _policy(self, directory: str, **updates) -> Path:
        policy = {
            "version": 1,
            "environment": "api.sandbox.webull.com",
            "authorization_level": "SANDBOX_MICRO",
            "new_entries_enabled": True,
            "approved_strategy_ids": ["crypto-day-v2"],
            "approved_symbols": ["BTCUSD"],
            "expires_at": "2026-02-01T00:00:00+00:00",
        }
        policy.update(updates)
        path = Path(directory) / "authorization.json"
        path.write_text(json.dumps(policy), encoding="utf-8")
        return path

    def test_repository_policy_blocks_new_risk(self):
        status = authorization_status(now=datetime(2026, 1, 20, tzinfo=timezone.utc))
        decision = authorize_automated_order(
            "crypto-day-v2",
            "BTCUSD",
            "BUY",
            now=datetime(2026, 1, 20, tzinfo=timezone.utc),
        )
        self.assertEqual(status["authorization_level"], "DATA_COLLECTION")
        self.assertFalse(status["new_entries_authorized"])
        self.assertFalse(decision["authorized"])

    def test_missing_policy_fails_closed_for_entry_but_preserves_verified_exit(self):
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "missing.json"
            buy = authorize_automated_order(
                "crypto-day-v2", "BTCUSD", "BUY", policy_file=missing
            )
            sell = authorize_automated_order(
                "crypto-day-v2",
                "BTCUSD",
                "SELL",
                policy_file=missing,
                current_position_quantity=Decimal("2"),
                order_quantity=Decimal("1"),
                current_open_order_count=0,
            )
        self.assertFalse(buy["authorized"])
        self.assertIn("policy_unreadable", buy["blocking_reasons"])
        self.assertTrue(sell["authorized"])
        self.assertEqual(sell["mode"], "RISK_REDUCTION")

    def test_sell_requires_and_cannot_exceed_verified_long_position(self):
        unverified = authorize_automated_order("any", "BTCUSD", "SELL")
        flat = authorize_automated_order(
            "any",
            "BTCUSD",
            "SELL",
            current_position_quantity=Decimal("0"),
            order_quantity=Decimal("1"),
            current_open_order_count=0,
        )
        invalid = authorize_automated_order(
            "any",
            "BTCUSD",
            "SELL",
            current_position_quantity=Decimal("1"),
            order_quantity=Decimal("0"),
            current_open_order_count=0,
        )
        oversized = authorize_automated_order(
            "any",
            "BTCUSD",
            "SELL",
            current_position_quantity=Decimal("1"),
            order_quantity=Decimal("1.0001"),
            current_open_order_count=0,
        )
        pending = authorize_automated_order(
            "any",
            "BTCUSD",
            "SELL",
            current_position_quantity=Decimal("1"),
            order_quantity=Decimal("1"),
            current_open_order_count=1,
        )
        exact = authorize_automated_order(
            "any",
            "BTCUSD",
            "SELL",
            current_position_quantity=Decimal("1"),
            order_quantity=Decimal("1"),
            current_open_order_count=0,
        )

        self.assertIn(
            "risk_reduction_quantity_unverified", unverified["blocking_reasons"]
        )
        self.assertIn("no_verified_long_position", flat["blocking_reasons"])
        self.assertIn("invalid_risk_reduction_quantity", invalid["blocking_reasons"])
        self.assertIn(
            "sell_exceeds_verified_long_position", oversized["blocking_reasons"]
        )
        self.assertIn("open_orders_present", pending["blocking_reasons"])
        self.assertTrue(exact["authorized"])
        self.assertEqual(exact["mode"], "RISK_REDUCTION")

    def test_buy_requires_fresh_flat_account_state_and_positive_quantity(self):
        now = datetime(2026, 1, 20, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            policy = self._policy(directory)
            unverified = authorize_automated_order(
                "crypto-day-v2", "BTCUSD", "BUY", policy_file=policy, now=now
            )
            positioned = authorize_automated_order(
                "crypto-day-v2",
                "BTCUSD",
                "BUY",
                policy_file=policy,
                now=now,
                current_position_quantity=Decimal("0.01"),
                order_quantity=Decimal("0.01"),
                current_open_order_count=0,
            )
            pending = authorize_automated_order(
                "crypto-day-v2",
                "BTCUSD",
                "BUY",
                policy_file=policy,
                now=now,
                current_position_quantity=Decimal("0"),
                order_quantity=Decimal("0.01"),
                current_open_order_count=1,
            )
            invalid = authorize_automated_order(
                "crypto-day-v2",
                "BTCUSD",
                "BUY",
                policy_file=policy,
                now=now,
                current_position_quantity=Decimal("0"),
                order_quantity=Decimal("0"),
                current_open_order_count=0,
            )

        self.assertIn("new_risk_state_unverified", unverified["blocking_reasons"])
        self.assertIn("position_already_exists", positioned["blocking_reasons"])
        self.assertIn("open_orders_present", pending["blocking_reasons"])
        self.assertIn("invalid_new_risk_quantity", invalid["blocking_reasons"])

    def test_unexpired_policy_limits_strategy_and_symbol(self):
        now = datetime(2026, 1, 20, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            policy = self._policy(directory)
            approved = authorize_automated_order(
                "crypto-day-v2",
                "BTCUSD",
                "BUY",
                policy_file=policy,
                now=now,
                current_position_quantity=Decimal("0"),
                order_quantity=Decimal("0.01"),
                current_open_order_count=0,
            )
            wrong_strategy = authorize_automated_order(
                "crypto-ema-ha-v1", "BTCUSD", "BUY", policy_file=policy, now=now
            )
            wrong_symbol = authorize_automated_order(
                "crypto-day-v2", "ETHUSD", "BUY", policy_file=policy, now=now
            )
        self.assertTrue(approved["authorized"])
        self.assertIn("strategy_not_approved", wrong_strategy["blocking_reasons"])
        self.assertIn("symbol_not_approved", wrong_symbol["blocking_reasons"])

    def test_production_endpoint_blocks_risk_reduction_too(self):
        with tempfile.TemporaryDirectory() as directory:
            policy = self._policy(directory)
            with mock.patch("execution_guard.API_ENDPOINT", "api.webull.com"):
                decision = authorize_automated_order(
                    "crypto-day-v2",
                    "BTCUSD",
                    "SELL",
                    policy_file=policy,
                    current_position_quantity=Decimal("1"),
                    order_quantity=Decimal("1"),
                    current_open_order_count=0,
                )
        self.assertFalse(decision["authorized"])


class FakeResponse:
    def __init__(self, status_code, data=None):
        self.status_code = status_code
        self._data = data

    def json(self):
        return self._data


class FakeCryptoAPI:
    def __init__(self, *, positions=None, open_orders=None, place_status=200):
        self.positions = positions or []
        self.open_orders = [] if open_orders is None else open_orders
        self.place_status = place_status
        self.place_calls = []
        self.trade = SimpleNamespace(
            account_v2=SimpleNamespace(
                get_account_position=lambda _: self.positions,
                get_account_balance=lambda _: {
                    "account_currency_assets": [{"currency": "USD", "buying_power": "1000000"}]
                },
            ),
            order_v3=SimpleNamespace(
                get_order_open=lambda *_args, **_kwargs: self.open_orders,
                get_order_detail=lambda *_: {},
                place_order=self._place_order,
            ),
        )
        self.data = SimpleNamespace(
            crypto_market_data=SimpleNamespace(
                get_crypto_snapshot=lambda symbols: [
                    {"symbol": symbol, "price": "100", "bid": "99", "ask": "101"}
                    for symbol in symbols
                ]
            ),
            instrument=SimpleNamespace(
                get_crypto_instrument=lambda symbols: [
                    {
                        "symbol": symbol,
                        "lot_size": "0.00000001",
                        "min_trade_qty": "0.00000001",
                        "min_trade_amt": "2",
                    }
                    for symbol in symbols
                ]
            ),
        )

    def account_id(self, reference):
        self.last_account_reference = reference
        return "crypto-account"

    def _place_order(self, account_id, orders):
        self.place_calls.append((account_id, orders))
        return FakeResponse(self.place_status, {"accepted": self.place_status < 400})


class CryptoRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        authorization_patcher = mock.patch(
            "crypto_runtime.authorize_automated_order",
            return_value={"authorized": True, "mode": "SANDBOX_MICRO", "blocking_reasons": []},
        )
        self.patches = [
            mock.patch("crypto_runtime.STATE_DIR", root),
            mock.patch("crypto_runtime.STATE_FILE", root / "state.json"),
            mock.patch("crypto_runtime.LOG_FILE", root / "events.jsonl"),
            mock.patch("crypto_runtime.LOCK_FILE", root / "strategy.lock"),
            mock.patch("crypto_runtime.load_report", return_value={"deployment_symbols": ["BTCUSD"]}),
            authorization_patcher,
        ]
        for patcher in self.patches:
            started = patcher.start()
            if patcher is authorization_patcher:
                self.authorization = started

    def tearDown(self):
        for patcher in reversed(self.patches):
            patcher.stop()
        self.temporary.cleanup()

    def _closed_bars(self):
        bars = make_bars(52)
        quality = DataQuality(52, 52, 1.0, 0, 7200, True)
        return {"BTCUSD": (bars, quality)}

    def test_repeated_run_does_not_submit_duplicate_order(self):
        from crypto_runtime import run_once

        api = FakeCryptoAPI()
        now = datetime(2026, 1, 20, tzinfo=timezone.utc)
        with mock.patch("crypto_runtime.webull_bars", return_value=self._closed_bars()), mock.patch(
            "crypto_runtime.signals", return_value=["HOLD"] * 51 + ["BUY"]
        ):
            run_once(api, confirmed=True, now=now)
            run_once(api, confirmed=True, now=now + timedelta(minutes=1))
        self.assertEqual(len(api.place_calls), 1)

    def test_execution_guard_blocks_entry_before_order_submission(self):
        from crypto_runtime import initialize_state, submit_market_order

        self.authorization.return_value = {
            "authorized": False,
            "mode": "BLOCKED",
            "blocking_reasons": ["new_entries_enabled"],
        }
        api = FakeCryptoAPI()
        state = initialize_state(datetime(2026, 1, 20, tzinfo=timezone.utc))
        result = submit_market_order(
            api,
            "crypto-account",
            state,
            symbol="BTCUSD",
            side="BUY",
            quantity=Decimal("0.01"),
            candle_time=datetime(2026, 1, 20, tzinfo=timezone.utc),
            reason="test",
        )
        self.assertTrue(result["blocked"])
        self.assertEqual(api.place_calls, [])
        self.assertEqual(state["submitted_order_ids"], [])

    def test_sell_authorization_uses_fresh_verified_position(self):
        from crypto_runtime import initialize_state, submit_market_order

        api = FakeCryptoAPI(positions=[{"symbol": "BTCUSD", "quantity": "0.01"}])
        state = initialize_state(datetime(2026, 1, 20, tzinfo=timezone.utc))
        submit_market_order(
            api,
            "crypto-account",
            state,
            symbol="BTCUSD",
            side="SELL",
            quantity=Decimal("0.01"),
            candle_time=datetime(2026, 1, 20, tzinfo=timezone.utc),
            reason="test",
        )

        self.authorization.assert_called_once_with(
            "crypto-ema-ha-v1",
            "BTCUSD",
            "SELL",
            current_position_quantity=Decimal("0.01"),
            order_quantity=Decimal("0.01"),
            current_open_order_count=0,
        )

    def test_sell_with_absent_symbol_is_blocked_as_verified_flat(self):
        from crypto_runtime import initialize_state, submit_market_order

        self.authorization.side_effect = authorize_automated_order
        api = FakeCryptoAPI()
        state = initialize_state(datetime(2026, 1, 20, tzinfo=timezone.utc))
        result = submit_market_order(
            api,
            "crypto-account",
            state,
            symbol="BTCUSD",
            side="SELL",
            quantity=Decimal("0.01"),
            candle_time=datetime(2026, 1, 20, tzinfo=timezone.utc),
            reason="test",
        )

        self.assertTrue(result["blocked"])
        self.assertEqual(result["authorization"]["blocking_reasons"], ["no_verified_long_position"])
        self.assertEqual(api.place_calls, [])
        self.assertEqual(
            self.authorization.call_args.kwargs["current_position_quantity"], Decimal("0")
        )

    def test_buy_authorization_uses_fresh_flat_account_state(self):
        from crypto_runtime import initialize_state, submit_market_order

        api = FakeCryptoAPI()
        state = initialize_state(datetime(2026, 1, 20, tzinfo=timezone.utc))
        submit_market_order(
            api,
            "crypto-account",
            state,
            symbol="BTCUSD",
            side="BUY",
            quantity=Decimal("0.01"),
            candle_time=datetime(2026, 1, 20, tzinfo=timezone.utc),
            reason="test",
        )

        self.authorization.assert_called_once_with(
            "crypto-ema-ha-v1",
            "BTCUSD",
            "BUY",
            current_position_quantity=Decimal("0"),
            order_quantity=Decimal("0.01"),
            current_open_order_count=0,
        )

    def test_unreadable_open_order_state_fails_before_authorization(self):
        from crypto_runtime import initialize_state, submit_market_order

        api = FakeCryptoAPI(open_orders={"unexpected": []})
        state = initialize_state(datetime(2026, 1, 20, tzinfo=timezone.utc))
        with self.assertRaisesRegex(RuntimeError, "unexpected data"):
            submit_market_order(
                api,
                "crypto-account",
                state,
                symbol="BTCUSD",
                side="BUY",
                quantity=Decimal("0.01"),
                candle_time=datetime(2026, 1, 20, tzinfo=timezone.utc),
                reason="test",
            )

        self.authorization.assert_not_called()
        self.assertEqual(api.place_calls, [])

    def test_crypto_status_requires_capital_authorization(self):
        from crypto_runtime import status

        with mock.patch(
            "crypto_runtime.authorization_status",
            return_value={"new_entries_authorized": False},
        ):
            result = status()
        self.assertEqual(result["deployment_symbols"], ["BTCUSD"])
        self.assertEqual(result["decision"], "NO_TRADE")

    def test_pending_order_blocks_new_entry(self):
        from crypto_runtime import initialize_state, run_once, write_state

        now = datetime(2026, 1, 20, tzinfo=timezone.utc)
        state = initialize_state(now)
        state["pending_orders"]["BTCUSD"] = {
            "order_id": "pending",
            "side": "BUY",
            "quantity": "1",
            "reason": "test",
            "estimated_loss": False,
            "submitted_at": now.isoformat(),
        }
        write_state(state)
        api = FakeCryptoAPI()
        with mock.patch("crypto_runtime.webull_bars", return_value=self._closed_bars()), mock.patch(
            "crypto_runtime.signals", return_value=["HOLD"] * 51 + ["BUY"]
        ):
            run_once(api, confirmed=True, now=now + timedelta(minutes=1))
        self.assertEqual(api.place_calls, [])

    def test_429_order_failure_is_not_retried(self):
        from crypto_runtime import initialize_state, submit_market_order

        api = FakeCryptoAPI(place_status=429)
        state = initialize_state(datetime(2026, 1, 20, tzinfo=timezone.utc))
        with self.assertRaisesRegex(RuntimeError, "429"):
            submit_market_order(
                api,
                "crypto-account",
                state,
                symbol="BTCUSD",
                side="BUY",
                quantity=Decimal("0.01"),
                candle_time=datetime(2026, 1, 20, tzinfo=timezone.utc),
                reason="test",
            )
        duplicate = submit_market_order(
            api,
            "crypto-account",
            state,
            symbol="BTCUSD",
            side="BUY",
            quantity=Decimal("0.01"),
            candle_time=datetime(2026, 1, 20, tzinfo=timezone.utc),
            reason="test",
        )
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual(len(api.place_calls), 1)

    def test_empty_order_detail_does_not_block_submission(self):
        from crypto_runtime import initialize_state, submit_market_order

        api = FakeCryptoAPI()
        api.trade.order_v3.get_order_detail = lambda *_: FakeResponse(200, {"orders": []})
        state = initialize_state(datetime(2026, 1, 20, tzinfo=timezone.utc))
        result = submit_market_order(
            api,
            "crypto-account",
            state,
            symbol="BTCUSD",
            side="BUY",
            quantity=Decimal("0.01"),
            candle_time=datetime(2026, 1, 20, tzinfo=timezone.utc),
            reason="test",
        )
        self.assertFalse(result["duplicate"])
        self.assertEqual(len(api.place_calls), 1)

    def test_paused_state_blocks_entry(self):
        from crypto_runtime import initialize_state, run_once, write_state

        now = datetime(2026, 1, 20, tzinfo=timezone.utc)
        state = initialize_state(now)
        state["paused"] = True
        write_state(state)
        api = FakeCryptoAPI()
        with mock.patch("crypto_runtime.webull_bars", return_value=self._closed_bars()), mock.patch(
            "crypto_runtime.signals", return_value=["HOLD"] * 51 + ["BUY"]
        ):
            run_once(api, confirmed=True, now=now + timedelta(minutes=1))
        self.assertEqual(api.place_calls, [])

    def test_expiration_closes_position(self):
        from crypto_runtime import initialize_state, run_once, write_state

        now = datetime(2026, 1, 20, tzinfo=timezone.utc)
        state = initialize_state(now - timedelta(days=31))
        write_state(state)
        api = FakeCryptoAPI(positions=[{
            "symbol": "BTCUSD",
            "quantity": "0.01",
            "cost_price": "100",
            "unrealized_profit_loss": "1",
        }])
        result = run_once(api, confirmed=True, now=now)
        self.assertEqual(result["status"], "closing")
        self.assertEqual(api.place_calls[0][1][0]["side"], "SELL")

    def test_three_losses_start_24_hour_cooldown(self):
        from crypto_runtime import initialize_state, reconcile_pending

        now = datetime(2026, 1, 20, tzinfo=timezone.utc)
        state = initialize_state(now)
        state["consecutive_losses"] = 2
        state["pending_orders"]["BTCUSD"] = {
            "order_id": "loss-three",
            "side": "SELL",
            "quantity": "1",
            "reason": "test",
            "estimated_loss": True,
            "submitted_at": now.isoformat(),
        }
        reconcile_pending(FakeCryptoAPI(), "crypto-account", state, {}, now)
        self.assertEqual(state["consecutive_losses"], 3)
        self.assertEqual(datetime.fromisoformat(state["cooldown_until"]), now + timedelta(hours=24))

    def test_production_endpoint_is_rejected(self):
        from crypto_runtime import ensure_sandbox

        with mock.patch("crypto_runtime.API_ENDPOINT", "api.webull.com"):
            with self.assertRaisesRegex(RuntimeError, "locked"):
                ensure_sandbox()


class DayTraderRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        report = {
            "deployment_symbols": ["BTCUSD", "ETHUSD"],
            "sources": {
                "coinbase_m5_90d": {
                    "symbols": {
                        symbol: {"professional_gate": {"passed": True}}
                        for symbol in ("BTCUSD", "ETHUSD")
                    }
                }
            },
        }
        authorization_patcher = mock.patch(
            "daytrader_runtime.authorize_automated_order",
            return_value={"authorized": True, "mode": "SANDBOX_MICRO", "blocking_reasons": []},
        )
        self.patches = [
            mock.patch("daytrader_runtime.STATE_DIR", root),
            mock.patch("daytrader_runtime.STATE_FILE", root / "state.json"),
            mock.patch("daytrader_runtime.LOG_FILE", root / "events.jsonl"),
            mock.patch("daytrader_runtime.LOCK_FILE", root / "strategy.lock"),
            mock.patch("daytrader_runtime.load_report", return_value=report),
            mock.patch("daytrader_runtime._ensure_old_runner_paused"),
            authorization_patcher,
        ]
        for patcher in self.patches:
            started = patcher.start()
            if patcher is authorization_patcher:
                self.authorization = started

    def tearDown(self):
        for patcher in reversed(self.patches):
            patcher.stop()
        self.temporary.cleanup()

    def _closed_bars(self):
        quality = DataQuality(576, 576, 1.0, 0, 300, True)
        bars = make_bars(102, interval_seconds=300)
        return {symbol: (bars, quality) for symbol in ("BTCUSD", "ETHUSD")}

    def test_daytrader_allows_only_one_global_pending_entry(self):
        from daytrader_runtime import run_once

        api = FakeCryptoAPI()
        decision = {"signal": "BUY"}
        with mock.patch("daytrader_runtime.webull_bars", return_value=self._closed_bars()), mock.patch(
            "daytrader_runtime.decision_snapshot", return_value=decision
        ):
            run_once(api, confirmed=True, now=datetime(2026, 1, 20, tzinfo=timezone.utc))
        self.assertEqual(len(api.place_calls), 1)
        self.assertEqual(api.place_calls[0][1][0]["symbol"], "BTCUSD")

    def test_daytrader_guard_blocks_entry_without_consuming_daily_limit(self):
        from daytrader_runtime import read_state, run_once

        self.authorization.return_value = {
            "authorized": False,
            "mode": "BLOCKED",
            "blocking_reasons": ["new_entries_enabled"],
        }
        api = FakeCryptoAPI()
        now = datetime(2026, 1, 20, tzinfo=timezone.utc)
        with mock.patch("daytrader_runtime.webull_bars", return_value=self._closed_bars()), mock.patch(
            "daytrader_runtime.decision_snapshot", return_value={"signal": "BUY"}
        ):
            run_once(api, confirmed=True, now=now)
        state = read_state()
        self.assertEqual(api.place_calls, [])
        self.assertEqual(state["entries_by_day"][now.date().isoformat()], [])

    def test_daytrader_sell_authorization_uses_fresh_verified_position(self):
        from daytrader_runtime import _submit_order, initialize_state

        api = FakeCryptoAPI(positions=[{"symbol": "BTCUSD", "quantity": "0.02"}])
        now = datetime(2026, 1, 20, tzinfo=timezone.utc)
        state = initialize_state(now)
        _submit_order(
            api,
            "crypto-account",
            state,
            symbol="BTCUSD",
            side="SELL",
            quantity=Decimal("0.01"),
            event_time=now,
            reason="test",
        )

        self.authorization.assert_called_once_with(
            "crypto-day-v2",
            "BTCUSD",
            "SELL",
            current_position_quantity=Decimal("0.02"),
            order_quantity=Decimal("0.01"),
            current_open_order_count=0,
        )

    def test_daytrader_status_requires_capital_authorization(self):
        from daytrader_runtime import status

        with mock.patch(
            "daytrader_runtime.authorization_status",
            return_value={"new_entries_authorized": False},
        ):
            result = status()
        self.assertEqual(result["deployment_symbols"], ["BTCUSD", "ETHUSD"])
        self.assertEqual(result["decision"], "NO_TRADE")

    def test_daytrader_pause_blocks_new_entries(self):
        from daytrader_runtime import initialize_state, run_once, write_state

        now = datetime(2026, 1, 20, tzinfo=timezone.utc)
        state = initialize_state(now)
        state["paused"] = True
        write_state(state)
        api = FakeCryptoAPI()
        decision = {"signal": "BUY"}
        with mock.patch("daytrader_runtime.webull_bars", return_value=self._closed_bars()), mock.patch(
            "daytrader_runtime.decision_snapshot", return_value=decision
        ):
            run_once(api, confirmed=True, now=now)
        self.assertEqual(api.place_calls, [])

    def test_daytrader_uncertain_order_halts_without_retry(self):
        from daytrader_runtime import run_once

        now = datetime(2026, 1, 20, tzinfo=timezone.utc)
        api = FakeCryptoAPI()

        def uncertain_place(account_id, orders):
            api.place_calls.append((account_id, orders))
            raise TimeoutError("unknown outcome")

        api.trade.order_v3.place_order = uncertain_place
        decision = {"signal": "BUY"}
        with mock.patch("daytrader_runtime.webull_bars", return_value=self._closed_bars()), mock.patch(
            "daytrader_runtime.decision_snapshot", return_value=decision
        ):
            with self.assertRaisesRegex(RuntimeError, "uncertain"):
                run_once(api, confirmed=True, now=now)
            result = run_once(api, confirmed=True, now=now + timedelta(seconds=5))
        self.assertEqual(result["status"], "halted")
        self.assertEqual(len(api.place_calls), 1)

    def test_daytrader_refuses_to_start_without_deployment_symbols(self):
        from daytrader_runtime import initialize_state

        report = {"deployment_symbols": [], "sources": {"coinbase_m5_90d": {"symbols": {}}}}
        with mock.patch("daytrader_runtime.load_report", return_value=report):
            with self.assertRaisesRegex(RuntimeError, "NO_TRADE"):
                initialize_state(datetime(2026, 1, 20, tzinfo=timezone.utc))


if __name__ == "__main__":
    unittest.main()

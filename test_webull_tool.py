import tempfile
import unittest
import urllib.parse
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from config import APP_KEY, APP_SECRET
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
from webull_api import is_mutating_call, redact_secrets
from webull_cli import replace_account_placeholder
from webull_orders import (
    build_order,
    order_instrument_type,
    validate_batch_orders,
    validate_order,
)


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

    def test_quantity_rounds_down_to_lot_size(self):
        self.assertEqual(
            quantity_for_notional(Decimal("10"), Decimal("3"), Decimal("0.01")),
            Decimal("3.33"),
        )

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


class FakeResponse:
    def __init__(self, status_code, data=None):
        self.status_code = status_code
        self._data = data

    def json(self):
        return self._data


class FakeCryptoAPI:
    def __init__(self, *, positions=None, place_status=200):
        self.positions = positions or []
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
        self.patches = [
            mock.patch("crypto_runtime.STATE_DIR", root),
            mock.patch("crypto_runtime.STATE_FILE", root / "state.json"),
            mock.patch("crypto_runtime.LOG_FILE", root / "events.jsonl"),
            mock.patch("crypto_runtime.LOCK_FILE", root / "strategy.lock"),
            mock.patch("crypto_runtime.load_report", return_value={"deployment_symbols": ["BTCUSD"]}),
        ]
        for patcher in self.patches:
            patcher.start()

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


if __name__ == "__main__":
    unittest.main()

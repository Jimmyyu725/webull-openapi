import tempfile
import unittest
import urllib.parse
from datetime import date, datetime, timedelta, timezone
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
from intraday_momentum_strategy import (
    Observation,
    _execute as execute_intraday_momentum,
    backtest as backtest_intraday_momentum,
    observations as intraday_momentum_observations,
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
from webull_api import is_mutating_call, redact_secrets
from webull_cli import replace_account_placeholder
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
        self.patches = [
            mock.patch("daytrader_runtime.STATE_DIR", root),
            mock.patch("daytrader_runtime.STATE_FILE", root / "state.json"),
            mock.patch("daytrader_runtime.LOG_FILE", root / "events.jsonl"),
            mock.patch("daytrader_runtime.LOCK_FILE", root / "strategy.lock"),
            mock.patch("daytrader_runtime.load_report", return_value=report),
            mock.patch("daytrader_runtime._ensure_old_runner_paused"),
        ]
        for patcher in self.patches:
            patcher.start()

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

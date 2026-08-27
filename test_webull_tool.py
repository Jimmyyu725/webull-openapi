import unittest

from config import APP_KEY, APP_SECRET
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


if __name__ == "__main__":
    unittest.main()

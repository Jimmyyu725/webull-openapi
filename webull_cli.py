#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
import warnings
from datetime import date, timedelta
from importlib.metadata import version
from typing import Any

warnings.filterwarnings("ignore", message="urllib3 v2 only supports OpenSSL.*")

from config import API_ENDPOINT
from webull_api import (
    ApiResult,
    WebullAPI,
    is_mutating_call,
    normalize_result,
    print_result,
    redact_secrets,
)
from webull_orders import (
    ACCOUNT_BY_INSTRUMENT,
    build_order,
    load_json,
    order_instrument_type,
    validate_batch_orders,
    validate_order,
)


def emit(data: Any, ok: bool = True, status: int = None) -> int:
    return print_result(ApiResult(ok, status, data))


def account_reference(args: argparse.Namespace, instrument_type: str = None) -> str:
    reference = getattr(args, "account", None) or getattr(args, "account_class", None)
    if reference:
        return reference
    if instrument_type:
        return ACCOUNT_BY_INSTRUMENT[instrument_type.upper()]
    return None


def replace_account_placeholder(value: Any, account_id: str) -> Any:
    if value == "@account":
        return account_id
    if isinstance(value, list):
        return [replace_account_placeholder(item, account_id) for item in value]
    if isinstance(value, dict):
        return {key: replace_account_placeholder(item, account_id) for key, item in value.items()}
    return value


def cmd_doctor(api: WebullAPI, _: argparse.Namespace) -> int:
    accounts = api.accounts(refresh=True)
    return emit({
        "environment": "Webull Sandbox / Paper Trading",
        "endpoint": API_ENDPOINT,
        "sdk_version": version("webull-openapi-python-sdk"),
        "account_classes": [account.get("account_class") for account in accounts],
        "connection": "ok",
    })


def cmd_accounts(api: WebullAPI, _: argparse.Namespace) -> int:
    return emit(api.accounts(refresh=True))


def cmd_balance(api: WebullAPI, args: argparse.Namespace) -> int:
    return print_result(normalize_result(
        api.trade.account_v2.get_account_balance(api.account_id(account_reference(args)))
    ))


def cmd_positions(api: WebullAPI, args: argparse.Namespace) -> int:
    return print_result(normalize_result(
        api.trade.account_v2.get_account_position(api.account_id(account_reference(args)))
    ))


def cmd_activities(api: WebullAPI, args: argparse.Namespace) -> int:
    result = api.trade.activity.get_activities(
        api.account_id(account_reference(args)),
        activity_types=args.types,
        start_time=args.start,
        end_time=args.end,
        last_activity_id=args.cursor,
        page_size=args.page_size,
    )
    return print_result(normalize_result(result))


def cmd_orders_open(api: WebullAPI, args: argparse.Namespace) -> int:
    result = api.trade.order_v3.get_order_open(
        api.account_id(account_reference(args)),
        page_size=args.page_size,
        last_client_order_id=args.cursor,
    )
    return print_result(normalize_result(result))


def cmd_orders_history(api: WebullAPI, args: argparse.Namespace) -> int:
    result = api.trade.order_v3.get_order_history(
        api.account_id(account_reference(args)),
        page_size=args.page_size,
        start_date=args.start,
        end_date=args.end,
        last_client_order_id=args.cursor,
    )
    return print_result(normalize_result(result))


def cmd_order_detail(api: WebullAPI, args: argparse.Namespace) -> int:
    result = api.trade.order_v3.get_order_detail(
        api.account_id(account_reference(args)), args.client_order_id
    )
    return print_result(normalize_result(result))


def order_list(args: argparse.Namespace) -> list[dict[str, Any]]:
    if args.orders:
        orders = load_json(args.orders, [])
        if not isinstance(orders, list) or not orders:
            raise ValueError("--orders must be a non-empty JSON list")
        for order in orders:
            validate_order(order)
        return orders

    if not args.symbol or not args.instrument_type or not args.quantity:
        raise ValueError("Provide --orders or --symbol, --instrument-type, and --quantity")
    instrument_type = args.instrument_type.upper()
    session = args.session or ("CORE" if instrument_type == "EQUITY" else None)
    return [build_order(
        symbol=args.symbol,
        instrument_type=instrument_type,
        side=args.side,
        quantity=args.quantity,
        order_type=args.order_type,
        tif=args.tif,
        limit_price=args.limit_price,
        stop_price=args.stop_price,
        client_order_id=args.client_order_id,
        combo_type=args.combo_type,
        entrust_type=args.entrust_type,
        session=session,
        event_outcome=args.event_outcome,
        option_strategy=args.option_strategy,
        legs=load_json(args.legs, None),
        extra=load_json(args.extra, None),
    )]


def order_account(api: WebullAPI, args: argparse.Namespace, orders: list[dict[str, Any]]) -> str:
    instrument_type = order_instrument_type(orders[0])
    if any(order_instrument_type(order) != instrument_type for order in orders):
        raise ValueError("Every order in one request must use the same instrument type")
    return api.account_id(account_reference(args, instrument_type))


def cmd_order_preview(api: WebullAPI, args: argparse.Namespace) -> int:
    orders = order_list(args)
    result = api.trade.order_v3.preview_order(
        order_account(api, args, orders), orders, args.client_combo_order_id
    )
    return print_result(normalize_result(result))


def cmd_order_place(api: WebullAPI, args: argparse.Namespace) -> int:
    if not args.yes:
        raise ValueError("Paper order placement requires --yes; use preview first")
    orders = order_list(args)
    result = api.trade.order_v3.place_order(
        order_account(api, args, orders), orders, args.client_combo_order_id
    )
    return print_result(normalize_result(result))


def cmd_order_replace(api: WebullAPI, args: argparse.Namespace) -> int:
    if not args.yes:
        raise ValueError("Paper order replacement requires --yes")
    orders = load_json(args.orders, None)
    if not isinstance(orders, list) or not orders:
        raise ValueError("--orders must be a non-empty JSON list")
    for order in orders:
        if not order.get("client_order_id"):
            raise ValueError("Every replacement requires client_order_id")
    result = api.trade.order_v3.replace_order(
        api.account_id(account_reference(args)), orders, args.client_combo_order_id
    )
    return print_result(normalize_result(result))


def cmd_order_batch(api: WebullAPI, args: argparse.Namespace) -> int:
    if not args.yes:
        raise ValueError("Paper batch placement requires --yes")
    orders = load_json(args.batch_orders, None)
    if not isinstance(orders, list) or not orders:
        raise ValueError("--batch-orders must be a non-empty JSON list")
    validate_batch_orders(orders)
    result = api.trade.order_v3.batch_place_order(
        api.account_id(account_reference(args)), orders
    )
    return print_result(normalize_result(result))


def cmd_order_cancel(api: WebullAPI, args: argparse.Namespace) -> int:
    if not args.yes:
        raise ValueError("Paper order cancellation requires --yes")
    result = api.trade.order_v3.cancel_order(
        api.account_id(account_reference(args)), args.client_order_id
    )
    return print_result(normalize_result(result))


def cmd_market_snapshot(api: WebullAPI, args: argparse.Namespace) -> int:
    symbols = args.symbols
    calls = {
        "stock": lambda: api.data.market_data.get_snapshot(symbols, args.category),
        "crypto": lambda: api.data.crypto_market_data.get_crypto_snapshot(symbols),
        "futures": lambda: api.data.futures_market_data.get_futures_snapshot(symbols, "US_FUTURES"),
        "option": lambda: api.data.option_market_data.get_option_snapshot(symbols, "US_OPTION"),
        "event": lambda: api.data.event_market_data.get_event_snapshot(symbols),
    }
    return print_result(normalize_result(calls[args.asset]()))


def cmd_market_bars(api: WebullAPI, args: argparse.Namespace) -> int:
    symbols = args.symbols
    calls = {
        "stock": lambda: api.data.market_data.get_batch_history_bar(
            symbols, args.category, args.timespan, args.count
        ),
        "crypto": lambda: api.data.crypto_market_data.get_crypto_history_bar(
            symbols, "US_CRYPTO", args.timespan, args.count
        ),
        "futures": lambda: api.data.futures_market_data.get_futures_history_bars(
            ",".join(symbols), "US_FUTURES", args.timespan, args.count
        ),
        "option": lambda: api.data.option_market_data.get_option_history_bars(
            symbols, "US_OPTION", args.timespan, args.count
        ),
        "event": lambda: api.data.event_market_data.get_event_bars(
            symbols, args.timespan, count=int(args.count)
        ),
    }
    return print_result(normalize_result(calls[args.asset]()))


def cmd_market_ticks(api: WebullAPI, args: argparse.Namespace) -> int:
    symbol = args.symbol
    calls = {
        "stock": lambda: api.data.market_data.get_tick(symbol, args.category, args.count),
        "futures": lambda: api.data.futures_market_data.get_futures_tick(symbol, "US_FUTURES", args.count),
        "option": lambda: api.data.option_market_data.get_option_tick(symbol, "US_OPTION", args.count),
        "event": lambda: api.data.event_market_data.get_event_tick(symbol, count=int(args.count)),
    }
    if args.asset not in calls:
        raise ValueError(f"Tick endpoint is not available for {args.asset}")
    return print_result(normalize_result(calls[args.asset]()))


def cmd_market_depth(api: WebullAPI, args: argparse.Namespace) -> int:
    symbol = args.symbol
    calls = {
        "stock": lambda: api.data.market_data.get_quotes(symbol, args.category, args.depth),
        "futures": lambda: api.data.futures_market_data.get_futures_depth(symbol, "US_FUTURES", args.depth),
        "event": lambda: api.data.event_market_data.get_event_depth(symbol, depth=args.depth or 10),
    }
    if args.asset not in calls:
        raise ValueError(f"Depth endpoint is not available for {args.asset}")
    return print_result(normalize_result(calls[args.asset]()))


def cmd_instruments(api: WebullAPI, args: argparse.Namespace) -> int:
    extra = load_json(args.extra, {})
    calls = {
        "stock": lambda: api.data.instrument.get_instrument(
            symbols=args.symbols or None, category=args.category, **extra
        ),
        "crypto": lambda: api.data.instrument.get_crypto_instrument(
            symbols=args.symbols or None, **extra
        ),
        "futures": lambda: api.data.instrument.get_futures_instrument(
            symbols=args.symbols or None, category="US_FUTURES", code=args.code, **extra
        ),
        "option": lambda: api.data.instrument.get_option_contracts(
            underlying_symbols=",".join(args.symbols) if args.symbols else None, **extra
        ),
    }
    return print_result(normalize_result(calls[args.asset]()))


def cmd_event_discovery(api: WebullAPI, args: argparse.Namespace) -> int:
    extra = load_json(args.extra, {})
    if args.mode == "categories":
        call = api.data.instrument.get_event_categories
    elif args.mode == "series":
        call = lambda: api.data.instrument.get_event_series(category=args.category, **extra)
    elif args.mode == "events":
        if not args.series:
            raise ValueError("--series is required for event discovery")
        call = lambda: api.data.instrument.get_event_events(args.series, **extra)
    else:
        if not args.series:
            raise ValueError("--series is required for event markets")
        call = lambda: api.data.instrument.get_event_instrument(args.series, **extra)
    return print_result(normalize_result(call()))


def cmd_catalog(api: WebullAPI, args: argparse.Namespace) -> int:
    return emit(api.catalog(args.target))


def cmd_call(api: WebullAPI, args: argparse.Namespace) -> int:
    if is_mutating_call(args.path) and not args.yes:
        raise ValueError("Mutating SDK calls require --yes")
    call_args = load_json(args.args, [])
    call_kwargs = load_json(args.kwargs, {})
    if not isinstance(call_args, list) or not isinstance(call_kwargs, dict):
        raise ValueError("--args must be a JSON list and --kwargs a JSON object")
    if args.account:
        account_id = api.account_id(args.account)
        call_args = replace_account_placeholder(call_args, account_id)
        call_kwargs = replace_account_placeholder(call_kwargs, account_id)
    return print_result(api.invoke(args.path, call_args, call_kwargs))


def cmd_capabilities(api: WebullAPI, _: argparse.Namespace) -> int:
    accounts = api.accounts(refresh=True)
    today = date.today()
    checks = {
        "stock_instruments": lambda: api.data.instrument.get_instrument(["AAPL"]),
        "crypto_instruments": lambda: api.data.instrument.get_crypto_instrument(["BTCUSD"]),
        "option_contracts": lambda: api.data.instrument.get_option_contracts(
            underlying_symbols="AAPL", page_size=1
        ),
        "futures_products": lambda: api.data.instrument.get_futures_product_class("US_FUTURES"),
        "event_categories": api.data.instrument.get_event_categories,
        "watchlists": api.data.watchlist.get_watchlist,
        "company_profile": lambda: api.data.instrument.get_company_profile("AAPL"),
        "trade_calendar": lambda: api.trade.trade_calendar.get_trade_calendar(
            "US", today.isoformat(), (today + timedelta(days=7)).isoformat()
        ),
    }
    results = {}
    for name, check in checks.items():
        try:
            result = normalize_result(check())
            results[name] = {"ok": result.ok, "status": result.status}
        except Exception as exc:
            results[name] = {"ok": False, "error": type(exc).__name__}
    return emit({
        "account_classes": [account.get("account_class") for account in accounts],
        "checks": results,
        "catalog_targets": list(WebullAPI.TARGETS),
    })


def cmd_stream_quotes(_: WebullAPI, args: argparse.Namespace) -> int:
    from webull_streams import stream_quotes

    return stream_quotes(
        args.symbols, args.category, args.types, args.duration, args.transport, args.port
    )


def cmd_stream_trades(api: WebullAPI, args: argparse.Namespace) -> int:
    from webull_streams import stream_trade_events

    account_ids = [api.account_id(reference) for reference in args.accounts]
    return stream_trade_events(account_ids, args.duration)


def add_account_selector(parser: argparse.ArgumentParser, required: bool = False) -> None:
    group = parser.add_mutually_exclusive_group(required=required)
    group.add_argument("--account", help="Account ID, number, label, or class")
    group.add_argument("--account-class", help="For example INDIVIDUAL_MARGIN or CRYPTO")


def add_order_input(parser: argparse.ArgumentParser) -> None:
    add_account_selector(parser)
    parser.add_argument("--orders", help="JSON list or @file; bypasses the single-order builder")
    parser.add_argument("--symbol")
    parser.add_argument("--instrument-type", choices=sorted(ACCOUNT_BY_INSTRUMENT))
    parser.add_argument("--side", default="BUY", choices=("BUY", "SELL", "SHORT"))
    parser.add_argument("--quantity")
    parser.add_argument("--order-type", default="LIMIT")
    parser.add_argument("--limit-price")
    parser.add_argument("--stop-price")
    parser.add_argument("--tif", default="DAY", choices=("DAY", "GTC", "IOC"))
    parser.add_argument("--session", choices=("CORE", "ALL", "NIGHT"))
    parser.add_argument("--entrust-type", default="QTY", choices=("QTY", "AMOUNT"))
    parser.add_argument("--combo-type", default="NORMAL")
    parser.add_argument("--client-order-id")
    parser.add_argument("--client-combo-order-id")
    parser.add_argument("--event-outcome", choices=("yes", "no"))
    parser.add_argument("--option-strategy")
    parser.add_argument("--legs", help="Option legs as JSON or @file")
    parser.add_argument("--extra", help="Extra order fields as JSON or @file")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Complete Webull Paper Trading OpenAPI CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    doctor = sub.add_parser("doctor", help="Verify credentials and show account classes")
    doctor.set_defaults(handler=cmd_doctor)
    accounts = sub.add_parser("accounts", help="List all paper accounts")
    accounts.set_defaults(handler=cmd_accounts)

    for name, handler in (("balance", cmd_balance), ("positions", cmd_positions)):
        command = sub.add_parser(name)
        add_account_selector(command, required=True)
        command.set_defaults(handler=handler)

    activities = sub.add_parser("activities")
    add_account_selector(activities, required=True)
    activities.add_argument("--types")
    activities.add_argument("--start")
    activities.add_argument("--end")
    activities.add_argument("--cursor")
    activities.add_argument("--page-size", type=int, default=10)
    activities.set_defaults(handler=cmd_activities)

    orders = sub.add_parser("orders")
    orders_sub = orders.add_subparsers(dest="orders_command", required=True)
    open_orders = orders_sub.add_parser("open")
    history = orders_sub.add_parser("history")
    for command in (open_orders, history):
        add_account_selector(command, required=True)
        command.add_argument("--page-size", type=int, default=100)
        command.add_argument("--cursor")
    open_orders.set_defaults(handler=cmd_orders_open)
    history.add_argument("--start")
    history.add_argument("--end")
    history.set_defaults(handler=cmd_orders_history)

    order = sub.add_parser("order")
    order_sub = order.add_subparsers(dest="order_command", required=True)
    for name, handler in (("preview", cmd_order_preview), ("place", cmd_order_place)):
        command = order_sub.add_parser(name)
        add_order_input(command)
        if name == "place":
            command.add_argument("--yes", action="store_true")
        command.set_defaults(handler=handler)

    detail = order_sub.add_parser("detail")
    add_account_selector(detail, required=True)
    detail.add_argument("--client-order-id", required=True)
    detail.set_defaults(handler=cmd_order_detail)

    cancel = order_sub.add_parser("cancel")
    add_account_selector(cancel, required=True)
    cancel.add_argument("--client-order-id", required=True)
    cancel.add_argument("--yes", action="store_true")
    cancel.set_defaults(handler=cmd_order_cancel)

    replace = order_sub.add_parser("replace")
    add_account_selector(replace, required=True)
    replace.add_argument("--orders", required=True)
    replace.add_argument("--client-combo-order-id")
    replace.add_argument("--yes", action="store_true")
    replace.set_defaults(handler=cmd_order_replace)

    batch = order_sub.add_parser("batch")
    add_account_selector(batch, required=True)
    batch.add_argument("--batch-orders", required=True)
    batch.add_argument("--yes", action="store_true")
    batch.set_defaults(handler=cmd_order_batch)

    market = sub.add_parser("market")
    market_sub = market.add_subparsers(dest="market_command", required=True)
    snapshot = market_sub.add_parser("snapshot")
    snapshot.add_argument("--asset", choices=("stock", "crypto", "futures", "option", "event"), required=True)
    snapshot.add_argument("symbols", nargs="+")
    snapshot.add_argument("--category", default="US_STOCK")
    snapshot.set_defaults(handler=cmd_market_snapshot)

    bars = market_sub.add_parser("bars")
    bars.add_argument("--asset", choices=("stock", "crypto", "futures", "option", "event"), required=True)
    bars.add_argument("symbols", nargs="+")
    bars.add_argument("--category", default="US_STOCK")
    bars.add_argument("--timespan", default="D")
    bars.add_argument("--count", default="200")
    bars.set_defaults(handler=cmd_market_bars)

    ticks = market_sub.add_parser("ticks")
    ticks.add_argument("--asset", choices=("stock", "futures", "option", "event"), required=True)
    ticks.add_argument("symbol")
    ticks.add_argument("--category", default="US_STOCK")
    ticks.add_argument("--count", default="30")
    ticks.set_defaults(handler=cmd_market_ticks)

    depth = market_sub.add_parser("depth")
    depth.add_argument("--asset", choices=("stock", "futures", "event"), required=True)
    depth.add_argument("symbol")
    depth.add_argument("--category", default="US_STOCK")
    depth.add_argument("--depth", type=int)
    depth.set_defaults(handler=cmd_market_depth)

    instruments = sub.add_parser("instruments")
    instruments.add_argument("--asset", choices=("stock", "crypto", "futures", "option"), required=True)
    instruments.add_argument("symbols", nargs="*")
    instruments.add_argument("--category", default="US_STOCK")
    instruments.add_argument("--code")
    instruments.add_argument("--extra", help="Additional method kwargs as JSON or @file")
    instruments.set_defaults(handler=cmd_instruments)

    events = sub.add_parser("events")
    events.add_argument("mode", choices=("categories", "series", "events", "markets"))
    events.add_argument("--category")
    events.add_argument("--series")
    events.add_argument("--extra")
    events.set_defaults(handler=cmd_event_discovery)

    catalog = sub.add_parser("catalog", help="List every exposed official SDK method")
    catalog.add_argument("--target", choices=WebullAPI.TARGETS)
    catalog.set_defaults(handler=cmd_catalog)

    call = sub.add_parser("call", help="Call any cataloged official SDK method")
    call.add_argument("path", help="For example data.market.get_snapshot")
    call.add_argument("--args", default="[]", help="JSON list or @file")
    call.add_argument("--kwargs", default="{}", help="JSON object or @file")
    call.add_argument("--account", help="Replaces every @account JSON value with the selected account ID")
    call.add_argument("--yes", action="store_true", help="Required for mutations")
    call.set_defaults(handler=cmd_call)

    capabilities = sub.add_parser("capabilities", help="Probe safe endpoints available to this key")
    capabilities.set_defaults(handler=cmd_capabilities)

    stream = sub.add_parser("stream")
    stream_sub = stream.add_subparsers(dest="stream_command", required=True)
    quotes = stream_sub.add_parser("quotes")
    quotes.add_argument("symbols", nargs="+")
    quotes.add_argument("--category", default="US_STOCK")
    quotes.add_argument("--types", nargs="+", default=["QUOTE", "SNAPSHOT", "TICK"])
    quotes.add_argument("--duration", type=int, default=30)
    quotes.add_argument("--transport", choices=("tcp", "websockets"), default="tcp")
    quotes.add_argument("--port", type=int, help="Default: 1883 for TCP, 8883 for WebSocket")
    quotes.set_defaults(handler=cmd_stream_quotes)
    trades = stream_sub.add_parser("trades")
    trades.add_argument("accounts", nargs="+", help="Account classes, numbers, labels, or IDs")
    trades.add_argument("--duration", type=int, help="Stop after N seconds; otherwise run until Ctrl+C")
    trades.set_defaults(handler=cmd_stream_trades)

    return parser


def main(argv: list[str] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.handler(WebullAPI(), args)
    except Exception as exc:
        print(json.dumps({
            "ok": False,
            "error": type(exc).__name__,
            "message": redact_secrets(exc),
        }, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

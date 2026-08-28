from __future__ import annotations

import hashlib
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from webull_api import WebullAPI, normalize_result


SYMBOLS = ("BTCUSD", "ETHUSD")
COINBASE_PRODUCTS = {"BTCUSD": "BTC-USD", "ETHUSD": "ETH-USD"}
INITIAL_CAPITAL = Decimal("1000000")
ALLOCATION = Decimal("0.005")
STOP_LOSS = Decimal("0.03")
SPREADS = (Decimal("0.005"), Decimal("0.01"), Decimal("0.015"))
BASE_SPREAD = Decimal("0.01")
LOT_SIZE = Decimal("0.00000001")
REPORT_JSON = Path(__file__).parent / "reports" / "crypto-backtest-90d.json"
REPORT_MARKDOWN = Path(__file__).parent / "reports" / "crypto-backtest-90d.md"
CACHE_DIR = Path(__file__).parent / ".cache" / "crypto"


@dataclass(frozen=True)
class Bar:
    time: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal = Decimal("0")


@dataclass(frozen=True)
class DataQuality:
    expected_bars: int
    actual_bars: int
    coverage: float
    duplicates: int
    max_gap_seconds: int
    passed: bool


@dataclass(frozen=True)
class Trade:
    entry_time: datetime
    exit_time: datetime
    entry_price: Decimal
    exit_price: Decimal
    quantity: Decimal
    pnl: Decimal
    reason: str


def parse_time(value: str) -> datetime:
    normalized = value.replace("Z", "+00:00")
    if len(normalized) >= 5 and normalized[-5] in "+-" and normalized[-3] != ":":
        normalized = f"{normalized[:-2]}:{normalized[-2:]}"
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def floor_time(value: datetime, seconds: int) -> datetime:
    value = value.astimezone(timezone.utc)
    return datetime.fromtimestamp(int(value.timestamp()) // seconds * seconds, timezone.utc)


def normalize_bars(
    bars: Iterable[Bar],
    *,
    interval_seconds: int,
    now: Optional[datetime] = None,
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
) -> tuple[list[Bar], int]:
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    unique: dict[datetime, Bar] = {}
    duplicates = 0
    for bar in bars:
        if bar.time in unique:
            duplicates += 1
        unique[bar.time] = bar
    result = []
    for bar in sorted(unique.values(), key=lambda item: item.time):
        if bar.time + timedelta(seconds=interval_seconds) > now:
            continue
        if start and bar.time < start:
            continue
        if end and bar.time >= end:
            continue
        result.append(bar)
    return result, duplicates


def assess_quality(
    bars: list[Bar],
    *,
    start: datetime,
    end: datetime,
    interval_seconds: int,
    duplicates: int = 0,
) -> DataQuality:
    expected = max(0, int((end - start).total_seconds() // interval_seconds))
    actual = len(bars)
    gaps = [
        int((right.time - left.time).total_seconds())
        for left, right in zip(bars, bars[1:])
    ]
    max_gap = max(gaps, default=interval_seconds)
    coverage = actual / expected if expected else 0.0
    return DataQuality(
        expected_bars=expected,
        actual_bars=actual,
        coverage=coverage,
        duplicates=duplicates,
        max_gap_seconds=max_gap,
        passed=coverage >= 0.995 and max_gap <= interval_seconds * 2,
    )


def webull_bars(
    api: WebullAPI,
    symbols: Iterable[str],
    *,
    timespan: str = "M120",
    count: int = 1200,
    now: Optional[datetime] = None,
    days: int = 90,
) -> dict[str, tuple[list[Bar], DataQuality]]:
    interval_seconds = {"M5": 300, "M120": 7200}[timespan]
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    end = floor_time(now, interval_seconds)
    start = end - timedelta(days=days)
    result = normalize_result(api.data.crypto_market_data.get_crypto_history_bar(
        list(symbols), "US_CRYPTO", timespan, str(count)
    ))
    if not result.ok or not isinstance(result.data, list):
        raise RuntimeError(f"Unable to retrieve Webull crypto bars: {result.status}")
    output: dict[str, tuple[list[Bar], DataQuality]] = {}
    for item in result.data:
        raw = item.get("result", [])
        parsed = [
            Bar(
                time=parse_time(row["time"]),
                open=Decimal(str(row["open"])),
                high=Decimal(str(row["high"])),
                low=Decimal(str(row["low"])),
                close=Decimal(str(row["close"])),
            )
            for row in raw
        ]
        bars, duplicates = normalize_bars(
            parsed,
            interval_seconds=interval_seconds,
            now=now,
            start=start,
            end=end,
        )
        output[str(item["symbol"])] = (
            bars,
            assess_quality(
                bars,
                start=start,
                end=end,
                interval_seconds=interval_seconds,
                duplicates=duplicates,
            ),
        )
    missing = set(symbols) - set(output)
    if missing:
        raise RuntimeError(f"Webull did not return bars for: {', '.join(sorted(missing))}")
    return output


def _request_json(url: str, attempts: int = 3) -> Any:
    request = urllib.request.Request(url, headers={"User-Agent": "webull-sandbox-backtest/1.0"})
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            if exc.code not in {429, 500, 502, 503, 504} or attempt == attempts - 1:
                raise
        except urllib.error.URLError:
            if attempt == attempts - 1:
                raise
        time.sleep(2 ** attempt)
    raise RuntimeError("Coinbase request failed")


def coinbase_bars(
    symbol: str,
    *,
    start: datetime,
    end: datetime,
    request_json: Callable[[str], Any] = _request_json,
    sleep: Callable[[float], None] = time.sleep,
    cache_dir: Path = CACHE_DIR,
) -> tuple[list[Bar], DataQuality]:
    product = COINBASE_PRODUCTS[symbol]
    interval_seconds = 300
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_key = hashlib.sha256(
        f"{product}:{int(start.timestamp())}:{int(end.timestamp())}:M5".encode()
    ).hexdigest()[:16]
    cache_file = cache_dir / f"coinbase-{product}-{cache_key}.json"
    if cache_file.exists():
        payload = json.loads(cache_file.read_text(encoding="utf-8"))
    else:
        payload = []
        cursor = start
        while cursor < end:
            # Stay below Coinbase's 300-candle cap. Adjacent pages may still
            # include the same boundary candle, which normalize_bars deduplicates.
            chunk_end = min(cursor + timedelta(seconds=interval_seconds * 299), end)
            query = urllib.parse.urlencode({
                "granularity": interval_seconds,
                "start": cursor.isoformat(),
                "end": chunk_end.isoformat(),
            })
            url = f"https://api.exchange.coinbase.com/products/{product}/candles?{query}"
            rows = request_json(url)
            if not isinstance(rows, list):
                raise RuntimeError(f"Unexpected Coinbase response for {product}")
            payload.extend(rows)
            cursor = chunk_end
            if cursor < end:
                sleep(0.12)
        temporary = cache_file.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
        temporary.replace(cache_file)
    parsed = [
        Bar(
            time=datetime.fromtimestamp(int(row[0]), timezone.utc),
            low=Decimal(str(row[1])),
            high=Decimal(str(row[2])),
            open=Decimal(str(row[3])),
            close=Decimal(str(row[4])),
            volume=Decimal(str(row[5])) if len(row) > 5 else Decimal("0"),
        )
        for row in payload
        if isinstance(row, list) and len(row) >= 5
    ]
    bars, duplicates = normalize_bars(
        parsed,
        interval_seconds=interval_seconds,
        now=end,
        start=start,
        end=end,
    )
    return bars, assess_quality(
        bars,
        start=start,
        end=end,
        interval_seconds=interval_seconds,
        duplicates=duplicates,
    )


def ema(values: list[Decimal], period: int) -> list[Optional[Decimal]]:
    output: list[Optional[Decimal]] = [None] * len(values)
    if len(values) < period:
        return output
    current = sum(values[:period]) / Decimal(period)
    output[period - 1] = current
    alpha = Decimal(2) / Decimal(period + 1)
    for index in range(period, len(values)):
        current = values[index] * alpha + current * (Decimal(1) - alpha)
        output[index] = current
    return output


def heikin_ashi_bullish(bars: list[Bar]) -> list[bool]:
    result: list[bool] = []
    previous_open: Optional[Decimal] = None
    previous_close: Optional[Decimal] = None
    for bar in bars:
        close = (bar.open + bar.high + bar.low + bar.close) / Decimal(4)
        open_ = (
            (bar.open + bar.close) / Decimal(2)
            if previous_open is None
            else (previous_open + previous_close) / Decimal(2)
        )
        result.append(close > open_)
        previous_open, previous_close = open_, close
    return result


def signals(bars: list[Bar]) -> list[str]:
    closes = [bar.close for bar in bars]
    fast = ema(closes, 20)
    slow = ema(closes, 50)
    bullish = heikin_ashi_bullish(bars)
    output = ["HOLD"] * len(bars)
    for index in range(50, len(bars)):
        previous_fast, previous_slow = fast[index - 1], slow[index - 1]
        current_fast, current_slow = fast[index], slow[index]
        if None in {previous_fast, previous_slow, current_fast, current_slow}:
            continue
        if previous_fast <= previous_slow and current_fast > current_slow and bullish[index]:
            output[index] = "BUY"
        elif previous_fast >= previous_slow and current_fast < current_slow:
            output[index] = "SELL"
    return output


def quantity_for_notional(
    notional: Decimal,
    price: Decimal,
    lot_size: Decimal = LOT_SIZE,
) -> Decimal:
    if notional <= 0 or price <= 0 or lot_size <= 0:
        raise ValueError("Notional, price, and lot size must be positive")
    steps = (notional / price / lot_size).to_integral_value(rounding=ROUND_DOWN)
    return steps * lot_size


def deterministic_order_id(
    symbol: str,
    candle_time: datetime,
    side: str,
    *,
    strategy: str = "crypto-trend-v1",
) -> str:
    raw = f"{strategy}:{symbol}:{candle_time.isoformat()}:{side.upper()}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def _number(value: Decimal) -> float:
    return round(float(value), 8)


def backtest(
    bars: list[Bar],
    *,
    spread: Decimal = BASE_SPREAD,
    initial_capital: Decimal = INITIAL_CAPITAL,
    allocation: Decimal = ALLOCATION,
    stop_loss: Optional[Decimal] = STOP_LOSS,
    signal_values: Optional[list[str]] = None,
) -> dict[str, Any]:
    if len(bars) < 52:
        raise ValueError("At least 52 closed bars are required")
    generated_signals = signal_values if signal_values is not None else signals(bars)
    if len(generated_signals) != len(bars):
        raise ValueError("Signal count must match bar count")
    cash = initial_capital
    quantity = Decimal("0")
    entry_price = Decimal("0")
    entry_time: Optional[datetime] = None
    pending = "HOLD"
    trades: list[Trade] = []
    equity_curve = [initial_capital]

    for index, bar in enumerate(bars):
        if pending == "BUY" and quantity == 0:
            price = bar.open * (Decimal("1") + spread)
            quantity = quantity_for_notional(cash * allocation, price)
            if quantity > 0:
                entry_price = price
                entry_time = bar.time
                cash -= quantity * price
        elif pending == "SELL" and quantity > 0:
            price = bar.open * (Decimal("1") - spread)
            pnl = quantity * (price - entry_price)
            cash += quantity * price
            trades.append(Trade(entry_time, bar.time, entry_price, price, quantity, pnl, "signal"))
            quantity, entry_price, entry_time = Decimal("0"), Decimal("0"), None

        if quantity > 0 and stop_loss is not None:
            stop_price = entry_price * (Decimal("1") - stop_loss)
            executable_low = bar.low * (Decimal("1") - spread)
            if executable_low <= stop_price:
                executable_open = bar.open * (Decimal("1") - spread)
                price = min(executable_open, stop_price)
                pnl = quantity * (price - entry_price)
                cash += quantity * price
                trades.append(Trade(entry_time, bar.time, entry_price, price, quantity, pnl, "stop"))
                quantity, entry_price, entry_time = Decimal("0"), Decimal("0"), None

        liquidation = quantity * bar.close * (Decimal("1") - spread)
        equity_curve.append(cash + liquidation)
        pending = generated_signals[index]

    if quantity > 0:
        bar = bars[-1]
        price = bar.close * (Decimal("1") - spread)
        pnl = quantity * (price - entry_price)
        cash += quantity * price
        trades.append(Trade(entry_time, bar.time, entry_price, price, quantity, pnl, "end"))
        equity_curve.append(cash)

    net_profit = cash - initial_capital
    wins = [trade for trade in trades if trade.pnl > 0]
    losses = [trade for trade in trades if trade.pnl < 0]
    gross_profit = sum((trade.pnl for trade in wins), Decimal("0"))
    gross_loss = -sum((trade.pnl for trade in losses), Decimal("0"))
    peak = equity_curve[0]
    max_drawdown = Decimal("0")
    for value in equity_curve:
        peak = max(peak, value)
        if peak:
            max_drawdown = max(max_drawdown, (peak - value) / peak)
    holding_hours = [
        (trade.exit_time - trade.entry_time).total_seconds() / 3600 for trade in trades
    ]
    benchmark_entry = bars[0].open * (Decimal("1") + spread)
    benchmark_qty = quantity_for_notional(initial_capital * allocation, benchmark_entry)
    benchmark_exit = bars[-1].close * (Decimal("1") - spread)
    benchmark_pnl = benchmark_qty * (benchmark_exit - benchmark_entry)
    return {
        "spread_per_side": _number(spread),
        "net_profit": _number(net_profit),
        "account_return": _number(net_profit / initial_capital),
        "allocated_return": _number(net_profit / (initial_capital * allocation)),
        "trade_count": len(trades),
        "win_rate": round(len(wins) / len(trades), 6) if trades else 0.0,
        "profit_factor": round(float(gross_profit / gross_loss), 6) if gross_loss else None,
        "max_drawdown": _number(max_drawdown),
        "average_holding_hours": round(sum(holding_hours) / len(holding_hours), 2) if trades else 0.0,
        "buy_hold_profit": _number(benchmark_pnl),
        "trades": [
            {
                **asdict(trade),
                "entry_time": trade.entry_time.isoformat(),
                "exit_time": trade.exit_time.isoformat(),
                "entry_price": _number(trade.entry_price),
                "exit_price": _number(trade.exit_price),
                "quantity": _number(trade.quantity),
                "pnl": _number(trade.pnl),
            }
            for trade in trades
        ],
    }


def _dataset_result(
    bars: list[Bar],
    quality: DataQuality,
    *,
    deployment_gate: bool,
) -> dict[str, Any]:
    costs = {f"{float(spread):.3f}": backtest(bars, spread=spread) for spread in SPREADS}
    base = costs[f"{float(BASE_SPREAD):.3f}"]
    eligible = bool(
        deployment_gate
        and quality.passed
        and base["net_profit"] > 0
        and base["trade_count"] >= 3
    )
    return {
        "quality": asdict(quality),
        "first_bar": bars[0].time.isoformat() if bars else None,
        "last_bar": bars[-1].time.isoformat() if bars else None,
        "costs": costs,
        "eligible": eligible,
    }


def run_backtests(
    api: WebullAPI,
    *,
    days: int = 90,
    source: str = "both",
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    if days != 90:
        raise ValueError("This experiment is fixed to a 90-day backtest")
    if source not in {"both", "webull", "coinbase"}:
        raise ValueError("source must be both, webull, or coinbase")
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    report: dict[str, Any] = {
        "generated_at": now.isoformat(),
        "days": days,
        "symbols": list(SYMBOLS),
        "strategy": {
            "entry": "EMA20 crosses above EMA50 and Heikin-Ashi is bullish",
            "exit": "EMA20 crosses below EMA50 or 3% stop-loss",
            "allocation_per_symbol": float(ALLOCATION),
            "long_only": True,
        },
        "sources": {},
        "deployment_symbols": [],
    }
    if source in {"both", "webull"}:
        datasets = webull_bars(api, SYMBOLS, now=now, days=days)
        report["sources"]["webull_m120"] = {
            "timeframe": "M120",
            "symbols": {
                symbol: _dataset_result(bars, quality, deployment_gate=True)
                for symbol, (bars, quality) in datasets.items()
            },
        }
    if source in {"both", "coinbase"}:
        end = floor_time(now, 300)
        start = end - timedelta(days=days)
        report["sources"]["coinbase_m5"] = {
            "timeframe": "M5",
            "diagnostic_only": True,
            "symbols": {},
        }
        for symbol in SYMBOLS:
            bars, quality = coinbase_bars(symbol, start=start, end=end)
            report["sources"]["coinbase_m5"]["symbols"][symbol] = _dataset_result(
                bars, quality, deployment_gate=False
            )
    webull = report["sources"].get("webull_m120", {}).get("symbols", {})
    report["deployment_symbols"] = [
        symbol for symbol in SYMBOLS if webull.get(symbol, {}).get("eligible")
    ]
    return report


def markdown_report(report: dict[str, Any]) -> str:
    lines = [
        "# Webull Crypto Sandbox 90天回测",
        "",
        f"生成时间：{report['generated_at']}",
        "",
        "策略：EMA20/EMA50 趋势交叉 + Heikin-Ashi 入场确认；只做多；每币0.5%仓位；3%止损。",
        "基础成本模型：买入和卖出各计1% Webull价差；同时测试0.5%和1.5%。",
        "",
        "| 数据源 | 标的 | 数据检查 | 覆盖率 | 成本/边 | 净收益 | 分配资金收益率 | 交易数 | 胜率 | 利润因子 | 最大回撤 | 平均持仓小时 | 买入持有收益 | 放行 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for source_name, source in report["sources"].items():
        for symbol, item in source["symbols"].items():
            for spread_key, metrics in item["costs"].items():
                profit_factor = metrics["profit_factor"]
                lines.append(
                    "| {source} | {symbol} | {quality} | {coverage:.2%} | {spread:.1%} | ${pnl:,.2f} | {allocated:.2%} | "
                    "{trades} | {wins:.1%} | {pf} | {drawdown:.2%} | {hours:.2f} | ${hold:,.2f} | {eligible} |".format(
                        source=source_name,
                        symbol=symbol,
                        quality="通过" if item["quality"]["passed"] else "失败",
                        coverage=item["quality"]["coverage"],
                        spread=float(spread_key),
                        pnl=metrics["net_profit"],
                        allocated=metrics["allocated_return"],
                        trades=metrics["trade_count"],
                        wins=metrics["win_rate"],
                        pf="—" if profit_factor is None else f"{profit_factor:.2f}",
                        drawdown=metrics["max_drawdown"],
                        hours=metrics["average_holding_hours"],
                        hold=metrics["buy_hold_profit"],
                        eligible="是" if item["eligible"] and spread_key == "0.010" else "否",
                    )
                )
    deployment = "、".join(report["deployment_symbols"]) or "无"
    lines.extend([
        "",
        f"## 自动模拟交易放行结果：{deployment}",
        "",
        "只有 Webull M120、每边1%成本、净收益为正、至少3笔交易且数据完整的标的可放行。Coinbase M5仅作诊断。",
        "",
    ])
    return "\n".join(lines)


def save_report(report: dict[str, Any]) -> tuple[Path, Path]:
    REPORT_JSON.parent.mkdir(parents=True, exist_ok=True)
    REPORT_JSON.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    REPORT_MARKDOWN.write_text(markdown_report(report), encoding="utf-8")
    return REPORT_JSON, REPORT_MARKDOWN


def load_report() -> dict[str, Any]:
    if not REPORT_JSON.exists():
        raise RuntimeError("Run crypto-strategy backtest before starting the experiment")
    return json.loads(REPORT_JSON.read_text(encoding="utf-8"))

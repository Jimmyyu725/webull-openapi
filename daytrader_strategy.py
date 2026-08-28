from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional

from crypto_strategy import (
    Bar,
    DataQuality,
    SYMBOLS,
    coinbase_bars,
    ema,
    floor_time,
    quantity_for_notional,
    webull_bars,
)
from webull_api import WebullAPI


TIMEFRAME = "M5"
INTERVAL_SECONDS = 300
FAST_EMA = 20
SLOW_EMA = 100
BREAKOUT_BARS = 48
ALLOCATION = Decimal("0.001")
STOP_LOSS = Decimal("0.02")
TAKE_PROFIT = Decimal("0.05")
MAX_HOLD_BARS = 288
COSTS = (Decimal("0"), Decimal("0.0025"), Decimal("0.01"))
REPORT_JSON = Path(__file__).parent / "reports" / "daytrader-backtest-90d.json"
REPORT_MARKDOWN = Path(__file__).parent / "reports" / "daytrader-backtest-90d.md"


@dataclass(frozen=True)
class DayTrade:
    entry_time: datetime
    exit_time: datetime
    entry_price: Decimal
    exit_price: Decimal
    quantity: Decimal
    pnl: Decimal
    reason: str


def daytrade_signals(bars: list[Bar]) -> list[str]:
    """Return close-of-bar signals; execution belongs to the next bar."""
    closes = [bar.close for bar in bars]
    fast = ema(closes, FAST_EMA)
    slow = ema(closes, SLOW_EMA)
    output = ["HOLD"] * len(bars)
    warmup = max(SLOW_EMA, BREAKOUT_BARS)
    for index in range(warmup, len(bars)):
        if fast[index] is None or slow[index] is None:
            continue
        prior_high = max(bar.high for bar in bars[index - BREAKOUT_BARS:index])
        if fast[index] > slow[index] and bars[index].close > prior_high:
            output[index] = "BUY"
        elif bars[index].close < fast[index]:
            output[index] = "SELL"
    return output


def _number(value: Decimal) -> float:
    return round(float(value), 8)


def backtest_daytrader(
    bars: list[Bar],
    *,
    cost_per_side: Decimal,
    initial_capital: Decimal = Decimal("1000000"),
    allocation: Decimal = ALLOCATION,
) -> dict[str, Any]:
    if len(bars) <= SLOW_EMA + 1:
        raise ValueError("At least 102 closed M5 bars are required")
    signals = daytrade_signals(bars)
    cash = initial_capital
    quantity = Decimal("0")
    entry_price = Decimal("0")
    entry_time: Optional[datetime] = None
    pending = "HOLD"
    held_bars = 0
    last_entry_day = None
    trades: list[DayTrade] = []
    equity_curve = [initial_capital]

    for index, bar in enumerate(bars):
        if pending == "BUY" and quantity == 0 and bar.time.date() != last_entry_day:
            entry_price = bar.open * (Decimal("1") + cost_per_side)
            quantity = quantity_for_notional(cash * allocation, entry_price)
            if quantity > 0:
                cash -= quantity * entry_price
                entry_time = bar.time
                last_entry_day = bar.time.date()
                held_bars = 0
        elif pending == "SELL" and quantity > 0:
            exit_price = bar.open * (Decimal("1") - cost_per_side)
            pnl = quantity * (exit_price - entry_price)
            cash += quantity * exit_price
            trades.append(DayTrade(entry_time, bar.time, entry_price, exit_price, quantity, pnl, "signal"))
            quantity, entry_price, entry_time, held_bars = Decimal("0"), Decimal("0"), None, 0

        pending = "HOLD"
        if quantity > 0:
            held_bars += 1
            executable_open = bar.open * (Decimal("1") - cost_per_side)
            executable_low = bar.low * (Decimal("1") - cost_per_side)
            executable_high = bar.high * (Decimal("1") - cost_per_side)
            stop_price = entry_price * (Decimal("1") - STOP_LOSS)
            target_price = entry_price * (Decimal("1") + TAKE_PROFIT)
            reason = None
            exit_price = Decimal("0")
            if executable_low <= stop_price:
                exit_price = min(executable_open, stop_price)
                reason = "stop"
            elif executable_high >= target_price:
                exit_price = max(executable_open, target_price)
                reason = "target"
            if reason:
                pnl = quantity * (exit_price - entry_price)
                cash += quantity * exit_price
                trades.append(DayTrade(entry_time, bar.time, entry_price, exit_price, quantity, pnl, reason))
                quantity, entry_price, entry_time, held_bars = Decimal("0"), Decimal("0"), None, 0
            elif held_bars >= MAX_HOLD_BARS or signals[index] == "SELL":
                pending = "SELL"

        if quantity == 0 and signals[index] == "BUY":
            pending = "BUY"
        liquidation = quantity * bar.close * (Decimal("1") - cost_per_side)
        equity_curve.append(cash + liquidation)

    if quantity > 0:
        bar = bars[-1]
        exit_price = bar.close * (Decimal("1") - cost_per_side)
        pnl = quantity * (exit_price - entry_price)
        cash += quantity * exit_price
        trades.append(DayTrade(entry_time, bar.time, entry_price, exit_price, quantity, pnl, "end"))
        equity_curve.append(cash)

    wins = [trade for trade in trades if trade.pnl > 0]
    losses = [trade for trade in trades if trade.pnl < 0]
    gross_profit = sum((trade.pnl for trade in wins), Decimal("0"))
    gross_loss = -sum((trade.pnl for trade in losses), Decimal("0"))
    peak = equity_curve[0]
    drawdown = Decimal("0")
    for value in equity_curve:
        peak = max(peak, value)
        if peak:
            drawdown = max(drawdown, (peak - value) / peak)
    holding_hours = [
        (trade.exit_time - trade.entry_time).total_seconds() / 3600
        for trade in trades
    ]
    return {
        "cost_per_side": _number(cost_per_side),
        "net_profit": _number(cash - initial_capital),
        "account_return": _number((cash - initial_capital) / initial_capital),
        "allocated_return": _number((cash - initial_capital) / (initial_capital * allocation)),
        "trade_count": len(trades),
        "win_rate": round(len(wins) / len(trades), 6) if trades else 0.0,
        "profit_factor": round(float(gross_profit / gross_loss), 6) if gross_loss else None,
        "max_drawdown": _number(drawdown),
        "average_holding_hours": round(sum(holding_hours) / len(holding_hours), 2) if trades else 0.0,
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


def _dataset(bars: list[Bar], quality: DataQuality) -> dict[str, Any]:
    return {
        "quality": asdict(quality),
        "first_bar": bars[0].time.isoformat(),
        "last_bar": bars[-1].time.isoformat(),
        "costs": {
            f"{float(cost):.4f}": backtest_daytrader(bars, cost_per_side=cost)
            for cost in COSTS
        },
    }


def run_daytrader_backtest(
    api: WebullAPI,
    *,
    days: int = 90,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    if days != 90:
        raise ValueError("This comparison is fixed to 90 days")
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    end = floor_time(now, INTERVAL_SECONDS)
    start = end - timedelta(days=days)
    report: dict[str, Any] = {
        "generated_at": now.isoformat(),
        "days": days,
        "strategy": {
            "name": "M5 trend breakout Sandbox day trader",
            "entry": f"EMA{FAST_EMA} above EMA{SLOW_EMA} and close breaks the prior {BREAKOUT_BARS} bars",
            "exit": f"EMA{FAST_EMA} loss, {STOP_LOSS:.0%} stop, {TAKE_PROFIT:.0%} target, or 24-hour limit",
            "allocation": float(ALLOCATION),
            "one_entry_per_symbol_per_utc_day": True,
            "long_only": True,
        },
        "sources": {},
        "sandbox_symbols": list(SYMBOLS),
    }
    coinbase = {}
    for symbol in SYMBOLS:
        bars, quality = coinbase_bars(symbol, start=start, end=end)
        coinbase[symbol] = _dataset(bars, quality)
    report["sources"]["coinbase_m5_90d"] = {
        "timeframe": TIMEFRAME,
        "symbols": coinbase,
    }
    live = webull_bars(api, SYMBOLS, timespan=TIMEFRAME, count=1200, now=now, days=4)
    report["sources"]["webull_m5_recent"] = {
        "timeframe": TIMEFRAME,
        "diagnostic_only": True,
        "symbols": {
            symbol: _dataset(bars, quality)
            for symbol, (bars, quality) in live.items()
        },
    }
    return report


def markdown_report(report: dict[str, Any]) -> str:
    lines = [
        "# Webull Crypto Sandbox 日内策略回测",
        "",
        f"生成时间：{report['generated_at']}",
        "",
        "策略：M5 趋势突破，只做多；每次使用0.1%购买力；每币每天最多入场一次；2%止损、5%止盈、最长持仓24小时。",
        "信号在K线收盘生成，下一根开盘成交；成本测试为每边0%、0.25%和1%。",
        "",
        "| 数据源 | 标的 | 数据检查 | 成本/边 | 净收益 | 账户收益率 | 分配资金收益率 | 交易数 | 胜率 | 利润因子 | 最大回撤 | 平均持仓小时 |",
        "|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for source_name, source in report["sources"].items():
        for symbol, item in source["symbols"].items():
            for cost, metrics in item["costs"].items():
                factor = metrics["profit_factor"]
                lines.append(
                    f"| {source_name} | {symbol} | {'通过' if item['quality']['passed'] else '失败'} | "
                    f"{float(cost):.2%} | ${metrics['net_profit']:,.2f} | {metrics['account_return']:.3%} | "
                    f"{metrics['allocated_return']:.1%} | {metrics['trade_count']} | {metrics['win_rate']:.1%} | "
                    f"{'—' if factor is None else f'{factor:.2f}'} | {metrics['max_drawdown']:.3%} | "
                    f"{metrics['average_holding_hours']:.2f} |"
                )
    lines.extend([
        "",
        "## 结论",
        "",
        "这是Sandbox学习模式，不是收益承诺。Webull加密货币每边约1%的成本会显著压低日内策略结果；自动运行采用极小仓位和硬性频率限制。",
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
        raise RuntimeError("Run daytrade-backtest before installing the Sandbox day trader")
    return json.loads(REPORT_JSON.read_text(encoding="utf-8"))

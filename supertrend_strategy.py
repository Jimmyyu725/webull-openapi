# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
# SuperTrend rules adapted from "SuperTrend STRATEGY" © KivancOzbilgic.

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional

from crypto_strategy import (
    ALLOCATION,
    BASE_SPREAD,
    SPREADS,
    SYMBOLS,
    Bar,
    backtest,
    coinbase_bars,
    floor_time,
    load_report,
    webull_bars,
)
from webull_api import WebullAPI


REPORT_JSON = Path(__file__).parent / "reports" / "supertrend-backtest-90d.json"
REPORT_MARKDOWN = Path(__file__).parent / "reports" / "supertrend-backtest-90d.md"
ALLOCATIONS = (ALLOCATION, Decimal("0.45"))
DIAGNOSTIC_SPREADS = (Decimal("0"), Decimal("0.0025"), *SPREADS)


def wilder_atr(bars: list[Bar], period: int = 10) -> list[Optional[Decimal]]:
    if period <= 0:
        raise ValueError("ATR period must be positive")
    ranges = []
    for index, bar in enumerate(bars):
        previous_close = bars[index - 1].close if index else bar.close
        ranges.append(max(
            bar.high - bar.low,
            abs(bar.high - previous_close),
            abs(bar.low - previous_close),
        ))
    result: list[Optional[Decimal]] = [None] * len(bars)
    if len(ranges) < period:
        return result
    current = sum(ranges[:period]) / Decimal(period)
    result[period - 1] = current
    for index in range(period, len(ranges)):
        current = (current * Decimal(period - 1) + ranges[index]) / Decimal(period)
        result[index] = current
    return result


def supertrend_signals(
    bars: list[Bar],
    *,
    period: int = 10,
    multiplier: Decimal = Decimal("3"),
) -> list[str]:
    atr = wilder_atr(bars, period)
    signals = ["HOLD"] * len(bars)
    trend = 1
    previous_up: Optional[Decimal] = None
    previous_down: Optional[Decimal] = None
    for index, bar in enumerate(bars):
        if atr[index] is None:
            continue
        source = (bar.high + bar.low) / Decimal(2)
        raw_up = source - multiplier * atr[index]
        raw_down = source + multiplier * atr[index]
        if previous_up is None:
            up, down = raw_up, raw_down
        else:
            prior_close = bars[index - 1].close
            up = max(raw_up, previous_up) if prior_close > previous_up else raw_up
            down = min(raw_down, previous_down) if prior_close < previous_down else raw_down
        previous_trend = trend
        if trend == -1 and previous_down is not None and bar.close > previous_down:
            trend = 1
        elif trend == 1 and previous_up is not None and bar.close < previous_up:
            trend = -1
        if trend == 1 and previous_trend == -1:
            signals[index] = "BUY"
        elif trend == -1 and previous_trend == 1:
            signals[index] = "SELL"
        previous_up, previous_down = up, down
    return signals


def _dataset(bars: list[Bar], quality: Any) -> dict[str, Any]:
    generated = supertrend_signals(bars)
    allocations = {}
    for allocation in ALLOCATIONS:
        costs = {}
        for spread in DIAGNOSTIC_SPREADS:
            metrics = backtest(
                bars,
                spread=spread,
                allocation=allocation,
                stop_loss=None,
                signal_values=generated,
            )
            metrics.pop("trades", None)
            metrics["excess_vs_buy_hold"] = round(
                metrics["net_profit"] - metrics["buy_hold_profit"], 8
            )
            costs[f"{float(spread):.4f}"] = metrics
        allocations[f"{float(allocation):.3f}"] = costs
    base = allocations["0.450"][f"{float(BASE_SPREAD):.4f}"]
    return {
        "quality": asdict(quality),
        "first_bar": bars[0].time.isoformat(),
        "last_bar": bars[-1].time.isoformat(),
        "allocations": allocations,
        "beats_buy_hold_at_45pct": base["excess_vs_buy_hold"] > 0,
    }


def run_supertrend_backtests(
    api: WebullAPI,
    *,
    days: int = 90,
    source: str = "both",
) -> dict[str, Any]:
    if days != 90:
        raise ValueError("This comparison is fixed to 90 days")
    if source not in {"both", "webull", "coinbase"}:
        raise ValueError("source must be both, webull, or coinbase")
    baseline = load_report()
    now = datetime.fromisoformat(baseline["generated_at"]).astimezone(timezone.utc)
    report: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "comparison_period_end": now.isoformat(),
        "days": days,
        "strategy": {
            "name": "SuperTrend",
            "atr_period": 10,
            "atr_multiplier": 3.0,
            "atr_method": "Wilder RMA",
            "mode": "Webull-compatible long-only; SELL closes the long position",
            "stop_loss": None,
        },
        "sources": {},
    }
    if source in {"both", "webull"}:
        datasets = webull_bars(api, SYMBOLS, now=now, days=days)
        report["sources"]["webull_m120"] = {
            "timeframe": "M120",
            "symbols": {
                symbol: _dataset(bars, quality)
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
            report["sources"]["coinbase_m5"]["symbols"][symbol] = _dataset(bars, quality)
    return report


def markdown_report(report: dict[str, Any]) -> str:
    lines = [
        "# SuperTrend 90天回测",
        "",
        "参数：ATR 10、倍数3、Wilder ATR；Webull兼容的只做多版本；不添加源码之外的止损。",
        "成本测试包含零成本、每边0.25%低成本场所诊断，以及每边0.5%、1%和1.5%的敏感性。",
        "信号在K线收盘生成，下一根开盘成交；SELL只平多，不做空。",
        "",
        "| 数据源 | 标的 | 仓位 | 成本/边 | 净收益 | 账户收益率 | 交易数 | 胜率 | 利润因子 | 最大回撤 | 买入持有 | 超额收益 |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for source_name, source in report["sources"].items():
        for symbol, item in source["symbols"].items():
            for allocation, costs in item["allocations"].items():
                for spread, metrics in costs.items():
                    factor = metrics["profit_factor"]
                    lines.append(
                        f"| {source_name} | {symbol} | {float(allocation):.1%} | {float(spread):.2%} | "
                        f"${metrics['net_profit']:,.2f} | {metrics['account_return']:.2%} | "
                        f"{metrics['trade_count']} | {metrics['win_rate']:.1%} | "
                        f"{'—' if factor is None else f'{factor:.2f}'} | {metrics['max_drawdown']:.2%} | "
                        f"${metrics['buy_hold_profit']:,.2f} | ${metrics['excess_vs_buy_hold']:,.2f} |"
                    )
    lines.extend([
        "",
        "本报告只评估策略，不会改变或部署现有Sandbox自动交易任务。",
        "",
    ])
    return "\n".join(lines)


def save_report(report: dict[str, Any]) -> tuple[Path, Path]:
    REPORT_JSON.parent.mkdir(parents=True, exist_ok=True)
    REPORT_JSON.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    REPORT_MARKDOWN.write_text(markdown_report(report), encoding="utf-8")
    return REPORT_JSON, REPORT_MARKDOWN

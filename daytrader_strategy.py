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
STOP_LOSS = Decimal("0.02")
TAKE_PROFIT = Decimal("0.05")
MAX_HOLD_BARS = 288
RISK_PER_TRADE = Decimal("0.0001")
MAX_ALLOCATION = Decimal("0.005")
BASE_COST = Decimal("0.01")
STRESS_COST = Decimal("0.0125")
COSTS = (Decimal("0"), Decimal("0.0025"), BASE_COST, STRESS_COST)
MIN_OOS_TRADES = 20
MIN_PROFIT_FACTOR = 1.2
MAX_ACCOUNT_DRAWDOWN = Decimal("0.001")
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


def allocation_for_risk(
    *,
    risk_per_trade: Decimal = RISK_PER_TRADE,
    stop_loss: Decimal = STOP_LOSS,
    cost_per_side: Decimal = BASE_COST,
) -> Decimal:
    """Size notional from a fixed account-risk budget, including round-trip cost."""
    if risk_per_trade <= 0 or stop_loss <= 0 or cost_per_side < 0:
        raise ValueError("Risk, stop loss, and cost must be valid positive values")
    loss_fraction = stop_loss + cost_per_side * Decimal(2)
    return min(MAX_ALLOCATION, risk_per_trade / loss_fraction)


ALLOCATION = allocation_for_risk()


def decision_snapshot(bars: list[Bar]) -> dict[str, Any]:
    if len(bars) <= SLOW_EMA:
        raise ValueError("At least 102 closed M5 bars are required")
    closes = [bar.close for bar in bars]
    fast = ema(closes, FAST_EMA)[-1]
    slow = ema(closes, SLOW_EMA)[-1]
    prior_high = max(bar.high for bar in bars[-BREAKOUT_BARS - 1:-1])
    close = bars[-1].close
    break_even = (Decimal("1") + BASE_COST) / (Decimal("1") - BASE_COST) - Decimal("1")
    return {
        "signal": daytrade_signals(bars)[-1],
        "close": _number(close),
        "ema_fast": _number(fast),
        "ema_slow": _number(slow),
        "prior_breakout_high": _number(prior_high),
        "trend_regime": "up" if fast > slow else "down",
        "break_even_move": _number(break_even),
    }


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
        "average_trade_return": round(
            sum(float(trade.pnl / (trade.entry_price * trade.quantity)) for trade in trades) / len(trades),
            8,
        ) if trades else 0.0,
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


def _compact_backtest(bars: list[Bar], cost: Decimal) -> dict[str, Any]:
    metrics = backtest_daytrader(bars, cost_per_side=cost)
    metrics.pop("trades", None)
    return metrics


def _buy_and_hold(bars: list[Bar], cost: Decimal) -> dict[str, float]:
    initial_capital = Decimal("1000000")
    entry_price = bars[0].open * (Decimal("1") + cost)
    exit_price = bars[-1].close * (Decimal("1") - cost)
    quantity = quantity_for_notional(initial_capital * ALLOCATION, entry_price)
    net_profit = quantity * (exit_price - entry_price)
    return {
        "net_profit": _number(net_profit),
        "account_return": _number(net_profit / initial_capital),
        "allocated_return": _number(net_profit / (initial_capital * ALLOCATION)),
    }


def _professional_gate(dataset: dict[str, Any]) -> dict[str, Any]:
    full = dataset["costs"][f"{float(BASE_COST):.4f}"]
    oos = dataset["out_of_sample"]["costs"][f"{float(BASE_COST):.4f}"]
    oos_benchmark = dataset["out_of_sample"]["buy_and_hold"][f"{float(BASE_COST):.4f}"]
    stress = dataset["out_of_sample"]["costs"][f"{float(STRESS_COST):.4f}"]
    positive_folds = sum(
        fold["net_profit"] > 0 for fold in dataset["walk_forward"][f"{float(BASE_COST):.4f}"]
    )
    checks = {
        "data_quality": bool(dataset["quality"]["passed"]),
        "full_period_net_positive": full["net_profit"] > 0,
        "oos_net_positive": oos["net_profit"] > 0,
        "oos_beats_buy_and_hold": oos["net_profit"] > oos_benchmark["net_profit"],
        "oos_minimum_trades": oos["trade_count"] >= MIN_OOS_TRADES,
        "oos_profit_factor": (oos["profit_factor"] or 0) >= MIN_PROFIT_FACTOR,
        "oos_drawdown_within_budget": Decimal(str(oos["max_drawdown"])) <= MAX_ACCOUNT_DRAWDOWN,
        "stress_cost_positive": stress["net_profit"] > 0,
        "walk_forward_stability": positive_folds >= 2,
    }
    return {
        "passed": all(checks.values()),
        "checks": checks,
        "positive_walk_forward_folds": positive_folds,
        "required_positive_walk_forward_folds": 2,
    }


def _dataset(
    bars: list[Bar],
    quality: DataQuality,
    *,
    deployment_gate: bool,
) -> dict[str, Any]:
    split = len(bars) * 2 // 3
    development = bars[:split]
    out_of_sample = bars[split:]
    fold_size = len(bars) // 3
    folds = [
        bars[index * fold_size:(index + 1) * fold_size if index < 2 else len(bars)]
        for index in range(3)
    ]
    result = {
        "quality": asdict(quality),
        "first_bar": bars[0].time.isoformat(),
        "last_bar": bars[-1].time.isoformat(),
        "costs": {
            f"{float(cost):.4f}": _compact_backtest(bars, cost)
            for cost in COSTS
        },
        "buy_and_hold": {
            f"{float(cost):.4f}": _buy_and_hold(bars, cost)
            for cost in COSTS
        },
        "development": {
            "first_bar": development[0].time.isoformat(),
            "last_bar": development[-1].time.isoformat(),
            "costs": {
                f"{float(cost):.4f}": _compact_backtest(development, cost)
                for cost in (BASE_COST, STRESS_COST)
            },
            "buy_and_hold": {
                f"{float(cost):.4f}": _buy_and_hold(development, cost)
                for cost in (BASE_COST, STRESS_COST)
            },
        },
        "out_of_sample": {
            "first_bar": out_of_sample[0].time.isoformat(),
            "last_bar": out_of_sample[-1].time.isoformat(),
            "costs": {
                f"{float(cost):.4f}": _compact_backtest(out_of_sample, cost)
                for cost in (BASE_COST, STRESS_COST)
            },
            "buy_and_hold": {
                f"{float(cost):.4f}": _buy_and_hold(out_of_sample, cost)
                for cost in (BASE_COST, STRESS_COST)
            },
        },
        "walk_forward": {
            f"{float(cost):.4f}": [_compact_backtest(fold, cost) for fold in folds]
            for cost in (BASE_COST, STRESS_COST)
        },
    }
    result["professional_gate"] = _professional_gate(result) if deployment_gate else None
    return result


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
            "risk_per_trade": float(RISK_PER_TRADE),
            "max_allocation": float(MAX_ALLOCATION),
            "risk_sized_allocation": float(ALLOCATION),
            "one_entry_per_symbol_per_utc_day": True,
            "long_only": True,
            "selection_policy": "Parameters fixed before the final 30-day out-of-sample segment",
            "parameter_search": False,
            "tested_strategy_variants": 1,
        },
        "professional_gate": {
            "base_cost_per_side": float(BASE_COST),
            "stress_cost_per_side": float(STRESS_COST),
            "minimum_oos_trades": MIN_OOS_TRADES,
            "minimum_profit_factor": MIN_PROFIT_FACTOR,
            "maximum_account_drawdown": float(MAX_ACCOUNT_DRAWDOWN),
            "minimum_positive_walk_forward_folds": 2,
        },
        "sources": {},
        "candidate_symbols": list(SYMBOLS),
        "deployment_symbols": [],
    }
    coinbase = {}
    for symbol in SYMBOLS:
        bars, quality = coinbase_bars(symbol, start=start, end=end)
        coinbase[symbol] = _dataset(bars, quality, deployment_gate=True)
    report["sources"]["coinbase_m5_90d"] = {
        "timeframe": TIMEFRAME,
        "symbols": coinbase,
    }
    live = webull_bars(api, SYMBOLS, timespan=TIMEFRAME, count=1200, now=now, days=4)
    report["sources"]["webull_m5_recent"] = {
        "timeframe": TIMEFRAME,
        "diagnostic_only": True,
        "symbols": {
            symbol: _dataset(bars, quality, deployment_gate=False)
            for symbol, (bars, quality) in live.items()
        },
    }
    report["deployment_symbols"] = [
        symbol
        for symbol in SYMBOLS
        if coinbase[symbol]["professional_gate"]["passed"]
    ]
    return report


def markdown_report(report: dict[str, Any]) -> str:
    allocation = report["strategy"]["risk_sized_allocation"]
    lines = [
        "# Webull Crypto Sandbox 专业交易审查",
        "",
        f"生成时间：{report['generated_at']}",
        "",
        f"策略：M5 趋势突破，只做多；按账户风险定仓为{allocation:.2%}；每币每天最多入场一次；2%止损、5%止盈、最长持仓24小时。",
        "研究纪律：参数先锁定；前60天为开发样本，最后30天完全样本外；另检查三个连续时间段的稳定性。",
        "成本纪律：Webull基准为每边1%，压力测试为每边1.25%；信号在K线收盘生成，下一根开盘成交。",
        "",
        "## 全样本成本敏感性",
        "",
        "| 数据源 | 标的 | 数据检查 | 成本/边 | 净收益 | 账户收益率 | 交易数 | 胜率 | 利润因子 | 最大回撤 | 平均每笔收益 |",
        "|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for source_name, source in report["sources"].items():
        for symbol, item in source["symbols"].items():
            for cost, metrics in item["costs"].items():
                factor = metrics["profit_factor"]
                lines.append(
                    f"| {source_name} | {symbol} | {'通过' if item['quality']['passed'] else '失败'} | "
                    f"{float(cost):.2%} | ${metrics['net_profit']:,.2f} | {metrics['account_return']:.3%} | "
                    f"{metrics['trade_count']} | {metrics['win_rate']:.1%} | "
                    f"{'—' if factor is None else f'{factor:.2f}'} | {metrics['max_drawdown']:.3%} | "
                    f"{metrics['average_trade_return']:.2%} |"
                )
    lines.extend([
        "",
        "## 样本外放行审查",
        "",
        "| 标的 | 策略净收益 | 买入持有 | 超额收益 | 交易数 | 利润因子 | 1.25%压力成本 | 正收益时段 | 放行 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ])
    coinbase = report["sources"]["coinbase_m5_90d"]["symbols"]
    for symbol, item in coinbase.items():
        base = item["out_of_sample"]["costs"][f"{float(BASE_COST):.4f}"]
        benchmark = item["out_of_sample"]["buy_and_hold"][f"{float(BASE_COST):.4f}"]
        stress = item["out_of_sample"]["costs"][f"{float(STRESS_COST):.4f}"]
        gate = item["professional_gate"]
        factor = base["profit_factor"]
        lines.append(
            f"| {symbol} | ${base['net_profit']:,.2f} | ${benchmark['net_profit']:,.2f} | "
            f"${base['net_profit'] - benchmark['net_profit']:,.2f} | {base['trade_count']} | "
            f"{'—' if factor is None else f'{factor:.2f}'} | ${stress['net_profit']:,.2f} | "
            f"{gate['positive_walk_forward_folds']}/3 | {'通过' if gate['passed'] else '拒绝'} |"
        )
    deployment = "、".join(report["deployment_symbols"]) or "无"
    lines.extend([
        "",
        "## 结论",
        "",
        f"可部署标的：{deployment}。",
        "专业交易不是必须下单：没有通过样本外、成本压力和稳定性审查时，系统必须输出NO_TRADE。",
        "",
    ])
    for symbol, item in coinbase.items():
        failed = [name for name, passed in item["professional_gate"]["checks"].items() if not passed]
        lines.append(f"- {symbol} 未通过：{', '.join(failed) if failed else '无'}")
    lines.append("")
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

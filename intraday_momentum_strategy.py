from __future__ import annotations

import json
import math
import statistics
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from typing import Any, Optional

from equity_orb_strategy import EASTERN, Session, build_sessions, webull_stock_bars
from webull_api import WebullAPI


SYMBOLS = ("SPY", "QQQ")
START = datetime(2025, 3, 2, tzinfo=timezone.utc)
END = datetime(2026, 3, 2, tzinfo=timezone.utc)
INITIAL_CAPITAL = Decimal("1000000")
ALLOCATION = Decimal("0.10")
BASE_COST = Decimal("0.0001")
STRESS_COST = Decimal("0.0003")
MIN_SESSIONS = 230
REPORT_JSON = Path(__file__).parent / "reports" / "intraday-momentum-1y.json"
REPORT_MARKDOWN = Path(__file__).parent / "reports" / "intraday-momentum-1y.md"


@dataclass(frozen=True)
class Observation:
    day: date
    first_return: Decimal
    last_return: Decimal
    entry: Decimal
    exit: Decimal


@dataclass(frozen=True)
class MomentumTrade:
    day: date
    side: str
    entry: Decimal
    exit: Decimal
    quantity: int
    pnl: Decimal
    allocated_return: Decimal
    account_return: Decimal


def _number(value: Decimal | float) -> float:
    return round(float(value), 8)


def _bar(session: Session, hour: int, minute: int):
    return next(
        (
            bar
            for bar in session.bars
            if (bar.time.astimezone(EASTERN).hour, bar.time.astimezone(EASTERN).minute) == (hour, minute)
        ),
        None,
    )


def observations(sessions: list[Session]) -> list[Observation]:
    output = []
    for previous, current in zip(sessions, sessions[1:]):
        first_end = _bar(current, 9, 55)
        entry = _bar(current, 15, 30)
        final = _bar(current, 15, 55)
        if not first_end or not entry or not final or previous.close <= 0 or entry.open <= 0:
            continue
        output.append(Observation(
            day=current.day,
            first_return=first_end.close / previous.close - Decimal("1"),
            last_return=final.close / entry.open - Decimal("1"),
            entry=entry.open,
            exit=final.close,
        ))
    return output


def _correlation_and_slope(items: list[Observation]) -> tuple[float, float]:
    if len(items) < 2:
        return 0.0, 0.0
    x = [float(item.first_return) for item in items]
    y = [float(item.last_return) for item in items]
    x_mean = statistics.mean(x)
    y_mean = statistics.mean(y)
    covariance = sum((a - x_mean) * (b - y_mean) for a, b in zip(x, y))
    x_variance = sum((a - x_mean) ** 2 for a in x)
    y_variance = sum((b - y_mean) ** 2 for b in y)
    slope = covariance / x_variance if x_variance else 0.0
    correlation = covariance / math.sqrt(x_variance * y_variance) if x_variance and y_variance else 0.0
    return round(correlation, 6), round(slope, 6)


def _execute(
    item: Observation,
    equity: Decimal,
    cost: Decimal,
    *,
    always_long: bool = False,
) -> Optional[MomentumTrade]:
    if item.first_return == 0 and not always_long:
        return None
    side = "LONG" if always_long or item.first_return > 0 else "SHORT"
    quantity = int((equity * ALLOCATION / item.entry).to_integral_value(rounding=ROUND_DOWN))
    if quantity <= 0:
        return None
    entry = item.entry * (Decimal("1") + cost if side == "LONG" else Decimal("1") - cost)
    exit_price = item.exit * (Decimal("1") - cost if side == "LONG" else Decimal("1") + cost)
    pnl = Decimal(quantity) * (exit_price - entry if side == "LONG" else entry - exit_price)
    notional = Decimal(quantity) * item.entry
    return MomentumTrade(
        day=item.day,
        side=side,
        entry=entry,
        exit=exit_price,
        quantity=quantity,
        pnl=pnl,
        allocated_return=pnl / notional,
        account_return=pnl / equity,
    )


def _trade_dict(trade: MomentumTrade) -> dict[str, Any]:
    return {
        **asdict(trade),
        "day": trade.day.isoformat(),
        "entry": _number(trade.entry),
        "exit": _number(trade.exit),
        "pnl": _number(trade.pnl),
        "allocated_return": _number(trade.allocated_return),
        "account_return": _number(trade.account_return),
    }


def backtest(
    items: list[Observation],
    *,
    cost: Decimal,
    always_long: bool = False,
    include_trades: bool = True,
) -> dict[str, Any]:
    equity = INITIAL_CAPITAL
    peak = equity
    max_drawdown = Decimal("0")
    trades = []
    for item in items:
        trade = _execute(item, equity, cost, always_long=always_long)
        if not trade:
            continue
        trades.append(trade)
        equity += trade.pnl
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, (peak - equity) / peak)

    wins = [trade for trade in trades if trade.pnl > 0]
    losses = [trade for trade in trades if trade.pnl < 0]
    gross_profit = sum((trade.pnl for trade in wins), Decimal("0"))
    gross_loss = -sum((trade.pnl for trade in losses), Decimal("0"))
    returns = [float(trade.allocated_return) for trade in trades]
    mean_return = statistics.mean(returns) if returns else 0.0
    deviation = statistics.stdev(returns) if len(returns) > 1 else 0.0
    correlation, slope = _correlation_and_slope(items)
    result = {
        "cost_per_side": _number(cost),
        "net_profit": _number(equity - INITIAL_CAPITAL),
        "account_return": _number((equity - INITIAL_CAPITAL) / INITIAL_CAPITAL),
        "trade_count": len(trades),
        "win_rate": round(len(wins) / len(trades), 6) if trades else 0.0,
        "profit_factor": round(float(gross_profit / gross_loss), 6) if gross_loss else None,
        "average_net_bps": round(mean_return * 10000, 6),
        "annualized_sharpe": round(mean_return / deviation * math.sqrt(252), 6) if deviation else 0.0,
        "mean_t_stat": round(mean_return / (deviation / math.sqrt(len(returns))), 6) if deviation else 0.0,
        "max_drawdown": _number(max_drawdown),
        "worst_daily_account_loss": _number(min((trade.account_return for trade in trades), default=Decimal("0"))),
        "first_last_correlation": correlation,
        "regression_slope": slope,
        "long_trades": sum(trade.side == "LONG" for trade in trades),
        "short_trades": sum(trade.side == "SHORT" for trade in trades),
    }
    if include_trades:
        result["trades"] = [_trade_dict(trade) for trade in trades]
    return result


def _buy_and_hold(sessions: list[Session], cost: Decimal) -> dict[str, float]:
    if not sessions:
        return {"net_profit": 0.0, "account_return": 0.0, "allocated_return": 0.0}
    entry = sessions[0].open * (Decimal("1") + cost)
    exit_price = sessions[-1].close * (Decimal("1") - cost)
    quantity = int((INITIAL_CAPITAL * ALLOCATION / entry).to_integral_value(rounding=ROUND_DOWN))
    pnl = Decimal(quantity) * (exit_price - entry)
    return {
        "net_profit": _number(pnl),
        "account_return": _number(pnl / INITIAL_CAPITAL),
        "allocated_return": _number(pnl / (INITIAL_CAPITAL * ALLOCATION)),
    }


def _folds(items: list[Observation]) -> list[list[Observation]]:
    return [
        items[index * len(items) // 4:(index + 1) * len(items) // 4]
        for index in range(4)
    ]


def _gate(report: dict[str, Any]) -> dict[str, Any]:
    spy = report["symbols"]["SPY"]
    qqq = report["symbols"]["QQQ"]
    spy_base = spy["costs"]["0.0001"]
    spy_stress = spy["costs"]["0.0003"]
    qqq_base = qqq["costs"]["0.0001"]
    qqq_stress = qqq["costs"]["0.0003"]
    positive_folds = sum(item["net_profit"] > 0 for item in spy["folds"])
    checks = {
        "data_quality": all(
            item["quality"]["passed"] and item["quality"]["complete_sessions"] >= MIN_SESSIONS
            for item in report["symbols"].values()
        ),
        "spy_net_positive": spy_base["net_profit"] > 0,
        "spy_beats_always_long": spy_base["net_profit"] > spy["always_long"]["net_profit"],
        "spy_profit_factor": (spy_base["profit_factor"] or 0) >= 1.20,
        "spy_expectancy_positive": spy_base["average_net_bps"] > 0,
        "spy_sharpe": spy_base["annualized_sharpe"] >= 1.00,
        "spy_t_stat": spy_base["mean_t_stat"] >= 1.65,
        "spy_stress_positive": spy_stress["net_profit"] > 0,
        "spy_time_stability": positive_folds >= 3,
        "qqq_base_positive": qqq_base["net_profit"] > 0,
        "qqq_stress_positive": qqq_stress["net_profit"] > 0,
        "qqq_profit_factor": (qqq_base["profit_factor"] or 0) >= 1.00,
        "drawdown_budget": Decimal(str(spy_base["max_drawdown"])) <= Decimal("0.005"),
        "worst_day_budget": Decimal(str(spy_base["worst_daily_account_loss"])) >= Decimal("-0.0015"),
        "positive_slopes": spy_base["regression_slope"] > 0 and qqq_base["regression_slope"] > 0,
    }
    passed = all(checks.values())
    return {
        "passed": passed,
        "decision": "FORWARD_SHADOW" if passed else "NO_TRADE",
        "checks": checks,
        "positive_spy_folds": positive_folds,
        "required_positive_spy_folds": 3,
    }


def run_intraday_momentum_backtest(api: WebullAPI) -> dict[str, Any]:
    raw = webull_stock_bars(api, symbols=SYMBOLS, days=365, now=END, timespan="M5")
    report: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "window_start": START.isoformat(),
        "window_end": END.isoformat(),
        "timeframe": "M5",
        "preregistration": "reports/intraday-momentum-preregistration.md",
        "parameter_search": False,
        "symbols": {},
    }
    for symbol in SYMBOLS:
        sessions, quality = build_sessions(raw[symbol])
        items = observations(sessions)
        base = backtest(items, cost=BASE_COST)
        report["symbols"][symbol] = {
            "first_session": sessions[0].day.isoformat(),
            "last_session": sessions[-1].day.isoformat(),
            "quality": asdict(quality),
            "signal_days": len(items),
            "costs": {
                "0.0001": base,
                "0.0003": backtest(items, cost=STRESS_COST),
            },
            "always_long": backtest(items, cost=BASE_COST, always_long=True, include_trades=False),
            "buy_and_hold": _buy_and_hold(sessions, BASE_COST),
            "folds": [backtest(fold, cost=BASE_COST, include_trades=False) for fold in _folds(items)],
        }
    report["professional_gate"] = _gate(report)
    return report


def markdown_report(report: dict[str, Any]) -> str:
    lines = [
        "# Webull股票Sandbox 收盘半小时日内动量专业审查",
        "",
        f"生成时间：{report['generated_at']}",
        f"保留窗口：{report['window_start']}至{report['window_end']}。规则已预先冻结于`{report['preregistration']}`。",
        "",
        "## 结果",
        "",
        "| 标的 | 成本/边 | 净收益 | 账户收益率 | 交易数 | 胜率 | 利润因子 | 平均净基点 | Sharpe | t统计量 | 最大回撤 | 回归斜率 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for symbol, item in report["symbols"].items():
        for cost, metrics in item["costs"].items():
            factor = metrics["profit_factor"]
            lines.append(
                f"| {symbol} | {float(cost):.2%} | ${metrics['net_profit']:,.2f} | {metrics['account_return']:.3%} | "
                f"{metrics['trade_count']} | {metrics['win_rate']:.1%} | {'—' if factor is None else f'{factor:.2f}'} | "
                f"{metrics['average_net_bps']:.2f} | {metrics['annualized_sharpe']:.2f} | {metrics['mean_t_stat']:.2f} | "
                f"{metrics['max_drawdown']:.3%} | {metrics['regression_slope']:.4f} |"
            )
    lines.extend(["", "## 基准与稳定性", ""])
    for symbol, item in report["symbols"].items():
        folds = ", ".join(f"${fold['net_profit']:,.2f}" for fold in item["folds"])
        lines.extend([
            f"- {symbol}：Always Long `${item['always_long']['net_profit']:,.2f}`；Buy & Hold `${item['buy_and_hold']['net_profit']:,.2f}`；四段 `{folds}`。",
            f"- {symbol}数据：{item['quality']['complete_sessions']}个完整交易日，信号日{item['signal_days']}个，质量检查`{'PASS' if item['quality']['passed'] else 'FAIL'}`。",
        ])
    gate = report["professional_gate"]
    lines.extend(["", "## 放行审查", "", f"结论：`{gate['decision']}`", ""])
    for name, passed in gate["checks"].items():
        lines.append(f"- {'通过' if passed else '失败'}：`{name}`")
    lines.extend([
        "",
        "通过也只允许20个交易日的无下单前向影子观察；本报告不会启动或提交Sandbox订单。",
        "",
    ])
    return "\n".join(lines)


def save_report(report: dict[str, Any]) -> tuple[Path, Path]:
    REPORT_JSON.parent.mkdir(parents=True, exist_ok=True)
    REPORT_JSON.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    REPORT_MARKDOWN.write_text(markdown_report(report), encoding="utf-8")
    return REPORT_JSON, REPORT_MARKDOWN

from __future__ import annotations

import json
import math
import statistics
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from typing import Any

from equity_orb_strategy import Session, build_sessions, webull_stock_bars
from webull_api import WebullAPI


SYMBOL = "SPY"
END = datetime(2025, 3, 2, tzinfo=timezone.utc)
LOOKBACK = 14
INITIAL_CAPITAL = Decimal("1000000")
ALLOCATION = Decimal("0.10")
BASE_COST_PER_SHARE = Decimal("0.015")
STRESS_COST_PER_SHARE = Decimal("0.030")
MIN_SESSIONS = 230
DIVIDENDS = {
    date(2024, 3, 15): Decimal("1.595"),
    date(2024, 6, 21): Decimal("1.759"),
    date(2024, 9, 20): Decimal("1.746"),
    date(2024, 12, 20): Decimal("1.966"),
}
REPORT_JSON = Path(__file__).parent / "reports" / "noise-area-m5-1y.json"
REPORT_MARKDOWN = Path(__file__).parent / "reports" / "noise-area-m5-1y.md"


@dataclass(frozen=True)
class Checkpoint:
    signal_index: int
    execution_index: int
    target: int
    upper: Decimal
    lower: Decimal
    vwap: Decimal


@dataclass(frozen=True)
class NoiseTrade:
    day: date
    side: str
    entry_time: datetime
    exit_time: datetime
    entry_price: Decimal
    exit_price: Decimal
    quantity: int
    pnl: Decimal
    net_return: Decimal
    holding_minutes: int


@dataclass(frozen=True)
class DayResult:
    day: date
    pnl: Decimal
    account_return: Decimal
    spy_return: Decimal
    trade_count: int
    order_count: int
    long_pnl: Decimal
    short_pnl: Decimal


def _number(value: Decimal | float) -> float:
    return round(float(value), 8)


def _normal(session: Session) -> bool:
    return len(session.bars) == 78


def checkpoints(
    history: list[Session],
    previous: Session,
    current: Session,
) -> list[Checkpoint]:
    if len(history) != LOOKBACK or not all(_normal(item) for item in history) or not _normal(current):
        raise ValueError("Noise-Area checkpoints require 14 prior and one current normal session")
    open_price = current.open
    adjusted_previous_close = previous.close - DIVIDENDS.get(current.day, Decimal("0"))
    cumulative_value = Decimal("0")
    cumulative_volume = Decimal("0")
    output = []
    for index, bar in enumerate(current.bars):
        typical = (bar.high + bar.low + bar.close) / Decimal("3")
        cumulative_value += typical * bar.volume
        cumulative_volume += bar.volume
        if index < 5 or (index - 5) % 6 or index > 71:
            continue
        sigma = sum(
            (abs(item.bars[index].close / item.open - Decimal("1")) for item in history),
            Decimal("0"),
        ) / Decimal(LOOKBACK)
        upper = max(open_price, adjusted_previous_close) * (Decimal("1") + sigma)
        lower = min(open_price, adjusted_previous_close) * (Decimal("1") - sigma)
        vwap = cumulative_value / cumulative_volume if cumulative_volume else bar.close
        target = 1 if bar.close > upper and bar.close > vwap else -1 if bar.close < lower and bar.close < vwap else 0
        output.append(Checkpoint(index, index + 1, target, upper, lower, vwap))
    return output


def execute_targets(
    session: Session,
    targets: list[tuple[int, int]],
    *,
    quantity: int,
    cost_per_share: Decimal,
) -> list[NoiseTrade]:
    trades = []
    side = 0
    entry_index = 0
    entry_price = Decimal("0")

    def close(exit_index: int, exit_price: Decimal, holding_minutes: int) -> None:
        nonlocal side, entry_index, entry_price
        direction = Decimal(side)
        pnl = direction * Decimal(quantity) * (exit_price - entry_price)
        pnl -= Decimal(quantity) * cost_per_share * Decimal("2")
        notional = Decimal(quantity) * entry_price
        trades.append(NoiseTrade(
            day=session.day,
            side="LONG" if side == 1 else "SHORT",
            entry_time=session.bars[entry_index].time,
            exit_time=session.bars[exit_index].time,
            entry_price=entry_price,
            exit_price=exit_price,
            quantity=quantity,
            pnl=pnl,
            net_return=pnl / notional,
            holding_minutes=holding_minutes,
        ))
        side = 0

    for execution_index, target in targets:
        if target == side:
            continue
        execution_price = session.bars[execution_index].open
        if side:
            close(execution_index, execution_price, (execution_index - entry_index) * 5)
        if target:
            side = target
            entry_index = execution_index
            entry_price = execution_price
    if side:
        close(len(session.bars) - 1, session.close, (len(session.bars) - entry_index) * 5)
    return trades


def trade_day(
    history: list[Session],
    previous: Session,
    current: Session,
    *,
    equity: Decimal,
    cost_per_share: Decimal,
) -> list[NoiseTrade]:
    quantity = int((equity * ALLOCATION / current.open).to_integral_value(rounding=ROUND_DOWN))
    if quantity <= 0:
        return []
    targets = [(item.execution_index, item.target) for item in checkpoints(history, previous, current)]
    return execute_targets(current, targets, quantity=quantity, cost_per_share=cost_per_share)


def _regression(day_results: list[DayResult]) -> dict[str, float]:
    x = [float(item.spy_return) for item in day_results]
    y = [float(item.account_return) for item in day_results]
    if len(x) < 3:
        return {"annualized_alpha": 0.0, "alpha_t_stat": 0.0, "beta": 0.0}
    x_mean = statistics.mean(x)
    y_mean = statistics.mean(y)
    sxx = sum((item - x_mean) ** 2 for item in x)
    beta = sum((a - x_mean) * (b - y_mean) for a, b in zip(x, y)) / sxx if sxx else 0.0
    alpha = y_mean - beta * x_mean
    residuals = [b - alpha - beta * a for a, b in zip(x, y)]
    residual_variance = sum(item * item for item in residuals) / (len(x) - 2)
    alpha_variance = residual_variance * (1 / len(x) + x_mean * x_mean / sxx) if sxx else 0.0
    alpha_standard_error = math.sqrt(alpha_variance) if alpha_variance > 0 else 0.0
    return {
        "annualized_alpha": round(alpha * 252, 8),
        "alpha_t_stat": round(alpha / alpha_standard_error, 6) if alpha_standard_error else 0.0,
        "beta": round(beta, 6),
    }


def _trade_dict(trade: NoiseTrade) -> dict[str, Any]:
    return {
        **asdict(trade),
        "day": trade.day.isoformat(),
        "entry_time": trade.entry_time.isoformat(),
        "exit_time": trade.exit_time.isoformat(),
        "entry_price": _number(trade.entry_price),
        "exit_price": _number(trade.exit_price),
        "pnl": _number(trade.pnl),
        "net_return": _number(trade.net_return),
    }


def _day_dict(item: DayResult) -> dict[str, Any]:
    return {
        **asdict(item),
        "day": item.day.isoformat(),
        "pnl": _number(item.pnl),
        "account_return": _number(item.account_return),
        "spy_return": _number(item.spy_return),
        "long_pnl": _number(item.long_pnl),
        "short_pnl": _number(item.short_pnl),
    }


def backtest(
    sessions: list[Session],
    *,
    cost_per_share: Decimal,
    include_details: bool = False,
) -> dict[str, Any]:
    equity = INITIAL_CAPITAL
    peak = equity
    max_drawdown = Decimal("0")
    normal_history: list[Session] = []
    trades: list[NoiseTrade] = []
    day_results: list[DayResult] = []
    for index, current in enumerate(sessions):
        if index == 0:
            if _normal(current):
                normal_history.append(current)
            continue
        previous = sessions[index - 1]
        equity_before = equity
        current_trades = []
        if _normal(current) and len(normal_history) >= LOOKBACK:
            current_trades = trade_day(
                normal_history[-LOOKBACK:],
                previous,
                current,
                equity=equity,
                cost_per_share=cost_per_share,
            )
        pnl = sum((item.pnl for item in current_trades), Decimal("0"))
        dividend = DIVIDENDS.get(current.day, Decimal("0"))
        spy_return = (current.close + dividend) / previous.close - Decimal("1")
        long_pnl = sum((item.pnl for item in current_trades if item.side == "LONG"), Decimal("0"))
        short_pnl = sum((item.pnl for item in current_trades if item.side == "SHORT"), Decimal("0"))
        day_results.append(DayResult(
            day=current.day,
            pnl=pnl,
            account_return=pnl / equity_before,
            spy_return=spy_return,
            trade_count=len(current_trades),
            order_count=len(current_trades) * 2,
            long_pnl=long_pnl,
            short_pnl=short_pnl,
        ))
        trades.extend(current_trades)
        equity += pnl
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, (peak - equity) / peak)
        if _normal(current):
            normal_history.append(current)

    wins = [item for item in trades if item.pnl > 0]
    losses = [item for item in trades if item.pnl < 0]
    gross_profit = sum((item.pnl for item in wins), Decimal("0"))
    gross_loss = -sum((item.pnl for item in losses), Decimal("0"))
    daily_returns = [float(item.account_return) for item in day_results]
    daily_mean = statistics.mean(daily_returns) if daily_returns else 0.0
    daily_deviation = statistics.stdev(daily_returns) if len(daily_returns) > 1 else 0.0
    trade_returns = [float(item.net_return) for item in trades]
    result = {
        "cost_per_order_per_share": _number(cost_per_share),
        "net_profit": _number(equity - INITIAL_CAPITAL),
        "account_return": _number((equity - INITIAL_CAPITAL) / INITIAL_CAPITAL),
        "trade_days": sum(item.trade_count > 0 for item in day_results),
        "order_count": sum(item.order_count for item in day_results),
        "round_trip_trades": len(trades),
        "win_rate": round(len(wins) / len(trades), 6) if trades else 0.0,
        "profit_factor": round(float(gross_profit / gross_loss), 6) if gross_loss else None,
        "average_trade_bps": round(statistics.mean(trade_returns) * 10000, 6) if trade_returns else 0.0,
        "annualized_sharpe": round(daily_mean / daily_deviation * math.sqrt(252), 6) if daily_deviation else 0.0,
        "daily_mean_t_stat": round(daily_mean / (daily_deviation / math.sqrt(len(daily_returns))), 6) if daily_deviation else 0.0,
        "max_drawdown": _number(max_drawdown),
        "worst_daily_account_loss": _number(min(daily_returns, default=0.0)),
        "long_net_profit": _number(sum((item.pnl for item in trades if item.side == "LONG"), Decimal("0"))),
        "short_net_profit": _number(sum((item.pnl for item in trades if item.side == "SHORT"), Decimal("0"))),
        "average_holding_minutes": round(statistics.mean(item.holding_minutes for item in trades), 2) if trades else 0.0,
        "regression": _regression(day_results),
    }
    if include_details:
        result["trades"] = [_trade_dict(item) for item in trades]
        result["daily"] = [_day_dict(item) for item in day_results]
    return result


def _buy_and_hold(sessions: list[Session]) -> dict[str, float]:
    if not sessions:
        return {"net_profit": 0.0, "account_return": 0.0, "allocated_return": 0.0}
    quantity = int((INITIAL_CAPITAL * ALLOCATION / sessions[0].open).to_integral_value(rounding=ROUND_DOWN))
    dividends = sum((amount for day, amount in DIVIDENDS.items() if sessions[0].day <= day <= sessions[-1].day), Decimal("0"))
    pnl = Decimal(quantity) * (sessions[-1].close - sessions[0].open + dividends - BASE_COST_PER_SHARE * 2)
    return {
        "net_profit": _number(pnl),
        "account_return": _number(pnl / INITIAL_CAPITAL),
        "allocated_return": _number(pnl / (INITIAL_CAPITAL * ALLOCATION)),
    }


def _fold_profits(base: dict[str, Any]) -> list[float]:
    daily = base["daily"]
    return [
        round(sum(item["pnl"] for item in daily[index * len(daily) // 4:(index + 1) * len(daily) // 4]), 8)
        for index in range(4)
    ]


def _gate(report: dict[str, Any]) -> dict[str, Any]:
    zero = report["costs"]["0.000"]
    base = report["costs"]["0.015"]
    stress = report["costs"]["0.030"]
    positive_folds = sum(item > 0 for item in report["fold_net_profits"])
    checks = {
        "data_quality": report["quality"]["passed"] and report["quality"]["complete_sessions"] >= MIN_SESSIONS,
        "zero_cost_positive": zero["net_profit"] > 0,
        "base_cost_positive": base["net_profit"] > 0,
        "stress_cost_positive": stress["net_profit"] > 0,
        "minimum_trades": base["round_trip_trades"] >= 100,
        "profit_factor": (base["profit_factor"] or 0) >= 1.20,
        "positive_trade_expectancy": base["average_trade_bps"] > 0,
        "daily_sharpe": base["annualized_sharpe"] >= 1.00,
        "daily_t_stat": base["daily_mean_t_stat"] >= 1.65,
        "positive_alpha": base["regression"]["annualized_alpha"] > 0,
        "alpha_t_stat": base["regression"]["alpha_t_stat"] >= 1.65,
        "time_stability": positive_folds >= 3,
        "drawdown_budget": Decimal(str(base["max_drawdown"])) <= Decimal("0.01"),
        "worst_day_budget": Decimal(str(base["worst_daily_account_loss"])) >= Decimal("-0.005"),
        "both_sides_positive": base["long_net_profit"] >= 0 and base["short_net_profit"] >= 0,
    }
    passed = all(checks.values())
    return {
        "passed": passed,
        "decision": "M1_VALIDATION" if passed else "NO_TRADE",
        "checks": checks,
        "positive_folds": positive_folds,
        "required_positive_folds": 3,
    }


def run_noise_area_backtest(api: WebullAPI) -> dict[str, Any]:
    raw = webull_stock_bars(api, symbols=(SYMBOL,), days=365, now=END, timespan="M5")
    sessions, quality = build_sessions(raw[SYMBOL])
    zero = backtest(sessions, cost_per_share=Decimal("0"))
    base = backtest(sessions, cost_per_share=BASE_COST_PER_SHARE, include_details=True)
    stress = backtest(sessions, cost_per_share=STRESS_COST_PER_SHARE)
    report: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "symbol": SYMBOL,
        "window_start": "2024-03-02T00:00:00+00:00",
        "window_end": END.isoformat(),
        "timeframe": "M5",
        "preregistration": "reports/noise-area-preregistration.md",
        "parameter_search": False,
        "quality": asdict(quality),
        "normal_sessions": sum(_normal(item) for item in sessions),
        "first_session": sessions[0].day.isoformat(),
        "last_session": sessions[-1].day.isoformat(),
        "costs": {"0.000": zero, "0.015": base, "0.030": stress},
        "buy_and_hold": _buy_and_hold(sessions),
        "fold_net_profits": _fold_profits(base),
        "dividends": {day.isoformat(): _number(amount) for day, amount in DIVIDENDS.items()},
    }
    report["professional_gate"] = _gate(report)
    return report


def markdown_report(report: dict[str, Any]) -> str:
    lines = [
        "# Webull股票Sandbox Noise-Area/VWAP M5专业审查",
        "",
        f"生成时间：{report['generated_at']}",
        f"保留窗口：{report['window_start']}至{report['window_end']}；规则已预先冻结于`{report['preregistration']}`。",
        f"数据：{report['quality']['complete_sessions']}个完整交易日，其中{report['normal_sessions']}个正常交易日；质量检查`{'PASS' if report['quality']['passed'] else 'FAIL'}`。",
        "",
        "## 结果",
        "",
        "| 每股每单成本 | 净收益 | 账户收益率 | 交易日 | 往返交易 | 胜率 | 利润因子 | 平均交易bps | 日Sharpe | 日t统计量 | 最大回撤 | 年化Alpha | Alpha t | Beta |",
        "|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for cost, metrics in report["costs"].items():
        factor = metrics["profit_factor"]
        regression = metrics["regression"]
        lines.append(
            f"| ${float(cost):.3f} | ${metrics['net_profit']:,.2f} | {metrics['account_return']:.3%} | "
            f"{metrics['trade_days']} | {metrics['round_trip_trades']} | {metrics['win_rate']:.1%} | "
            f"{'—' if factor is None else f'{factor:.2f}'} | {metrics['average_trade_bps']:.2f} | "
            f"{metrics['annualized_sharpe']:.2f} | {metrics['daily_mean_t_stat']:.2f} | "
            f"{metrics['max_drawdown']:.3%} | {regression['annualized_alpha']:.2%} | "
            f"{regression['alpha_t_stat']:.2f} | {regression['beta']:.3f} |"
        )
    base = report["costs"]["0.015"]
    folds = ", ".join(f"${item:,.2f}" for item in report["fold_net_profits"])
    lines.extend([
        "",
        "## 方向、基准与稳定性",
        "",
        f"- 基准成本长端：`${base['long_net_profit']:,.2f}`；短端：`${base['short_net_profit']:,.2f}`；平均持仓：`{base['average_holding_minutes']:.1f}`分钟。",
        f"- 同10%初始资金SPY买入持有（含现金分红）：`${report['buy_and_hold']['net_profit']:,.2f}`。",
        f"- 四个连续时段：`{folds}`。",
        "",
        "## 放行审查",
        "",
        f"结论：`{report['professional_gate']['decision']}`",
        "",
    ])
    for name, passed in report["professional_gate"]["checks"].items():
        lines.append(f"- {'通过' if passed else '失败'}：`{name}`")
    lines.extend([
        "",
        "只有全部通过才允许用另一个未见窗口做M1精确复核；本报告不会创建影子任务或提交Sandbox订单。",
        "",
    ])
    return "\n".join(lines)


def save_report(report: dict[str, Any]) -> tuple[Path, Path]:
    REPORT_JSON.parent.mkdir(parents=True, exist_ok=True)
    REPORT_JSON.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    REPORT_MARKDOWN.write_text(markdown_report(report), encoding="utf-8")
    return REPORT_JSON, REPORT_MARKDOWN

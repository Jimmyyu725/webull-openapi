from __future__ import annotations

import json
import math
import statistics
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from typing import Any

from equity_orb_strategy import Session, build_sessions, webull_stock_bars
from webull_api import WebullAPI


SYMBOLS = ("SPY", "IVV")
END = datetime(2024, 3, 2, tzinfo=timezone.utc)
LOOKBACK = 20
INITIAL_CAPITAL = Decimal("1000000")
LEG_ALLOCATION = Decimal("0.05")
ENTRY_Z = 2.0
STOP_Z = 3.5
BASE_COST_PER_SHARE = Decimal("0.015")
STRESS_COST_PER_SHARE = Decimal("0.030")
MIN_NORMAL_SESSIONS = 230
SPY_DIVIDENDS = {
    date(2023, 3, 17): Decimal("1.506204"),
    date(2023, 6, 16): Decimal("1.638367"),
    date(2023, 9, 15): Decimal("1.583169"),
    date(2023, 12, 15): Decimal("1.906073"),
}
REPORT_JSON = Path(__file__).parent / "reports" / "spy-ivv-relative-value-m5-1y.json"
REPORT_MARKDOWN = Path(__file__).parent / "reports" / "spy-ivv-relative-value-m5-1y.md"


@dataclass(frozen=True)
class PairTrade:
    day: date
    direction: str
    entry_time: datetime
    exit_time: datetime
    spy_entry: Decimal
    spy_exit: Decimal
    ivv_entry: Decimal
    ivv_exit: Decimal
    spy_quantity: int
    ivv_quantity: int
    entry_z: float
    exit_z: float
    gross_notional: Decimal
    pnl: Decimal
    gross_return: Decimal
    holding_minutes: int
    exit_reason: str


@dataclass(frozen=True)
class DayResult:
    day: date
    pnl: Decimal
    account_return: Decimal
    spy_return: Decimal
    trade_count: int


def _number(value: Decimal | float) -> float:
    return round(float(value), 8)


def _normal(session: Session) -> bool:
    return len(session.bars) == 78


def _aligned(spy: Session, ivv: Session) -> bool:
    return len(spy.bars) == len(ivv.bars) and all(
        left.time == right.time for left, right in zip(spy.bars, ivv.bars)
    )


def spread(spy: Session, ivv: Session, index: int) -> float:
    return math.log(float(spy.bars[index].close / spy.bars[0].close)) - math.log(
        float(ivv.bars[index].close / ivv.bars[0].close)
    )


def checkpoint_z_scores(
    history: list[tuple[Session, Session]],
    current: tuple[Session, Session],
) -> list[tuple[int, int, float]]:
    if len(history) != LOOKBACK:
        raise ValueError("Relative-value z-scores require exactly 20 prior sessions")
    if not all(_normal(spy) and _normal(ivv) and _aligned(spy, ivv) for spy, ivv in [*history, current]):
        raise ValueError("Relative-value z-scores require aligned normal sessions")
    output = []
    current_spy, current_ivv = current
    for signal_index in range(5, 66, 6):
        prior = [spread(spy, ivv, signal_index) for spy, ivv in history]
        deviation = statistics.stdev(prior)
        z_score = 0.0 if deviation == 0 else (
            spread(current_spy, current_ivv, signal_index) - statistics.mean(prior)
        ) / deviation
        output.append((signal_index, signal_index + 1, z_score))
    return output


def _close_trade(
    *,
    day: date,
    direction: str,
    spy: Session,
    ivv: Session,
    entry_index: int,
    exit_index: int,
    spy_quantity: int,
    ivv_quantity: int,
    entry_z: float,
    exit_z: float,
    cost_per_share: Decimal,
    exit_reason: str,
) -> PairTrade:
    spy_entry = spy.bars[entry_index].open
    ivv_entry = ivv.bars[entry_index].open
    spy_exit = spy.close if exit_reason == "eod" else spy.bars[exit_index].open
    ivv_exit = ivv.close if exit_reason == "eod" else ivv.bars[exit_index].open
    spy_sign = Decimal("-1") if direction == "SHORT_SPY_LONG_IVV" else Decimal("1")
    ivv_sign = -spy_sign
    pnl = (
        spy_sign * Decimal(spy_quantity) * (spy_exit - spy_entry)
        + ivv_sign * Decimal(ivv_quantity) * (ivv_exit - ivv_entry)
        - Decimal(spy_quantity + ivv_quantity) * cost_per_share * Decimal("2")
    )
    gross_notional = Decimal(spy_quantity) * spy_entry + Decimal(ivv_quantity) * ivv_entry
    exit_time = spy.bars[exit_index].time + (timedelta(minutes=5) if exit_reason == "eod" else timedelta())
    return PairTrade(
        day=day,
        direction=direction,
        entry_time=spy.bars[entry_index].time,
        exit_time=exit_time,
        spy_entry=spy_entry,
        spy_exit=spy_exit,
        ivv_entry=ivv_entry,
        ivv_exit=ivv_exit,
        spy_quantity=spy_quantity,
        ivv_quantity=ivv_quantity,
        entry_z=entry_z,
        exit_z=exit_z,
        gross_notional=gross_notional,
        pnl=pnl,
        gross_return=pnl / gross_notional,
        holding_minutes=int((exit_time - spy.bars[entry_index].time).total_seconds() // 60),
        exit_reason=exit_reason,
    )


def trade_day(
    history: list[tuple[Session, Session]],
    current: tuple[Session, Session],
    *,
    equity: Decimal,
    cost_per_share: Decimal,
) -> PairTrade | None:
    spy, ivv = current
    direction = ""
    entry_index = 0
    entry_z = 0.0
    spy_quantity = 0
    ivv_quantity = 0
    checkpoints = checkpoint_z_scores(history, current)
    for _, execution_index, z_score in checkpoints:
        if not direction:
            if abs(z_score) < ENTRY_Z:
                continue
            direction = "SHORT_SPY_LONG_IVV" if z_score > 0 else "LONG_SPY_SHORT_IVV"
            entry_index = execution_index
            entry_z = z_score
            spy_quantity = int(
                (equity * LEG_ALLOCATION / spy.bars[execution_index].open).to_integral_value(rounding=ROUND_DOWN)
            )
            ivv_quantity = int(
                (equity * LEG_ALLOCATION / ivv.bars[execution_index].open).to_integral_value(rounding=ROUND_DOWN)
            )
            if not spy_quantity or not ivv_quantity:
                return None
            continue
        converged = (entry_z > 0 and z_score <= 0) or (entry_z < 0 and z_score >= 0)
        stopped = abs(z_score) >= STOP_Z
        if converged or stopped:
            return _close_trade(
                day=spy.day,
                direction=direction,
                spy=spy,
                ivv=ivv,
                entry_index=entry_index,
                exit_index=execution_index,
                spy_quantity=spy_quantity,
                ivv_quantity=ivv_quantity,
                entry_z=entry_z,
                exit_z=z_score,
                cost_per_share=cost_per_share,
                exit_reason="convergence" if converged else "stop",
            )
    if not direction:
        return None
    final_z = spread(spy, ivv, len(spy.bars) - 1)
    return _close_trade(
        day=spy.day,
        direction=direction,
        spy=spy,
        ivv=ivv,
        entry_index=entry_index,
        exit_index=len(spy.bars) - 1,
        spy_quantity=spy_quantity,
        ivv_quantity=ivv_quantity,
        entry_z=entry_z,
        exit_z=final_z,
        cost_per_share=cost_per_share,
        exit_reason="eod",
    )


def _regression(day_results: list[DayResult]) -> dict[str, float]:
    x = [float(item.spy_return) for item in day_results]
    y = [float(item.account_return) for item in day_results]
    if len(x) < 3:
        return {"annualized_alpha": 0.0, "alpha_t_stat": 0.0, "beta": 0.0}
    x_mean = statistics.mean(x)
    y_mean = statistics.mean(y)
    sxx = sum((item - x_mean) ** 2 for item in x)
    beta = sum((left - x_mean) * (right - y_mean) for left, right in zip(x, y)) / sxx if sxx else 0.0
    alpha = y_mean - beta * x_mean
    residuals = [right - alpha - beta * left for left, right in zip(x, y)]
    residual_variance = sum(item * item for item in residuals) / (len(x) - 2)
    alpha_variance = residual_variance * (1 / len(x) + x_mean * x_mean / sxx) if sxx else 0.0
    standard_error = math.sqrt(alpha_variance) if alpha_variance > 0 else 0.0
    return {
        "annualized_alpha": round(alpha * 252, 8),
        "alpha_t_stat": round(alpha / standard_error, 6) if standard_error else 0.0,
        "beta": round(beta, 6),
    }


def _trade_dict(item: PairTrade) -> dict[str, Any]:
    return {
        **asdict(item),
        "day": item.day.isoformat(),
        "entry_time": item.entry_time.isoformat(),
        "exit_time": item.exit_time.isoformat(),
        "spy_entry": _number(item.spy_entry),
        "spy_exit": _number(item.spy_exit),
        "ivv_entry": _number(item.ivv_entry),
        "ivv_exit": _number(item.ivv_exit),
        "entry_z": round(item.entry_z, 6),
        "exit_z": round(item.exit_z, 6),
        "gross_notional": _number(item.gross_notional),
        "pnl": _number(item.pnl),
        "gross_return": _number(item.gross_return),
    }


def _day_dict(item: DayResult) -> dict[str, Any]:
    return {
        "day": item.day.isoformat(),
        "pnl": _number(item.pnl),
        "account_return": _number(item.account_return),
        "spy_return": _number(item.spy_return),
        "trade_count": item.trade_count,
    }


def backtest(
    spy_sessions: list[Session],
    ivv_sessions: list[Session],
    *,
    cost_per_share: Decimal,
    include_details: bool = False,
) -> dict[str, Any]:
    spy_map = {item.day: item for item in spy_sessions}
    ivv_map = {item.day: item for item in ivv_sessions}
    days = sorted(spy_map.keys() & ivv_map.keys())
    equity = INITIAL_CAPITAL
    peak = equity
    max_drawdown = Decimal("0")
    normal_history: list[tuple[Session, Session]] = []
    trades: list[PairTrade] = []
    day_results: list[DayResult] = []
    previous_spy: Session | None = None
    for day in days:
        spy = spy_map[day]
        ivv = ivv_map[day]
        if previous_spy is None:
            previous_spy = spy
            if _normal(spy) and _normal(ivv) and _aligned(spy, ivv):
                normal_history.append((spy, ivv))
            continue
        equity_before = equity
        trade = None
        if _normal(spy) and _normal(ivv) and _aligned(spy, ivv) and len(normal_history) >= LOOKBACK:
            trade = trade_day(
                normal_history[-LOOKBACK:],
                (spy, ivv),
                equity=equity,
                cost_per_share=cost_per_share,
            )
        pnl = trade.pnl if trade else Decimal("0")
        dividend = SPY_DIVIDENDS.get(day, Decimal("0"))
        day_results.append(DayResult(
            day=day,
            pnl=pnl,
            account_return=pnl / equity_before,
            spy_return=(spy.close + dividend) / previous_spy.close - Decimal("1"),
            trade_count=int(trade is not None),
        ))
        if trade:
            trades.append(trade)
        equity += pnl
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, (peak - equity) / peak)
        previous_spy = spy
        if _normal(spy) and _normal(ivv) and _aligned(spy, ivv):
            normal_history.append((spy, ivv))

    wins = [item for item in trades if item.pnl > 0]
    losses = [item for item in trades if item.pnl < 0]
    gross_profit = sum((item.pnl for item in wins), Decimal("0"))
    gross_loss = -sum((item.pnl for item in losses), Decimal("0"))
    daily_returns = [float(item.account_return) for item in day_results]
    daily_mean = statistics.mean(daily_returns) if daily_returns else 0.0
    daily_deviation = statistics.stdev(daily_returns) if len(daily_returns) > 1 else 0.0
    result = {
        "cost_per_order_per_share": _number(cost_per_share),
        "net_profit": _number(equity - INITIAL_CAPITAL),
        "account_return": _number((equity - INITIAL_CAPITAL) / INITIAL_CAPITAL),
        "pair_trades": len(trades),
        "order_count": len(trades) * 4,
        "win_rate": round(len(wins) / len(trades), 6) if trades else 0.0,
        "profit_factor": round(float(gross_profit / gross_loss), 6) if gross_loss else None,
        "average_trade_bps": round(statistics.mean(float(item.gross_return) for item in trades) * 10000, 6) if trades else 0.0,
        "annualized_sharpe": round(daily_mean / daily_deviation * math.sqrt(252), 6) if daily_deviation else 0.0,
        "daily_mean_t_stat": round(daily_mean / (daily_deviation / math.sqrt(len(daily_returns))), 6) if daily_deviation else 0.0,
        "max_drawdown": _number(max_drawdown),
        "worst_daily_account_loss": _number(min(daily_returns, default=0.0)),
        "short_spy_long_ivv_profit": _number(sum((item.pnl for item in trades if item.direction == "SHORT_SPY_LONG_IVV"), Decimal("0"))),
        "long_spy_short_ivv_profit": _number(sum((item.pnl for item in trades if item.direction == "LONG_SPY_SHORT_IVV"), Decimal("0"))),
        "average_holding_minutes": round(statistics.mean(item.holding_minutes for item in trades), 2) if trades else 0.0,
        "regression": _regression(day_results),
    }
    if include_details:
        result["trades"] = [_trade_dict(item) for item in trades]
        result["daily"] = [_day_dict(item) for item in day_results]
    return result


def _buy_and_hold(spy_sessions: list[Session]) -> dict[str, float]:
    if not spy_sessions:
        return {"net_profit": 0.0, "account_return": 0.0, "allocated_return": 0.0}
    quantity = int(
        (INITIAL_CAPITAL * Decimal("0.10") / spy_sessions[0].open).to_integral_value(rounding=ROUND_DOWN)
    )
    dividends = sum(
        (amount for day, amount in SPY_DIVIDENDS.items() if spy_sessions[0].day <= day <= spy_sessions[-1].day),
        Decimal("0"),
    )
    pnl = Decimal(quantity) * (
        spy_sessions[-1].close - spy_sessions[0].open + dividends - BASE_COST_PER_SHARE * Decimal("2")
    )
    return {
        "net_profit": _number(pnl),
        "account_return": _number(pnl / INITIAL_CAPITAL),
        "allocated_return": _number(pnl / (INITIAL_CAPITAL * Decimal("0.10"))),
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
        "data_quality": (
            all(item["passed"] for item in report["quality"].values())
            and report["normal_common_sessions"] >= MIN_NORMAL_SESSIONS
            and not report["alignment_failures"]
        ),
        "zero_cost_positive": zero["net_profit"] > 0,
        "base_cost_positive": base["net_profit"] > 0,
        "stress_cost_positive": stress["net_profit"] > 0,
        "minimum_pairs": base["pair_trades"] >= 30,
        "profit_factor": (base["profit_factor"] or 0) >= 1.25,
        "positive_trade_expectancy": base["average_trade_bps"] > 0,
        "daily_sharpe": base["annualized_sharpe"] >= 1.00,
        "daily_t_stat": base["daily_mean_t_stat"] >= 1.65,
        "positive_alpha": base["regression"]["annualized_alpha"] > 0,
        "alpha_t_stat": base["regression"]["alpha_t_stat"] >= 1.65,
        "market_neutral_beta": abs(base["regression"]["beta"]) <= 0.10,
        "time_stability": positive_folds >= 3,
        "drawdown_budget": Decimal(str(base["max_drawdown"])) <= Decimal("0.005"),
        "worst_day_budget": Decimal(str(base["worst_daily_account_loss"])) >= Decimal("-0.0025"),
        "both_directions_positive": (
            base["short_spy_long_ivv_profit"] >= 0 and base["long_spy_short_ivv_profit"] >= 0
        ),
    }
    passed = all(checks.values())
    return {
        "passed": passed,
        "decision": "M1_VALIDATION" if passed else "NO_TRADE",
        "checks": checks,
        "positive_folds": positive_folds,
        "required_positive_folds": 3,
    }


def run_relative_value_backtest(api: WebullAPI) -> dict[str, Any]:
    raw = webull_stock_bars(api, symbols=SYMBOLS, days=365, now=END, timespan="M5")
    sessions = {}
    quality = {}
    for symbol in SYMBOLS:
        symbol_sessions, symbol_quality = build_sessions(raw[symbol])
        sessions[symbol] = symbol_sessions
        quality[symbol] = asdict(symbol_quality)
    spy_map = {item.day: item for item in sessions["SPY"]}
    ivv_map = {item.day: item for item in sessions["IVV"]}
    common_days = sorted(spy_map.keys() & ivv_map.keys())
    alignment_failures = [
        day.isoformat() for day in common_days if not _aligned(spy_map[day], ivv_map[day])
    ]
    normal_days = [
        day for day in common_days
        if _normal(spy_map[day]) and _normal(ivv_map[day]) and _aligned(spy_map[day], ivv_map[day])
    ]
    zero = backtest(sessions["SPY"], sessions["IVV"], cost_per_share=Decimal("0"))
    base = backtest(
        sessions["SPY"], sessions["IVV"], cost_per_share=BASE_COST_PER_SHARE, include_details=True
    )
    stress = backtest(sessions["SPY"], sessions["IVV"], cost_per_share=STRESS_COST_PER_SHARE)
    report: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "symbols": list(SYMBOLS),
        "window_start": "2023-03-02T00:00:00+00:00",
        "window_end": END.isoformat(),
        "timeframe": "M5",
        "preregistration": "reports/spy-ivv-relative-value-preregistration.md",
        "parameter_search": False,
        "quality": quality,
        "common_sessions": len(common_days),
        "normal_common_sessions": len(normal_days),
        "alignment_failures": alignment_failures,
        "first_session": common_days[0].isoformat(),
        "last_session": common_days[-1].isoformat(),
        "costs": {"0.000": zero, "0.015": base, "0.030": stress},
        "buy_and_hold_spy": _buy_and_hold(sessions["SPY"]),
        "fold_net_profits": _fold_profits(base),
        "spy_dividends": {day.isoformat(): _number(amount) for day, amount in SPY_DIVIDENDS.items()},
    }
    report["professional_gate"] = _gate(report)
    return report


def markdown_report(report: dict[str, Any]) -> str:
    lines = [
        "# Webull股票Sandbox SPY–IVV日内相对价值M5专业审查",
        "",
        f"生成时间：{report['generated_at']}",
        f"保留窗口：{report['window_start']}至{report['window_end']}；规则预先冻结于`{report['preregistration']}`。",
        f"共同完整交易日：{report['common_sessions']}；正常且完全对齐：{report['normal_common_sessions']}；对齐失败：{len(report['alignment_failures'])}。",
        "",
        "## 结果",
        "",
        "| 每股每单成本 | 净收益 | 账户收益率 | 配对数 | 胜率 | 利润因子 | 平均交易bps | 日Sharpe | 日t统计量 | 最大回撤 | 年化Alpha | Alpha t | Beta |",
        "|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for cost, metrics in report["costs"].items():
        factor = metrics["profit_factor"]
        regression = metrics["regression"]
        lines.append(
            f"| ${float(cost):.3f} | ${metrics['net_profit']:,.2f} | {metrics['account_return']:.3%} | "
            f"{metrics['pair_trades']} | {metrics['win_rate']:.1%} | "
            f"{'—' if factor is None else f'{factor:.2f}'} | {metrics['average_trade_bps']:.2f} | "
            f"{metrics['annualized_sharpe']:.2f} | {metrics['daily_mean_t_stat']:.2f} | "
            f"{metrics['max_drawdown']:.3%} | {regression['annualized_alpha']:.2%} | "
            f"{regression['alpha_t_stat']:.2f} | {regression['beta']:.3f} |"
        )
    base = report["costs"]["0.015"]
    folds = ", ".join(f"${item:,.2f}" for item in report["fold_net_profits"])
    lines.extend([
        "",
        "## 方向、机会成本与稳定性",
        "",
        f"- 做空SPY/做多IVV：`${base['short_spy_long_ivv_profit']:,.2f}`；做多SPY/做空IVV：`${base['long_spy_short_ivv_profit']:,.2f}`。",
        f"- 平均持仓：`{base['average_holding_minutes']:.1f}`分钟；同10%资金SPY买入持有：`${report['buy_and_hold_spy']['net_profit']:,.2f}`。",
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
        "只有全部通过才允许使用另一个未见窗口做M1精确复核；本报告不会创建影子任务或提交Sandbox订单。",
        "",
    ])
    return "\n".join(lines)


def save_report(report: dict[str, Any]) -> tuple[Path, Path]:
    REPORT_JSON.parent.mkdir(parents=True, exist_ok=True)
    REPORT_JSON.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    REPORT_MARKDOWN.write_text(markdown_report(report), encoding="utf-8")
    return REPORT_JSON, REPORT_MARKDOWN

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


SECTORS = ("XLB", "XLC", "XLE", "XLF", "XLI", "XLK", "XLP", "XLRE", "XLU", "XLV", "XLY")
BENCHMARK = "SPY"
DATA_SYMBOLS = (*SECTORS, BENCHMARK)
END = datetime(2023, 3, 2, tzinfo=timezone.utc)
INITIAL_CAPITAL = Decimal("1000000")
LEG_ALLOCATION = Decimal("0.05")
MIN_DISPERSION = Decimal("0.005")
STOP_GROSS_RETURN = Decimal("-0.005")
BASE_COST_PER_SHARE = Decimal("0.015")
STRESS_COST_PER_SHARE = Decimal("0.030")
MIN_NORMAL_SESSIONS = 230
SPY_DIVIDENDS = {
    date(2022, 3, 18): Decimal("1.366009"),
    date(2022, 6, 17): Decimal("1.576871"),
    date(2022, 9, 16): Decimal("1.596398"),
    date(2022, 12, 16): Decimal("1.781400"),
}
REPORT_JSON = Path(__file__).parent / "reports" / "opening-pressure-reversal-m5-1y.json"
REPORT_MARKDOWN = Path(__file__).parent / "reports" / "opening-pressure-reversal-m5-1y.md"


@dataclass(frozen=True)
class OpeningSignal:
    short_symbol: str
    long_symbol: str
    short_pressure: Decimal
    long_pressure: Decimal
    dispersion: Decimal


@dataclass(frozen=True)
class PressureTrade:
    day: date
    short_symbol: str
    long_symbol: str
    opening_dispersion: Decimal
    entry_time: datetime
    exit_time: datetime
    short_entry: Decimal
    short_exit: Decimal
    long_entry: Decimal
    long_exit: Decimal
    short_quantity: int
    long_quantity: int
    short_pnl: Decimal
    long_pnl: Decimal
    transaction_cost: Decimal
    pnl: Decimal
    gross_notional: Decimal
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


def _aligned(sessions: dict[str, Session]) -> bool:
    reference = tuple(bar.time for bar in sessions[BENCHMARK].bars)
    return all(tuple(bar.time for bar in sessions[symbol].bars) == reference for symbol in DATA_SYMBOLS)


def opening_signal(sessions: dict[str, Session]) -> OpeningSignal | None:
    if set(sessions) != set(DATA_SYMBOLS) or not all(_normal(item) for item in sessions.values()):
        raise ValueError("Opening-pressure signal requires every fixed symbol and a normal session")
    if not _aligned(sessions):
        raise ValueError("Opening-pressure signal requires aligned five-minute bars")
    spy_bar = sessions[BENCHMARK].bars[0]
    spy_return = spy_bar.close / spy_bar.open - Decimal("1")
    pressure = {
        symbol: sessions[symbol].bars[0].close / sessions[symbol].bars[0].open - Decimal("1") - spy_return
        for symbol in SECTORS
    }
    short_symbol = max(SECTORS, key=lambda symbol: (pressure[symbol], symbol))
    long_symbol = min(SECTORS, key=lambda symbol: (pressure[symbol], symbol))
    dispersion = pressure[short_symbol] - pressure[long_symbol]
    if dispersion < MIN_DISPERSION:
        return None
    return OpeningSignal(
        short_symbol=short_symbol,
        long_symbol=long_symbol,
        short_pressure=pressure[short_symbol],
        long_pressure=pressure[long_symbol],
        dispersion=dispersion,
    )


def _close_trade(
    sessions: dict[str, Session],
    signal: OpeningSignal,
    *,
    entry_index: int,
    exit_index: int,
    short_quantity: int,
    long_quantity: int,
    cost_per_share: Decimal,
    exit_reason: str,
) -> PressureTrade:
    short_session = sessions[signal.short_symbol]
    long_session = sessions[signal.long_symbol]
    short_entry = short_session.bars[entry_index].open
    long_entry = long_session.bars[entry_index].open
    eod = exit_reason == "eod" or exit_reason == "stop_eod"
    short_exit = short_session.close if eod else short_session.bars[exit_index].open
    long_exit = long_session.close if eod else long_session.bars[exit_index].open
    short_cost = Decimal(short_quantity) * cost_per_share * Decimal("2")
    long_cost = Decimal(long_quantity) * cost_per_share * Decimal("2")
    short_pnl = Decimal(short_quantity) * (short_entry - short_exit) - short_cost
    long_pnl = Decimal(long_quantity) * (long_exit - long_entry) - long_cost
    pnl = short_pnl + long_pnl
    gross_notional = Decimal(short_quantity) * short_entry + Decimal(long_quantity) * long_entry
    exit_time = short_session.bars[exit_index].time + (timedelta(minutes=5) if eod else timedelta())
    return PressureTrade(
        day=short_session.day,
        short_symbol=signal.short_symbol,
        long_symbol=signal.long_symbol,
        opening_dispersion=signal.dispersion,
        entry_time=short_session.bars[entry_index].time,
        exit_time=exit_time,
        short_entry=short_entry,
        short_exit=short_exit,
        long_entry=long_entry,
        long_exit=long_exit,
        short_quantity=short_quantity,
        long_quantity=long_quantity,
        short_pnl=short_pnl,
        long_pnl=long_pnl,
        transaction_cost=short_cost + long_cost,
        pnl=pnl,
        gross_notional=gross_notional,
        gross_return=pnl / gross_notional,
        holding_minutes=int((exit_time - short_session.bars[entry_index].time).total_seconds() // 60),
        exit_reason=exit_reason,
    )


def trade_day(
    sessions: dict[str, Session],
    *,
    equity: Decimal,
    cost_per_share: Decimal,
    direction: str = "reversal",
) -> PressureTrade | None:
    signal = opening_signal(sessions)
    if signal is None:
        return None
    if direction == "momentum":
        signal = OpeningSignal(
            short_symbol=signal.long_symbol,
            long_symbol=signal.short_symbol,
            short_pressure=signal.long_pressure,
            long_pressure=signal.short_pressure,
            dispersion=signal.dispersion,
        )
    elif direction != "reversal":
        raise ValueError("Opening-pressure direction must be reversal or momentum")
    entry_index = 1
    short_session = sessions[signal.short_symbol]
    long_session = sessions[signal.long_symbol]
    short_entry = short_session.bars[entry_index].open
    long_entry = long_session.bars[entry_index].open
    short_quantity = int(
        (equity * LEG_ALLOCATION / short_entry).to_integral_value(rounding=ROUND_DOWN)
    )
    long_quantity = int(
        (equity * LEG_ALLOCATION / long_entry).to_integral_value(rounding=ROUND_DOWN)
    )
    if not short_quantity or not long_quantity:
        return None
    gross_notional = Decimal(short_quantity) * short_entry + Decimal(long_quantity) * long_entry
    for index in range(entry_index, len(short_session.bars)):
        raw_pnl = (
            Decimal(short_quantity) * (short_entry - short_session.bars[index].close)
            + Decimal(long_quantity) * (long_session.bars[index].close - long_entry)
        )
        if raw_pnl / gross_notional > STOP_GROSS_RETURN:
            continue
        if index + 1 < len(short_session.bars):
            return _close_trade(
                sessions,
                signal,
                entry_index=entry_index,
                exit_index=index + 1,
                short_quantity=short_quantity,
                long_quantity=long_quantity,
                cost_per_share=cost_per_share,
                exit_reason="stop",
            )
        return _close_trade(
            sessions,
            signal,
            entry_index=entry_index,
            exit_index=index,
            short_quantity=short_quantity,
            long_quantity=long_quantity,
            cost_per_share=cost_per_share,
            exit_reason="stop_eod",
        )
    return _close_trade(
        sessions,
        signal,
        entry_index=entry_index,
        exit_index=len(short_session.bars) - 1,
        short_quantity=short_quantity,
        long_quantity=long_quantity,
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


def _trade_dict(item: PressureTrade) -> dict[str, Any]:
    return {
        **asdict(item),
        "day": item.day.isoformat(),
        "entry_time": item.entry_time.isoformat(),
        "exit_time": item.exit_time.isoformat(),
        "opening_dispersion": _number(item.opening_dispersion),
        "short_entry": _number(item.short_entry),
        "short_exit": _number(item.short_exit),
        "long_entry": _number(item.long_entry),
        "long_exit": _number(item.long_exit),
        "short_pnl": _number(item.short_pnl),
        "long_pnl": _number(item.long_pnl),
        "transaction_cost": _number(item.transaction_cost),
        "pnl": _number(item.pnl),
        "gross_notional": _number(item.gross_notional),
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
    sessions_by_symbol: dict[str, list[Session]],
    *,
    cost_per_share: Decimal,
    include_details: bool = False,
    direction: str = "reversal",
    spy_dividends: dict[date, Decimal] = SPY_DIVIDENDS,
) -> dict[str, Any]:
    maps = {
        symbol: {session.day: session for session in sessions_by_symbol[symbol]}
        for symbol in DATA_SYMBOLS
    }
    days = sorted(set.intersection(*(set(item) for item in maps.values())))
    equity = INITIAL_CAPITAL
    peak = equity
    max_drawdown = Decimal("0")
    trades: list[PressureTrade] = []
    day_results: list[DayResult] = []
    previous_spy: Session | None = None
    for day in days:
        current = {symbol: maps[symbol][day] for symbol in DATA_SYMBOLS}
        equity_before = equity
        trade = None
        if all(_normal(item) for item in current.values()) and _aligned(current):
            trade = trade_day(
                current,
                equity=equity,
                cost_per_share=cost_per_share,
                direction=direction,
            )
        pnl = trade.pnl if trade else Decimal("0")
        spy = current[BENCHMARK]
        dividend = spy_dividends.get(day, Decimal("0"))
        spy_return = (
            (spy.close + dividend) / previous_spy.close - Decimal("1")
            if previous_spy is not None else Decimal("0")
        )
        day_results.append(DayResult(
            day=day,
            pnl=pnl,
            account_return=pnl / equity_before,
            spy_return=spy_return,
            trade_count=int(trade is not None),
        ))
        if trade:
            trades.append(trade)
        equity += pnl
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, (peak - equity) / peak)
        previous_spy = spy

    wins = [item for item in trades if item.pnl > 0]
    losses = [item for item in trades if item.pnl < 0]
    gross_profit = sum((item.pnl for item in wins), Decimal("0"))
    gross_loss = -sum((item.pnl for item in losses), Decimal("0"))
    daily_returns = [float(item.account_return) for item in day_results]
    daily_mean = statistics.mean(daily_returns) if daily_returns else 0.0
    daily_deviation = statistics.stdev(daily_returns) if len(daily_returns) > 1 else 0.0
    sector_contributions = {
        symbol: {
            "net_profit": _number(sum(
                (trade.long_pnl if trade.long_symbol == symbol else trade.short_pnl if trade.short_symbol == symbol else Decimal("0") for trade in trades),
                Decimal("0"),
            )),
            "long_trades": sum(trade.long_symbol == symbol for trade in trades),
            "short_trades": sum(trade.short_symbol == symbol for trade in trades),
        }
        for symbol in SECTORS
    }
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
        "long_leg_profit": _number(sum((item.long_pnl for item in trades), Decimal("0"))),
        "short_leg_profit": _number(sum((item.short_pnl for item in trades), Decimal("0"))),
        "transaction_cost": _number(sum((item.transaction_cost for item in trades), Decimal("0"))),
        "average_holding_minutes": round(statistics.mean(item.holding_minutes for item in trades), 2) if trades else 0.0,
        "stop_count": sum(item.exit_reason.startswith("stop") for item in trades),
        "positive_sector_contributions": sum(item["net_profit"] >= 0 for item in sector_contributions.values()),
        "sector_contributions": sector_contributions,
        "regression": _regression(day_results[1:]),
    }
    if include_details:
        result["trades"] = [_trade_dict(item) for item in trades]
        result["daily"] = [_day_dict(item) for item in day_results]
    return result


def _buy_and_hold(
    spy_sessions: list[Session],
    *,
    spy_dividends: dict[date, Decimal] = SPY_DIVIDENDS,
) -> dict[str, float]:
    if not spy_sessions:
        return {"net_profit": 0.0, "account_return": 0.0, "allocated_return": 0.0}
    quantity = int(
        (INITIAL_CAPITAL * Decimal("0.10") / spy_sessions[0].open).to_integral_value(rounding=ROUND_DOWN)
    )
    dividends = sum(
        (amount for day, amount in spy_dividends.items() if spy_sessions[0].day <= day <= spy_sessions[-1].day),
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
            and not report["day_set_mismatches"]
            and not report["alignment_failures"]
        ),
        "zero_cost_positive": zero["net_profit"] > 0,
        "base_cost_positive": base["net_profit"] > 0,
        "stress_cost_positive": stress["net_profit"] > 0,
        "minimum_pairs": base["pair_trades"] >= 50,
        "profit_factor": (base["profit_factor"] or 0) >= 1.25,
        "positive_trade_expectancy": base["average_trade_bps"] > 0,
        "daily_sharpe": base["annualized_sharpe"] >= 1.00,
        "daily_t_stat": base["daily_mean_t_stat"] >= 1.65,
        "positive_alpha": base["regression"]["annualized_alpha"] > 0,
        "alpha_t_stat": base["regression"]["alpha_t_stat"] >= 1.65,
        "market_neutral_beta": abs(base["regression"]["beta"]) <= 0.10,
        "time_stability": positive_folds >= 3,
        "drawdown_budget": Decimal(str(base["max_drawdown"])) <= Decimal("0.01"),
        "worst_day_budget": Decimal(str(base["worst_daily_account_loss"])) >= Decimal("-0.0025"),
        "both_legs_positive": base["long_leg_profit"] >= 0 and base["short_leg_profit"] >= 0,
        "sector_breadth": base["positive_sector_contributions"] >= 6,
    }
    passed = all(checks.values())
    return {
        "passed": passed,
        "decision": "M1_VALIDATION" if passed else "NO_TRADE",
        "checks": checks,
        "positive_folds": positive_folds,
        "required_positive_folds": 3,
    }


def run_opening_pressure_backtest(api: WebullAPI) -> dict[str, Any]:
    raw = webull_stock_bars(api, symbols=DATA_SYMBOLS, days=365, now=END, timespan="M5")
    sessions = {}
    quality = {}
    for symbol in DATA_SYMBOLS:
        symbol_sessions, symbol_quality = build_sessions(raw[symbol])
        sessions[symbol] = symbol_sessions
        quality[symbol] = asdict(symbol_quality)
    maps = {symbol: {item.day: item for item in sessions[symbol]} for symbol in DATA_SYMBOLS}
    day_sets = [set(item) for item in maps.values()]
    common_days = sorted(set.intersection(*day_sets))
    day_set_mismatches = sorted(set.union(*day_sets) - set(common_days))
    alignment_failures = []
    normal_days = []
    for day in common_days:
        current = {symbol: maps[symbol][day] for symbol in DATA_SYMBOLS}
        if not _aligned(current):
            alignment_failures.append(day.isoformat())
        elif all(_normal(item) for item in current.values()):
            normal_days.append(day)
    zero = backtest(sessions, cost_per_share=Decimal("0"))
    base = backtest(sessions, cost_per_share=BASE_COST_PER_SHARE, include_details=True)
    stress = backtest(sessions, cost_per_share=STRESS_COST_PER_SHARE)
    report: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "symbols": list(DATA_SYMBOLS),
        "window_start": "2022-03-02T00:00:00+00:00",
        "window_end": END.isoformat(),
        "timeframe": "M5",
        "preregistration": "reports/opening-pressure-reversal-preregistration.md",
        "parameter_search": False,
        "quality": quality,
        "common_sessions": len(common_days),
        "normal_common_sessions": len(normal_days),
        "day_set_mismatches": [day.isoformat() for day in day_set_mismatches],
        "alignment_failures": alignment_failures,
        "first_session": common_days[0].isoformat(),
        "last_session": common_days[-1].isoformat(),
        "costs": {"0.000": zero, "0.015": base, "0.030": stress},
        "buy_and_hold_spy": _buy_and_hold(sessions[BENCHMARK]),
        "fold_net_profits": _fold_profits(base),
        "spy_dividends": {day.isoformat(): _number(amount) for day, amount in SPY_DIVIDENDS.items()},
    }
    report["professional_gate"] = _gate(report)
    return report


def markdown_report(report: dict[str, Any]) -> str:
    lines = [
        "# Webull股票Sandbox 开盘价格压力横截面反转M5专业审查",
        "",
        f"生成时间：{report['generated_at']}",
        f"保留窗口：{report['window_start']}至{report['window_end']}；规则预先冻结于`{report['preregistration']}`。",
        f"共同完整交易日：{report['common_sessions']}；正常且完全对齐：{report['normal_common_sessions']}；日期集合差异：{len(report['day_set_mismatches'])}；对齐失败：{len(report['alignment_failures'])}。",
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
    contributions = sorted(
        base["sector_contributions"].items(), key=lambda item: item[1]["net_profit"], reverse=True
    )
    lines.extend([
        "",
        "## 归因、风险与稳定性",
        "",
        f"- 多头腿：`${base['long_leg_profit']:,.2f}`；空头腿：`${base['short_leg_profit']:,.2f}`；累计成本：`${base['transaction_cost']:,.2f}`。",
        f"- 平均持仓：`{base['average_holding_minutes']:.1f}`分钟；止损：`{base['stop_count']}`次；非负行业贡献：`{base['positive_sector_contributions']}/11`。",
        f"- 同10%资金SPY买入持有：`${report['buy_and_hold_spy']['net_profit']:,.2f}`；四个连续时段：`{folds}`。",
        "- 行业贡献：" + "，".join(
            f"`{symbol} ${item['net_profit']:,.2f} (多{item['long_trades']}/空{item['short_trades']})`"
            for symbol, item in contributions
        ) + "。",
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

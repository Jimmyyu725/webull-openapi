from __future__ import annotations

import json
import math
import statistics
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from typing import Any

from crypto_strategy import Bar
from equity_orb_strategy import webull_stock_bars
from webull_api import WebullAPI


SECTORS = ("XLB", "XLE", "XLF", "XLI", "XLK", "XLP", "XLU", "XLV", "XLY")
BENCHMARK = "SPY"
DATA_SYMBOLS = (*SECTORS, BENCHMARK)
INITIAL_CAPITAL = Decimal("1000000")
GROSS_ALLOCATION = Decimal("0.30")
FORMATION_SESSIONS = 126
HOLDING_MONTHS = 6
WINNERS = 3
DEVELOPMENT_DATA_START = datetime(1999, 1, 1, tzinfo=timezone.utc)
DEVELOPMENT_END = datetime(2015, 1, 1, tzinfo=timezone.utc)
DEVELOPMENT_FETCH_END = DEVELOPMENT_END + timedelta(days=1)
DEVELOPMENT_START_DAY = date(2000, 1, 3)
DEVELOPMENT_END_DAY = date(2014, 12, 31)
HOLDOUT_DATA_START = datetime(2014, 1, 1, tzinfo=timezone.utc)
HOLDOUT_END = datetime(2026, 8, 28, tzinfo=timezone.utc)
HOLDOUT_FETCH_END = HOLDOUT_END + timedelta(days=1)
HOLDOUT_START_DAY = date(2015, 1, 2)
HOLDOUT_END_DAY = date(2026, 8, 27)
REPORT_JSON = Path(__file__).parent / "reports" / "classic-sector-momentum-stage-gate.json"
REPORT_MARKDOWN = Path(__file__).parent / "reports" / "classic-sector-momentum-stage-gate.md"


@dataclass(frozen=True)
class CostModel:
    per_share: Decimal
    slippage_rate: Decimal


ZERO_COST = CostModel(Decimal("0"), Decimal("0"))
BASE_COST = CostModel(Decimal("0.015"), Decimal("0.0002"))
STRESS_COST = CostModel(Decimal("0.030"), Decimal("0.0005"))


def _number(value: Decimal | float) -> float:
    return round(float(value), 8)


def _transaction_cost(shares: int, price: Decimal, model: CostModel) -> Decimal:
    return Decimal(shares) * (model.per_share + price * model.slippage_rate)


def _month_start(times: list[datetime], index: int) -> bool:
    return index == 0 or (times[index - 1].year, times[index - 1].month) != (
        times[index].year,
        times[index].month,
    )


def _winners(bars: dict[str, list[Bar]], index: int) -> tuple[str, ...]:
    if index < FORMATION_SESSIONS + 1:
        raise ValueError("Sector momentum requires 126 completed sessions")
    scores = {
        symbol: bars[symbol][index - 1].close / bars[symbol][index - 1 - FORMATION_SESSIONS].close
        - Decimal("1")
        for symbol in SECTORS
    }
    return tuple(sorted(SECTORS, key=lambda symbol: (scores[symbol], symbol), reverse=True)[:WINNERS])


def _aligned(raw: dict[str, list[Bar]], *, minimum_sessions: int) -> tuple[dict[str, list[Bar]], dict[str, Any]]:
    timestamp_sets = {symbol: {item.time for item in raw[symbol]} for symbol in DATA_SYMBOLS}
    common = sorted(set.intersection(*(set(item) for item in timestamp_sets.values())))
    union = set.union(*(set(item) for item in timestamp_sets.values()))
    bars = {
        symbol: [item for item in raw[symbol] if item.time in set(common)]
        for symbol in DATA_SYMBOLS
    }
    invalid_ohlc = []
    jump_flags = []
    duplicates = {}
    ordered = {}
    for symbol in DATA_SYMBOLS:
        source = raw[symbol]
        duplicates[symbol] = len(source) - len({item.time for item in source})
        ordered[symbol] = all(left.time < right.time for left, right in zip(source, source[1:]))
        for item in bars[symbol]:
            if item.low <= 0 or item.open <= 0 or item.close <= 0 or item.high <= 0 or not (
                item.low <= item.open <= item.high and item.low <= item.close <= item.high
            ):
                invalid_ohlc.append(f"{symbol}:{item.time.date().isoformat()}")
        for previous, current in zip(bars[symbol], bars[symbol][1:]):
            if abs(current.close / previous.close - Decimal("1")) > Decimal("0.35"):
                jump_flags.append(f"{symbol}:{current.time.date().isoformat()}")
    quality = {
        "common_sessions": len(common),
        "first_session": common[0].date().isoformat() if common else None,
        "last_session": common[-1].date().isoformat() if common else None,
        "date_set_mismatches": len(union - set(common)),
        "duplicates": duplicates,
        "ordered": ordered,
        "invalid_ohlc": invalid_ohlc,
        "jump_flags": jump_flags,
    }
    quality["passed"] = (
        len(common) >= minimum_sessions
        and not quality["date_set_mismatches"]
        and not any(duplicates.values())
        and all(ordered.values())
        and not invalid_ohlc
        and not jump_flags
    )
    return bars, quality


def _target_weights(mode: str, cohorts: list[tuple[str, ...]]) -> dict[str, Decimal]:
    if mode == "spy":
        return {BENCHMARK: GROSS_ALLOCATION}
    if mode == "equal_weight":
        return {symbol: GROSS_ALLOCATION / Decimal(len(SECTORS)) for symbol in SECTORS}
    if mode != "momentum" or len(cohorts) != HOLDING_MONTHS:
        raise ValueError("Momentum targets require six completed monthly cohorts")
    weight = GROSS_ALLOCATION / Decimal(HOLDING_MONTHS * WINNERS)
    targets = {symbol: Decimal("0") for symbol in SECTORS}
    for cohort in cohorts:
        for symbol in cohort:
            targets[symbol] += weight
    targets[cohorts[-1][-1]] += GROSS_ALLOCATION - sum(targets.values(), Decimal("0"))
    return targets


def _regression(strategy_returns: list[float], benchmark_returns: list[float]) -> dict[str, float]:
    if len(strategy_returns) != len(benchmark_returns) or len(strategy_returns) < 3:
        return {"annualized_alpha": 0.0, "alpha_t_stat": 0.0, "beta": 0.0}
    x_mean = statistics.mean(benchmark_returns)
    y_mean = statistics.mean(strategy_returns)
    sxx = sum((item - x_mean) ** 2 for item in benchmark_returns)
    beta = (
        sum((left - x_mean) * (right - y_mean) for left, right in zip(benchmark_returns, strategy_returns))
        / sxx
        if sxx else 0.0
    )
    alpha = y_mean - beta * x_mean
    residuals = [right - alpha - beta * left for left, right in zip(benchmark_returns, strategy_returns)]
    residual_variance = sum(item * item for item in residuals) / (len(residuals) - 2)
    alpha_variance = residual_variance * (1 / len(residuals) + x_mean * x_mean / sxx) if sxx else 0.0
    standard_error = math.sqrt(alpha_variance) if alpha_variance > 0 else 0.0
    return {
        "annualized_alpha": round(alpha * 252, 8),
        "alpha_t_stat": round(alpha / standard_error, 6) if standard_error else 0.0,
        "beta": round(beta, 6),
    }


def simulate(
    bars: dict[str, list[Bar]],
    *,
    start_day: date,
    end_day: date,
    mode: str,
    cost_model: CostModel,
) -> dict[str, Any]:
    times = [item.time for item in bars[BENCHMARK]]
    cohorts: list[tuple[str, ...]] = []
    quantities = {symbol: 0 for symbol in DATA_SYMBOLS}
    contributions = {symbol: Decimal("0") for symbol in DATA_SYMBOLS}
    cash = INITIAL_CAPITAL
    previous_equity = INITIAL_CAPITAL
    daily: list[dict[str, Any]] = []
    orders: list[dict[str, Any]] = []
    selection_history: list[dict[str, Any]] = []
    order_count = 0
    traded_notional = Decimal("0")
    transaction_cost = Decimal("0")
    peak = INITIAL_CAPITAL
    max_drawdown = Decimal("0")
    evaluation_indices = [
        index for index, item in enumerate(times) if start_day <= item.date() <= end_day
    ]
    if not evaluation_indices:
        raise ValueError("No daily bars in the requested evaluation window")
    first_evaluation = evaluation_indices[0]
    last_evaluation = evaluation_indices[-1]

    for index, timestamp in enumerate(times[:last_evaluation + 1]):
        if mode == "momentum" and _month_start(times, index) and index >= FORMATION_SESSIONS + 1:
            winners = _winners(bars, index)
            cohorts.append(winners)
            cohorts = cohorts[-HOLDING_MONTHS:]
            selection_history.append({"day": timestamp.date().isoformat(), "winners": list(winners)})
        if index < first_evaluation:
            continue

        opens = {symbol: bars[symbol][index].open for symbol in DATA_SYMBOLS}
        closes = {symbol: bars[symbol][index].close for symbol in DATA_SYMBOLS}
        if index > first_evaluation:
            previous_closes = {symbol: bars[symbol][index - 1].close for symbol in DATA_SYMBOLS}
            for symbol, quantity in quantities.items():
                contributions[symbol] += Decimal(quantity) * (opens[symbol] - previous_closes[symbol])

        rebalance = index == first_evaluation or (
            mode != "spy" and _month_start(times, index)
        )
        if rebalance:
            weights = _target_weights(mode, cohorts)
            equity_at_open = cash + sum(
                (Decimal(quantity) * opens[symbol] for symbol, quantity in quantities.items()),
                Decimal("0"),
            )
            target_quantities = {
                symbol: int(
                    (equity_at_open * weights.get(symbol, Decimal("0")) / opens[symbol]).to_integral_value(
                        rounding=ROUND_DOWN
                    )
                )
                for symbol in DATA_SYMBOLS
            }
            deltas = {
                symbol: target_quantities[symbol] - quantities[symbol]
                for symbol in DATA_SYMBOLS
                if target_quantities[symbol] != quantities[symbol]
            }
            for symbol in sorted(deltas, key=lambda item: deltas[item]):
                delta = deltas[symbol]
                shares = abs(delta)
                notional = Decimal(shares) * opens[symbol]
                cost = _transaction_cost(shares, opens[symbol], cost_model)
                cash += notional - cost if delta < 0 else -notional - cost
                quantities[symbol] += delta
                contributions[symbol] -= cost
                transaction_cost += cost
                traded_notional += notional
                order_count += 1
                orders.append({
                    "day": timestamp.date().isoformat(),
                    "symbol": symbol,
                    "quantity_delta": delta,
                    "price": _number(opens[symbol]),
                    "cost": _number(cost),
                })

        for symbol, quantity in quantities.items():
            contributions[symbol] += Decimal(quantity) * (closes[symbol] - opens[symbol])

        if index == last_evaluation:
            for symbol in DATA_SYMBOLS:
                shares = quantities[symbol]
                if not shares:
                    continue
                notional = Decimal(shares) * closes[symbol]
                cost = _transaction_cost(shares, closes[symbol], cost_model)
                cash += notional - cost
                contributions[symbol] -= cost
                transaction_cost += cost
                traded_notional += notional
                quantities[symbol] = 0
                order_count += 1
                orders.append({
                    "day": timestamp.date().isoformat(),
                    "symbol": symbol,
                    "quantity_delta": -shares,
                    "price": _number(closes[symbol]),
                    "cost": _number(cost),
                })

        equity = cash + sum(
            (Decimal(quantity) * closes[symbol] for symbol, quantity in quantities.items()),
            Decimal("0"),
        )
        account_return = equity / previous_equity - Decimal("1")
        daily.append({
            "day": timestamp.date().isoformat(),
            "equity": _number(equity),
            "account_return": _number(account_return),
        })
        previous_equity = equity
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, (peak - equity) / peak)

    daily_returns = [item["account_return"] for item in daily]
    mean = statistics.mean(daily_returns) if daily_returns else 0.0
    deviation = statistics.stdev(daily_returns) if len(daily_returns) > 1 else 0.0
    years = max((end_day - start_day).days / 365.25, 1 / 365.25)
    calendar_returns = {}
    for year in sorted({item["day"][:4] for item in daily}):
        value = 1.0
        for item in daily:
            if item["day"].startswith(year):
                value *= 1 + item["account_return"]
        calendar_returns[year] = round(value - 1, 8)
    return {
        "mode": mode,
        "net_profit": _number(previous_equity - INITIAL_CAPITAL),
        "account_return": _number(previous_equity / INITIAL_CAPITAL - Decimal("1")),
        "cagr": round(float(previous_equity / INITIAL_CAPITAL) ** (1 / years) - 1, 8),
        "annualized_sharpe": round(mean / deviation * math.sqrt(252), 6) if deviation else 0.0,
        "max_drawdown": _number(max_drawdown),
        "worst_daily_return": round(min(daily_returns, default=0.0), 8),
        "transaction_cost": _number(transaction_cost),
        "traded_notional": _number(traded_notional),
        "order_count": order_count,
        "calendar_returns": calendar_returns,
        "positive_sector_contributions": sum(
            contributions[symbol] >= 0 for symbol in SECTORS
        ),
        "sector_contributions": {
            symbol: _number(contributions[symbol]) for symbol in SECTORS
        },
        "selection_history": selection_history,
        "orders": orders,
        "daily": daily,
    }


def _stage(
    raw: dict[str, list[Bar]],
    *,
    start_day: date,
    end_day: date,
    minimum_sessions: int,
) -> dict[str, Any]:
    bars, quality = _aligned(raw, minimum_sessions=minimum_sessions)
    if not quality["passed"]:
        return {"quality": quality, "strategy": None, "benchmarks": None}
    strategy = {
        "zero": simulate(bars, start_day=start_day, end_day=end_day, mode="momentum", cost_model=ZERO_COST),
        "base": simulate(bars, start_day=start_day, end_day=end_day, mode="momentum", cost_model=BASE_COST),
        "stress": simulate(bars, start_day=start_day, end_day=end_day, mode="momentum", cost_model=STRESS_COST),
    }
    benchmarks = {
        "spy": simulate(bars, start_day=start_day, end_day=end_day, mode="spy", cost_model=BASE_COST),
        "equal_weight": simulate(
            bars,
            start_day=start_day,
            end_day=end_day,
            mode="equal_weight",
            cost_model=BASE_COST,
        ),
    }
    base_returns = [item["account_return"] for item in strategy["base"]["daily"]]
    spy_returns = [item["account_return"] for item in benchmarks["spy"]["daily"]]
    strategy["base"]["regression"] = _regression(base_returns, spy_returns)
    strategy["base"]["excess"] = {
        name: {
            "net_profit": round(strategy["base"]["net_profit"] - benchmark["net_profit"], 8),
            "cagr": round(strategy["base"]["cagr"] - benchmark["cagr"], 8),
            "sharpe": round(strategy["base"]["annualized_sharpe"] - benchmark["annualized_sharpe"], 6),
        }
        for name, benchmark in benchmarks.items()
    }
    strategy["base"]["calendar_excess_equal_weight"] = {
        year: round(value - benchmarks["equal_weight"]["calendar_returns"].get(year, 0.0), 8)
        for year, value in strategy["base"]["calendar_returns"].items()
    }
    for name in ("zero", "stress"):
        for detail in ("daily", "orders", "selection_history"):
            strategy[name].pop(detail)
    return {"quality": quality, "strategy": strategy, "benchmarks": benchmarks}


def _gate(stage: dict[str, Any], *, holdout: bool) -> dict[str, Any]:
    if not stage["quality"]["passed"]:
        return {
            "passed": False,
            "checks": {"data_quality": False},
            "positive_years": 0,
            "positive_excess_years": 0,
        }
    zero = stage["strategy"]["zero"]
    base = stage["strategy"]["base"]
    stress = stage["strategy"]["stress"]
    spy = stage["benchmarks"]["spy"]
    equal = stage["benchmarks"]["equal_weight"]
    positive_years = sum(item > 0 for item in base["calendar_returns"].values())
    positive_excess_years = sum(item > 0 for item in base["calendar_excess_equal_weight"].values())
    checks = {
        "data_quality": stage["quality"]["passed"],
        "zero_cost_positive": zero["net_profit"] > 0,
        "base_cost_positive": base["net_profit"] > 0,
        "stress_cost_positive": stress["net_profit"] > 0,
        "beats_spy_profit": base["net_profit"] > spy["net_profit"],
        "beats_equal_weight_profit": base["net_profit"] > equal["net_profit"],
        "beats_spy_cagr": base["cagr"] > spy["cagr"],
        "beats_equal_weight_cagr": base["cagr"] > equal["cagr"],
        "sharpe": base["annualized_sharpe"] >= 0.75,
        "positive_alpha": base["regression"]["annualized_alpha"] > 0,
        "alpha_t_stat": base["regression"]["alpha_t_stat"] >= (2.33 if holdout else 1.65),
        "positive_years": positive_years >= (8 if holdout else 10),
        "positive_excess_years": positive_excess_years >= (7 if holdout else 8),
        "drawdown_vs_benchmarks": base["max_drawdown"] <= min(spy["max_drawdown"], equal["max_drawdown"]),
        "worst_day": base["worst_daily_return"] >= -0.03,
        "sector_breadth": base["positive_sector_contributions"] >= 6,
    }
    if holdout:
        checks["sharpe_vs_benchmarks"] = base["annualized_sharpe"] >= max(
            spy["annualized_sharpe"], equal["annualized_sharpe"]
        )
    return {
        "passed": all(checks.values()),
        "checks": checks,
        "positive_years": positive_years,
        "positive_excess_years": positive_excess_years,
    }


def run_classic_sector_momentum(api: WebullAPI) -> dict[str, Any]:
    development_raw = webull_stock_bars(
        api,
        symbols=DATA_SYMBOLS,
        days=(DEVELOPMENT_FETCH_END - DEVELOPMENT_DATA_START).days,
        now=DEVELOPMENT_FETCH_END,
        timespan="D",
    )
    development = _stage(
        development_raw,
        start_day=DEVELOPMENT_START_DAY,
        end_day=DEVELOPMENT_END_DAY,
        minimum_sessions=3750,
    )
    development["gate"] = _gate(development, holdout=False)
    report: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "preregistration": "reports/classic-sector-momentum-preregistration.md",
        "parameter_search": False,
        "development_window": [DEVELOPMENT_START_DAY.isoformat(), DEVELOPMENT_END_DAY.isoformat()],
        "holdout_window": [HOLDOUT_START_DAY.isoformat(), HOLDOUT_END_DAY.isoformat()],
        "development": development,
        "holdout_requested": False,
        "holdout": None,
        "decision": "REJECT_BEFORE_HOLDOUT",
    }
    if not development["gate"]["passed"]:
        return report

    report["holdout_requested"] = True
    holdout_raw = webull_stock_bars(
        api,
        symbols=DATA_SYMBOLS,
        days=(HOLDOUT_FETCH_END - HOLDOUT_DATA_START).days,
        now=HOLDOUT_FETCH_END,
        timespan="D",
    )
    holdout = _stage(
        holdout_raw,
        start_day=HOLDOUT_START_DAY,
        end_day=HOLDOUT_END_DAY,
        minimum_sessions=2850,
    )
    holdout["gate"] = _gate(holdout, holdout=True)
    report["holdout"] = holdout
    report["decision"] = "SHADOW_ELIGIBLE" if holdout["gate"]["passed"] else "NO_TRADE"
    return report


def _metric_row(name: str, metrics: dict[str, Any]) -> str:
    return (
        f"| {name} | ${metrics['net_profit']:,.2f} | {metrics['account_return']:.2%} | "
        f"{metrics['cagr']:.2%} | {metrics['annualized_sharpe']:.2f} | "
        f"{metrics['max_drawdown']:.2%} | ${metrics['transaction_cost']:,.2f} |"
    )


def _stage_markdown(name: str, stage: dict[str, Any]) -> list[str]:
    quality = stage["quality"]
    lines = [
        f"## {name}",
        "",
        f"数据：{quality['first_session']}至{quality['last_session']}，"
        f"共同日线{quality['common_sessions']}，质量Gate：{'通过' if quality['passed'] else '失败'}。",
    ]
    if not quality["passed"]:
        invalid = quality["invalid_ohlc"]
        invalid_examples = ", ".join(invalid[:10])
        if len(invalid) > 10:
            invalid_examples += " 等"
        lines.extend([
            "",
            f"- 日期集合不一致：{quality['date_set_mismatches']}个时间戳",
            f"- 重复时间戳：{sum(quality['duplicates'].values())}个",
            f"- 倒序标的：{sum(not item for item in quality['ordered'].values())}个",
            f"- 非法OHLC：{len(invalid)}根" + (f"（{invalid_examples}）" if invalid else ""),
            f"- 超过35%的相邻复权收盘跳空：{len(quality['jump_flags'])}处",
            "",
            "预注册规定数据质量失败即停止；未计算策略收益、基准、Alpha或Sharpe，也未请求保留数据。",
            "",
            "Gate：`FAIL`。",
            "- 失败：`data_quality`",
            "",
        ])
        return lines
    lines.extend([
        "",
        "| 组合 | 净收益 | 账户收益 | CAGR | Sharpe | 最大回撤 | 成本 |",
        "|---|---:|---:|---:|---:|---:|---:|",
        _metric_row("动量 零成本", stage["strategy"]["zero"]),
        _metric_row("动量 基准成本", stage["strategy"]["base"]),
        _metric_row("动量 压力成本", stage["strategy"]["stress"]),
        _metric_row("30% SPY", stage["benchmarks"]["spy"]),
        _metric_row("30% 行业等权", stage["benchmarks"]["equal_weight"]),
        "",
        f"Alpha：{stage['strategy']['base']['regression']['annualized_alpha']:.2%}；"
        f"Alpha t：{stage['strategy']['base']['regression']['alpha_t_stat']:.2f}；"
        f"Beta：{stage['strategy']['base']['regression']['beta']:.3f}。",
        f"正收益年份：{stage['gate']['positive_years']}；跑赢行业等权年份：{stage['gate']['positive_excess_years']}；"
        f"非负行业贡献：{stage['strategy']['base']['positive_sector_contributions']}/9。",
        f"Gate：`{'PASS' if stage['gate']['passed'] else 'FAIL'}`。",
    ])
    for check, passed in stage["gate"]["checks"].items():
        lines.append(f"- {'通过' if passed else '失败'}：`{check}`")
    lines.append("")
    return lines


def markdown_report(report: dict[str, Any]) -> str:
    lines = [
        "# Webull股票Sandbox 经典行业动量长期验证",
        "",
        f"生成时间：{report['generated_at']}",
        f"规则预先冻结于`{report['preregistration']}`；未进行参数搜索。",
        "",
        *_stage_markdown("开发阶段", report["development"]),
    ]
    if report["holdout"] is None:
        lines.extend(["## 保留阶段", "", "开发Gate失败，未请求保留数据。", ""])
    else:
        lines.extend(_stage_markdown("保留阶段", report["holdout"]))
    lines.extend([
        "## 结论",
        "",
        f"`{report['decision']}`",
        "",
        "只有保留Gate全部通过才允许进入一个完整月的无下单影子观察；本报告不会提交Sandbox订单。",
        "",
    ])
    return "\n".join(lines)


def save_report(report: dict[str, Any]) -> tuple[Path, Path]:
    REPORT_JSON.parent.mkdir(parents=True, exist_ok=True)
    REPORT_JSON.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    REPORT_MARKDOWN.write_text(markdown_report(report), encoding="utf-8")
    return REPORT_JSON, REPORT_MARKDOWN

from __future__ import annotations

import json
import math
import statistics
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from typing import Any

from classic_sector_momentum import (
    BASE_COST,
    BENCHMARK,
    DATA_SYMBOLS,
    GROSS_ALLOCATION,
    INITIAL_CAPITAL,
    SECTORS,
    STRESS_COST,
    ZERO_COST,
    CostModel,
    _month_start,
    _number,
    _transaction_cost,
)
from crypto_strategy import Bar
from equity_orb_strategy import webull_stock_bars
from webull_api import WebullAPI


SKIP_RECENT_SESSIONS = 126
LOOKBACK_SESSIONS = 252
WINNERS = 3
DEVELOPMENT_FETCH_START = datetime(1999, 1, 1, tzinfo=timezone.utc)
DEVELOPMENT_FETCH_END = datetime(2015, 1, 2, tzinfo=timezone.utc)
DEVELOPMENT_QUALITY_START = date(1999, 2, 1)
DEVELOPMENT_START_DAY = date(2000, 2, 1)
DEVELOPMENT_END_DAY = date(2014, 12, 31)
HOLDOUT_FETCH_START = datetime(2014, 1, 1, tzinfo=timezone.utc)
HOLDOUT_FETCH_END = datetime(2026, 8, 29, tzinfo=timezone.utc)
HOLDOUT_QUALITY_START = date(2014, 1, 1)
HOLDOUT_START_DAY = date(2015, 1, 2)
HOLDOUT_END_DAY = date(2026, 8, 27)
REPORT_JSON = Path(__file__).parent / "reports" / "intermediate-sector-momentum-stage-gate.json"
REPORT_MARKDOWN = Path(__file__).parent / "reports" / "intermediate-sector-momentum-stage-gate.md"


def _winners(bars: dict[str, list[Bar]], index: int) -> tuple[str, ...]:
    if index < LOOKBACK_SESSIONS + 1:
        raise ValueError("Intermediate sector momentum requires 252 completed sessions")
    scores = {
        symbol: (
            bars[symbol][index - 1 - SKIP_RECENT_SESSIONS].close
            / bars[symbol][index - 1 - LOOKBACK_SESSIONS].close
            - Decimal("1")
        )
        for symbol in SECTORS
    }
    return tuple(sorted(SECTORS, key=lambda symbol: (scores[symbol], symbol), reverse=True)[:WINNERS])


def _aligned_close(
    raw: dict[str, list[Bar]],
    *,
    quality_start: date,
    minimum_sessions: int,
) -> tuple[dict[str, list[Bar]], dict[str, Any]]:
    filtered = {
        symbol: [item for item in raw.get(symbol, []) if item.time.date() >= quality_start]
        for symbol in DATA_SYMBOLS
    }
    timestamp_sets = {
        symbol: {item.time for item in filtered[symbol]}
        for symbol in DATA_SYMBOLS
    }
    common_set = set.intersection(*(set(items) for items in timestamp_sets.values()))
    common = sorted(common_set)
    bars = {
        symbol: [item for item in filtered[symbol] if item.time in common_set]
        for symbol in DATA_SYMBOLS
    }
    duplicates = {
        symbol: len(filtered[symbol]) - len(timestamp_sets[symbol])
        for symbol in DATA_SYMBOLS
    }
    ordered = {
        symbol: all(left.time < right.time for left, right in zip(filtered[symbol], filtered[symbol][1:]))
        for symbol in DATA_SYMBOLS
    }
    invalid_close = []
    jump_flags = []
    for symbol in DATA_SYMBOLS:
        for item in bars[symbol]:
            if (
                item.high <= 0
                or item.low <= 0
                or item.close <= 0
                or not item.low <= item.close <= item.high
            ):
                invalid_close.append(f"{symbol}:{item.time.date().isoformat()}")
        for previous, current in zip(bars[symbol], bars[symbol][1:]):
            if abs(current.close / previous.close - Decimal("1")) > Decimal("0.35"):
                jump_flags.append(f"{symbol}:{current.time.date().isoformat()}")
    mismatch_count = sum(len(items - common_set) for items in timestamp_sets.values())
    quality = {
        "quality_start": quality_start.isoformat(),
        "common_sessions": len(common),
        "first_session": common[0].date().isoformat() if common else None,
        "last_session": common[-1].date().isoformat() if common else None,
        "date_set_mismatches": mismatch_count,
        "duplicates": duplicates,
        "ordered": ordered,
        "invalid_close": invalid_close,
        "jump_flags": jump_flags,
        "ignored_field": "open",
    }
    quality["passed"] = (
        len(common) >= minimum_sessions
        and mismatch_count == 0
        and not any(duplicates.values())
        and all(ordered.values())
        and not invalid_close
        and not jump_flags
    )
    return bars, quality


def _target_weights(mode: str, winners: tuple[str, ...] = ()) -> dict[str, Decimal]:
    if mode == "spy":
        return {BENCHMARK: GROSS_ALLOCATION}
    if mode == "equal_weight":
        weight = GROSS_ALLOCATION / Decimal(len(SECTORS))
        targets = {symbol: weight for symbol in SECTORS}
        targets[SECTORS[-1]] += GROSS_ALLOCATION - sum(targets.values(), Decimal("0"))
        return targets
    if mode != "momentum" or len(winners) != WINNERS:
        raise ValueError("Momentum targets require exactly three winners")
    return {symbol: GROSS_ALLOCATION / Decimal(WINNERS) for symbol in winners}


def simulate(
    bars: dict[str, list[Bar]],
    *,
    start_day: date,
    end_day: date,
    mode: str,
    cost_model: CostModel,
) -> dict[str, Any]:
    times = [item.time for item in bars[BENCHMARK]]
    evaluation_indices = [
        index for index, timestamp in enumerate(times) if start_day <= timestamp.date() <= end_day
    ]
    if not evaluation_indices:
        raise ValueError("No daily bars in the requested evaluation window")
    first_evaluation = evaluation_indices[0]
    last_evaluation = evaluation_indices[-1]
    quantities = {symbol: 0 for symbol in DATA_SYMBOLS}
    contributions = {symbol: Decimal("0") for symbol in SECTORS}
    selection_counts = {symbol: 0 for symbol in SECTORS}
    cash = INITIAL_CAPITAL
    previous_equity = INITIAL_CAPITAL
    daily: list[dict[str, Any]] = []
    orders: list[dict[str, Any]] = []
    selections: list[dict[str, Any]] = []
    transaction_cost = Decimal("0")
    traded_notional = Decimal("0")
    peak = INITIAL_CAPITAL
    max_drawdown = Decimal("0")

    for index in range(first_evaluation, last_evaluation + 1):
        timestamp = times[index]
        closes = {symbol: bars[symbol][index].close for symbol in DATA_SYMBOLS}
        if index > first_evaluation:
            previous_closes = {symbol: bars[symbol][index - 1].close for symbol in DATA_SYMBOLS}
            for symbol in SECTORS:
                contributions[symbol] += Decimal(quantities[symbol]) * (
                    closes[symbol] - previous_closes[symbol]
                )

        monthly = index == first_evaluation or _month_start(times, index)
        rebalance = index < last_evaluation and (index == first_evaluation if mode == "spy" else monthly)
        if rebalance:
            winners: tuple[str, ...] = ()
            if mode == "momentum":
                winners = _winners(bars, index)
                for symbol in winners:
                    selection_counts[symbol] += 1
                selections.append({"day": timestamp.date().isoformat(), "winners": list(winners)})
            weights = _target_weights(mode, winners)
            equity_at_close = cash + sum(
                (Decimal(quantity) * closes[symbol] for symbol, quantity in quantities.items()),
                Decimal("0"),
            )
            target_quantities = {
                symbol: int(
                    (equity_at_close * weights.get(symbol, Decimal("0")) / closes[symbol]).to_integral_value(
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
                notional = Decimal(shares) * closes[symbol]
                cost = _transaction_cost(shares, closes[symbol], cost_model)
                cash += notional - cost if delta < 0 else -notional - cost
                quantities[symbol] += delta
                if symbol in contributions:
                    contributions[symbol] -= cost
                transaction_cost += cost
                traded_notional += notional
                orders.append({
                    "day": timestamp.date().isoformat(),
                    "symbol": symbol,
                    "quantity_delta": delta,
                    "close_proxy": _number(closes[symbol]),
                    "cost": _number(cost),
                })

        if index == last_evaluation:
            for symbol in DATA_SYMBOLS:
                shares = quantities[symbol]
                if not shares:
                    continue
                notional = Decimal(shares) * closes[symbol]
                cost = _transaction_cost(shares, closes[symbol], cost_model)
                cash += notional - cost
                quantities[symbol] = 0
                if symbol in contributions:
                    contributions[symbol] -= cost
                transaction_cost += cost
                traded_notional += notional
                orders.append({
                    "day": timestamp.date().isoformat(),
                    "symbol": symbol,
                    "quantity_delta": -shares,
                    "close_proxy": _number(closes[symbol]),
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
    mean = statistics.mean(daily_returns)
    deviation = statistics.stdev(daily_returns) if len(daily_returns) > 1 else 0.0
    years = max((times[last_evaluation].date() - times[first_evaluation].date()).days / 365.25, 1 / 365.25)
    calendar_returns = {}
    for year in sorted({item["day"][:4] for item in daily}):
        value = 1.0
        for item in daily:
            if item["day"].startswith(year):
                value *= 1 + item["account_return"]
        calendar_returns[year] = round(value - 1, 8)
    total_selections = sum(selection_counts.values())
    selection_shares = {
        symbol: round(count / total_selections, 8) if total_selections else 0.0
        for symbol, count in selection_counts.items()
    }
    return {
        "mode": mode,
        "execution_proxy": "monthly first common session close",
        "net_profit": _number(previous_equity - INITIAL_CAPITAL),
        "account_return": _number(previous_equity / INITIAL_CAPITAL - Decimal("1")),
        "cagr": round(float(previous_equity / INITIAL_CAPITAL) ** (1 / years) - 1, 8),
        "annualized_sharpe": round(mean / deviation * math.sqrt(252), 6) if deviation else 0.0,
        "max_drawdown": _number(max_drawdown),
        "worst_daily_return": round(min(daily_returns, default=0.0), 8),
        "transaction_cost": _number(transaction_cost),
        "traded_notional": _number(traded_notional),
        "annualized_turnover": round(float(traded_notional / INITIAL_CAPITAL) / years, 8),
        "order_count": len(orders),
        "calendar_returns": calendar_returns,
        "selected_sector_count": sum(count > 0 for count in selection_counts.values()),
        "selection_counts": selection_counts,
        "selection_shares": selection_shares,
        "max_selection_share": max(selection_shares.values(), default=0.0),
        "sector_contributions": {symbol: _number(value) for symbol, value in contributions.items()},
        "daily": daily,
        "orders": orders,
        "selections": selections,
    }


def _paired_excess(strategy: dict[str, Any], benchmark: dict[str, Any]) -> dict[str, float]:
    differences = [
        left["account_return"] - right["account_return"]
        for left, right in zip(strategy["daily"], benchmark["daily"])
    ]
    mean = statistics.mean(differences) if differences else 0.0
    deviation = statistics.stdev(differences) if len(differences) > 1 else 0.0
    return {
        "annualized_information_ratio": round(mean / deviation * math.sqrt(252), 6) if deviation else 0.0,
        "t_stat": round(mean / (deviation / math.sqrt(len(differences))), 6) if deviation else 0.0,
        "mean_daily_excess": round(mean, 8),
    }


def _stage(
    raw: dict[str, list[Bar]],
    *,
    quality_start: date,
    start_day: date,
    end_day: date,
    minimum_sessions: int,
) -> dict[str, Any]:
    bars, quality = _aligned_close(raw, quality_start=quality_start, minimum_sessions=minimum_sessions)
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
    base = strategy["base"]
    base["excess_equal_weight"] = _paired_excess(base, benchmarks["equal_weight"])
    base["calendar_excess_equal_weight"] = {
        year: round(value - benchmarks["equal_weight"]["calendar_returns"].get(year, 0.0), 8)
        for year, value in base["calendar_returns"].items()
    }
    for result in (*strategy.values(), *benchmarks.values()):
        for detail in ("daily", "orders", "selections"):
            result.pop(detail)
    return {"quality": quality, "strategy": strategy, "benchmarks": benchmarks}


def _gate(stage: dict[str, Any], *, holdout: bool) -> dict[str, Any]:
    if not stage["quality"]["passed"]:
        return {
            "passed": False,
            "checks": {"data_quality": False},
            "positive_years": 0,
            "positive_excess_years": 0,
            "cost_erosion": None,
        }
    zero = stage["strategy"]["zero"]
    base = stage["strategy"]["base"]
    stress = stage["strategy"]["stress"]
    spy = stage["benchmarks"]["spy"]
    equal = stage["benchmarks"]["equal_weight"]
    positive_years = sum(value > 0 for value in base["calendar_returns"].values())
    positive_excess_years = sum(value > 0 for value in base["calendar_excess_equal_weight"].values())
    cost_erosion = (zero["net_profit"] - base["net_profit"]) / zero["net_profit"] if zero["net_profit"] > 0 else float("inf")
    checks = {
        "data_quality": True,
        "zero_cost_positive": zero["net_profit"] > 0,
        "base_cost_positive": base["net_profit"] > 0,
        "stress_cost_positive": stress["net_profit"] > 0,
        "beats_spy_profit": base["net_profit"] > spy["net_profit"],
        "beats_equal_weight_profit": base["net_profit"] > equal["net_profit"],
        "beats_spy_cagr": base["cagr"] > spy["cagr"],
        "beats_equal_weight_cagr": base["cagr"] > equal["cagr"],
        "sharpe": base["annualized_sharpe"] >= 0.75,
        "information_ratio": base["excess_equal_weight"]["annualized_information_ratio"] >= 0.50,
        "excess_t_stat": base["excess_equal_weight"]["t_stat"] >= (2.33 if holdout else 1.65),
        "positive_years": positive_years >= (8 if holdout else 10),
        "positive_excess_years": positive_excess_years >= (7 if holdout else 8),
        "drawdown_vs_benchmarks": base["max_drawdown"] <= min(spy["max_drawdown"], equal["max_drawdown"]),
        "worst_day": base["worst_daily_return"] >= -0.03,
        "sector_breadth": base["selected_sector_count"] >= 6,
        "sector_concentration": base["max_selection_share"] <= 0.35,
        "cost_erosion": cost_erosion <= 0.25,
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
        "cost_erosion": round(cost_erosion, 8) if math.isfinite(cost_erosion) else None,
    }


def run_intermediate_sector_momentum(api: WebullAPI) -> dict[str, Any]:
    development_raw = webull_stock_bars(
        api,
        symbols=DATA_SYMBOLS,
        days=(DEVELOPMENT_FETCH_END - DEVELOPMENT_FETCH_START).days,
        now=DEVELOPMENT_FETCH_END,
        timespan="D",
        require_cache=True,
    )
    development = _stage(
        development_raw,
        quality_start=DEVELOPMENT_QUALITY_START,
        start_day=DEVELOPMENT_START_DAY,
        end_day=DEVELOPMENT_END_DAY,
        minimum_sessions=3750,
    )
    development["gate"] = _gate(development, holdout=False)
    report: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "preregistration": "reports/intermediate-sector-momentum-preregistration.md",
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
        days=(HOLDOUT_FETCH_END - HOLDOUT_FETCH_START).days,
        now=HOLDOUT_FETCH_END,
        timespan="D",
    )
    holdout = _stage(
        holdout_raw,
        quality_start=HOLDOUT_QUALITY_START,
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
        f"## {name}：{'通过' if stage['gate']['passed'] else '未通过'}",
        "",
        f"数据从{quality['first_session']}至{quality['last_session']}，共{quality['common_sessions']}个共同交易日。"
        f"质量Gate：{'通过' if quality['passed'] else '失败'}；`open`字段被预注册排除。",
    ]
    if not quality["passed"]:
        lines.extend([
            "",
            f"日期不一致时间戳：{quality['date_set_mismatches']}；非法收盘：{len(quality['invalid_close'])}；"
            f"异常跳变：{len(quality['jump_flags'])}。",
            "预注册要求数据失败即停止，因此未计算绩效。",
            "",
        ])
        return lines
    base = stage["strategy"]["base"]
    lines.extend([
        "",
        "| 组合 | 净收益 | 账户收益 | CAGR | Sharpe | 最大回撤 | 成本 |",
        "|---|---:|---:|---:|---:|---:|---:|",
        _metric_row("中期动量 零成本", stage["strategy"]["zero"]),
        _metric_row("中期动量 基准成本", base),
        _metric_row("中期动量 压力成本", stage["strategy"]["stress"]),
        _metric_row("30% SPY", stage["benchmarks"]["spy"]),
        _metric_row("30% 行业等权", stage["benchmarks"]["equal_weight"]),
        "",
        f"相对行业等权的信息比率为{base['excess_equal_weight']['annualized_information_ratio']:.2f}，"
        f"配对日超额收益t统计量为{base['excess_equal_weight']['t_stat']:.2f}。"
        f"{stage['gate']['positive_years']}个年份为正，{stage['gate']['positive_excess_years']}个年份跑赢行业等权。",
        f"行业覆盖{base['selected_sector_count']}/9，最高单行业赢家席位占比{base['max_selection_share']:.2%}，"
        f"成本侵蚀{stage['gate']['cost_erosion']:.2%}。",
        "",
        "### Gate明细",
        "",
    ])
    for check, passed in stage["gate"]["checks"].items():
        lines.append(f"- {'通过' if passed else '失败'}：`{check}`")
    lines.append("")
    return lines


def markdown_report(report: dict[str, Any]) -> str:
    lines = [
        "# Webull股票Sandbox 中期行业动量分阶段验证",
        "",
        "## 技术摘要",
        "",
        f"结论：`{report['decision']}`。规则预先冻结，未进行参数搜索；"
        f"保留样本{'已请求' if report['holdout_requested'] else '未请求'}。",
        "",
        "## 核心结果与证据",
        "",
        *_stage_markdown("开发阶段", report["development"]),
    ]
    if report["holdout"] is None:
        lines.extend(["## 保留阶段保持封存", "", "开发Gate失败，程序没有请求或计算2015年后的保留数据。", ""])
    else:
        lines.extend(_stage_markdown("保留阶段", report["holdout"]))
    lines.extend([
        "## 范围、定义与方法",
        "",
        "固定九只经典行业ETF；每月按形成前12至7个月收益选前三名，各占账户10%。"
        "信号只用前一交易日以前的收盘价，调仓日收盘是15:55附近市场单的执行代理。"
        "所有组合使用30%账户敞口、整股、双边成本和相同现金假设。",
        "",
        "## 局限、不确定性与稳健性",
        "",
        "日线收盘无法精确重建15:55至16:00的成交价格，因此回测即使通过也不能直接授权订单。"
        "开发样本曾被其他行业策略查看，只能用于开发；唯一有效的样本外证据是条件触发后才请求的保留窗口。",
        "",
        "## 建议的下一步",
        "",
        "只有两阶段Gate全部通过，才进入一个完整月的无下单影子观察；否则维持`NO_TRADE`。",
        "",
        "## 仍需回答的问题",
        "",
        "影子期需要量化15:55可执行价相对正式收盘的误差，以及月初调仓时真实Sandbox滑点是否落在压力成本内。",
    ])
    return "\n".join(lines) + "\n"


def save_report(report: dict[str, Any]) -> tuple[Path, Path]:
    REPORT_JSON.parent.mkdir(parents=True, exist_ok=True)
    REPORT_JSON.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    REPORT_MARKDOWN.write_text(markdown_report(report), encoding="utf-8")
    return REPORT_JSON, REPORT_MARKDOWN

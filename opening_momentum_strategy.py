from __future__ import annotations

import json
from dataclasses import asdict
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from equity_orb_strategy import Session, build_sessions, webull_stock_bars
from opening_pressure_strategy import (
    BASE_COST_PER_SHARE,
    BENCHMARK,
    DATA_SYMBOLS,
    SECTORS,
    STRESS_COST_PER_SHARE,
    _aligned,
    _buy_and_hold,
    _fold_profits,
    _normal,
    backtest,
)
from webull_api import WebullAPI


DEVELOPMENT_END = datetime(2023, 3, 2, tzinfo=timezone.utc)
HOLDOUT_END = datetime(2022, 3, 2, tzinfo=timezone.utc)
MIN_NORMAL_SESSIONS = 245
MAX_MISMATCHED_DAYS = 2
HOLDOUT_DIVIDENDS = {
    date(2021, 3, 19): Decimal("1.27779"),
    date(2021, 6, 18): Decimal("1.37588"),
    date(2021, 9, 17): Decimal("1.42812"),
    date(2021, 12, 17): Decimal("1.63643"),
}
REPORT_JSON = Path(__file__).parent / "reports" / "opening-pressure-momentum-stage-gate.json"
REPORT_MARKDOWN = Path(__file__).parent / "reports" / "opening-pressure-momentum-stage-gate.md"


def _prepare(raw: dict[str, Any]) -> tuple[dict[str, list[Session]], dict[str, Any]]:
    sessions: dict[str, list[Session]] = {}
    quality = {}
    for symbol in DATA_SYMBOLS:
        symbol_sessions, symbol_quality = build_sessions(raw[symbol])
        sessions[symbol] = symbol_sessions
        quality[symbol] = asdict(symbol_quality)

    maps = {symbol: {item.day: item for item in sessions[symbol]} for symbol in DATA_SYMBOLS}
    day_sets = [set(item) for item in maps.values()]
    common_days = sorted(set.intersection(*day_sets))
    mismatches = sorted(set.union(*day_sets) - set(common_days))
    normal_days = []
    alignment_failures = []
    for day in common_days:
        current = {symbol: maps[symbol][day] for symbol in DATA_SYMBOLS}
        if not _aligned(current):
            alignment_failures.append(day.isoformat())
        elif all(_normal(item) for item in current.values()):
            normal_days.append(day)

    individual_quality = all(
        item["duplicates"] == 0
        and not item["overnight_gap_flags"]
        and len(item["incomplete_middle_sessions"]) <= MAX_MISMATCHED_DAYS
        for item in quality.values()
    )
    metadata = {
        "quality": quality,
        "common_sessions": len(common_days),
        "normal_common_sessions": len(normal_days),
        "day_set_mismatches": [item.isoformat() for item in mismatches],
        "alignment_failures": alignment_failures,
        "first_session": common_days[0].isoformat() if common_days else None,
        "last_session": common_days[-1].isoformat() if common_days else None,
        "quality_passed": (
            individual_quality
            and len(normal_days) >= MIN_NORMAL_SESSIONS
            and len(mismatches) <= MAX_MISMATCHED_DAYS
            and not alignment_failures
        ),
    }
    return sessions, metadata


def _stage(
    raw: dict[str, Any],
    *,
    spy_dividends: dict[date, Decimal],
) -> dict[str, Any]:
    sessions, metadata = _prepare(raw)
    zero = backtest(
        sessions,
        cost_per_share=Decimal("0"),
        direction="momentum",
        spy_dividends=spy_dividends,
    )
    base = backtest(
        sessions,
        cost_per_share=BASE_COST_PER_SHARE,
        include_details=True,
        direction="momentum",
        spy_dividends=spy_dividends,
    )
    stress = backtest(
        sessions,
        cost_per_share=STRESS_COST_PER_SHARE,
        direction="momentum",
        spy_dividends=spy_dividends,
    )
    return {
        **metadata,
        "costs": {"0.000": zero, "0.015": base, "0.030": stress},
        "fold_net_profits": _fold_profits(base),
        "buy_and_hold_spy": _buy_and_hold(
            sessions[BENCHMARK],
            spy_dividends=spy_dividends,
        ),
    }


def development_gate(stage: dict[str, Any]) -> dict[str, Any]:
    zero = stage["costs"]["0.000"]
    base = stage["costs"]["0.015"]
    stress = stage["costs"]["0.030"]
    checks = {
        "data_quality": stage["quality_passed"],
        "zero_cost_positive": zero["net_profit"] > 0,
        "base_cost_positive": base["net_profit"] > 0,
        "stress_cost_positive": stress["net_profit"] > 0,
        "minimum_pairs": base["pair_trades"] >= 50,
        "profit_factor": (base["profit_factor"] or 0) >= 1.10,
        "positive_trade_expectancy": base["average_trade_bps"] > 0,
        "time_stability": sum(item > 0 for item in stage["fold_net_profits"]) >= 3,
        "both_legs_positive": base["long_leg_profit"] > 0 and base["short_leg_profit"] > 0,
        "sector_breadth": base["positive_sector_contributions"] >= 6,
        "drawdown_budget": Decimal(str(base["max_drawdown"])) <= Decimal("0.01"),
        "worst_day_budget": Decimal(str(base["worst_daily_account_loss"])) >= Decimal("-0.0025"),
    }
    return {"passed": all(checks.values()), "checks": checks}


def holdout_gate(stage: dict[str, Any]) -> dict[str, Any]:
    base_gate = development_gate(stage)
    base = stage["costs"]["0.015"]
    checks = {
        **base_gate["checks"],
        "holdout_profit_factor": (base["profit_factor"] or 0) >= 1.25,
        "daily_sharpe": base["annualized_sharpe"] >= 1.00,
        "daily_t_stat": base["daily_mean_t_stat"] >= 2.33,
        "positive_alpha": base["regression"]["annualized_alpha"] > 0,
        "alpha_t_stat": base["regression"]["alpha_t_stat"] >= 2.33,
        "market_neutral_beta": abs(base["regression"]["beta"]) <= 0.10,
    }
    return {"passed": all(checks.values()), "checks": checks}


def run_opening_momentum_backtest(api: WebullAPI) -> dict[str, Any]:
    development_raw = webull_stock_bars(
        api,
        symbols=DATA_SYMBOLS,
        days=365,
        now=DEVELOPMENT_END,
        timespan="M5",
        require_cache=True,
    )
    development = _stage(
        development_raw,
        spy_dividends={
            date(2022, 3, 18): Decimal("1.36601"),
            date(2022, 6, 17): Decimal("1.57687"),
            date(2022, 9, 16): Decimal("1.59640"),
            date(2022, 12, 16): Decimal("1.78140"),
        },
    )
    development["gate"] = development_gate(development)
    report: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "preregistration": "reports/opening-pressure-momentum-preregistration.md",
        "parameter_search": False,
        "development_window": ["2022-03-02T00:00:00+00:00", DEVELOPMENT_END.isoformat()],
        "holdout_window": ["2021-03-02T00:00:00+00:00", HOLDOUT_END.isoformat()],
        "development_sample_contaminated": True,
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
        days=365,
        now=HOLDOUT_END,
        timespan="M5",
    )
    holdout = _stage(holdout_raw, spy_dividends=HOLDOUT_DIVIDENDS)
    holdout["gate"] = holdout_gate(holdout)
    report["holdout"] = holdout
    report["decision"] = "SHADOW_ELIGIBLE" if holdout["gate"]["passed"] else "NO_TRADE"
    return report


def _stage_lines(name: str, stage: dict[str, Any]) -> list[str]:
    lines = [
        f"## {name}",
        "",
        f"共同交易日：{stage['common_sessions']}；正常共同交易日：{stage['normal_common_sessions']}；"
        f"日期差异：{len(stage['day_set_mismatches'])}；数据门槛：{'通过' if stage['quality_passed'] else '失败'}。",
        "",
        "| 每股每单成本 | 净收益 | 配对数 | 胜率 | 利润因子 | 平均bps | Sharpe | t统计量 | 最大回撤 |",
        "|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for cost, metrics in stage["costs"].items():
        factor = metrics["profit_factor"]
        lines.append(
            f"| ${float(cost):.3f} | ${metrics['net_profit']:,.2f} | {metrics['pair_trades']} | "
            f"{metrics['win_rate']:.1%} | {'—' if factor is None else f'{factor:.2f}'} | "
            f"{metrics['average_trade_bps']:.2f} | {metrics['annualized_sharpe']:.2f} | "
            f"{metrics['daily_mean_t_stat']:.2f} | {metrics['max_drawdown']:.3%} |"
        )
    base = stage["costs"]["0.015"]
    lines.extend([
        "",
        f"- 多头腿：`${base['long_leg_profit']:,.2f}`；空头腿：`${base['short_leg_profit']:,.2f}`；"
        f"成本：`${base['transaction_cost']:,.2f}`。",
        f"- 四段净收益：`{', '.join(f'${item:,.2f}' for item in stage['fold_net_profits'])}`。",
        f"- Gate：`{'PASS' if stage['gate']['passed'] else 'FAIL'}`。",
    ])
    for name, passed in stage["gate"]["checks"].items():
        lines.append(f"  - {'通过' if passed else '失败'}：`{name}`")
    lines.append("")
    return lines


def markdown_report(report: dict[str, Any]) -> str:
    lines = [
        "# Webull股票Sandbox 行业开盘价格压力动量分阶段审查",
        "",
        f"生成时间：{report['generated_at']}",
        f"规则预先冻结于`{report['preregistration']}`；未进行参数搜索。",
        "开发样本已被前一次反转研究污染，只能用于成本可行性筛选。",
        "",
        *_stage_lines("阶段A：成本可行性开发样本", report["development"]),
    ]
    if report["holdout"] is None:
        lines.extend([
            "## 保留样本状态",
            "",
            "阶段A失败；程序没有请求或计算2021-03-02至2022-03-02的未见窗口。",
            "",
        ])
    else:
        lines.extend(_stage_lines("阶段B：完全未见保留样本", report["holdout"]))
    lines.extend([
        "## 结论",
        "",
        f"`{report['decision']}`",
        "",
        "只有保留样本全部通过才有资格进入20个交易日无下单影子观察；本报告不会提交Sandbox订单。",
        "",
    ])
    return "\n".join(lines)


def save_report(report: dict[str, Any]) -> tuple[Path, Path]:
    REPORT_JSON.parent.mkdir(parents=True, exist_ok=True)
    REPORT_JSON.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    REPORT_MARKDOWN.write_text(markdown_report(report), encoding="utf-8")
    return REPORT_JSON, REPORT_MARKDOWN

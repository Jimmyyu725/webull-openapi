from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from typing import Any, Iterable, Optional
from zoneinfo import ZoneInfo

from crypto_strategy import Bar, floor_time, parse_time
from webull_api import WebullAPI, normalize_result


SYMBOLS = ("AAPL", "AMD", "AMZN", "GOOGL", "JPM", "META", "MSFT", "NVDA", "TSLA", "XOM")
BENCHMARK = "QQQ"
DATA_SYMBOLS = (*SYMBOLS, BENCHMARK)
INTERVAL_SECONDS = 300
LOOKBACK = 14
INITIAL_CAPITAL = Decimal("1000000")
RISK_PER_TRADE = Decimal("0.0005")
MAX_NOTIONAL = Decimal("0.10")
BASE_COST = Decimal("0.0005")
STRESS_COST = Decimal("0.001")
MIN_SESSIONS = 55
MIN_OOS_TRADES = 10
MIN_PROFIT_FACTOR = 1.2
MAX_OOS_DRAWDOWN = Decimal("0.005")
EASTERN = ZoneInfo("America/New_York")
CACHE_DIR = Path(__file__).parent / ".cache" / "equity-orb"
REPORT_JSON = Path(__file__).parent / "reports" / "equity-orb-backtest-90d.json"
REPORT_MARKDOWN = Path(__file__).parent / "reports" / "equity-orb-backtest-90d.md"
M1_REPORT_JSON = Path(__file__).parent / "reports" / "equity-orb-m1-holdout-90d.json"
M1_REPORT_MARKDOWN = Path(__file__).parent / "reports" / "equity-orb-m1-holdout-90d.md"


@dataclass(frozen=True)
class Session:
    day: date
    bars: tuple[Bar, ...]

    @property
    def open(self) -> Decimal:
        return self.bars[0].open

    @property
    def high(self) -> Decimal:
        return max(bar.high for bar in self.bars)

    @property
    def low(self) -> Decimal:
        return min(bar.low for bar in self.bars)

    @property
    def close(self) -> Decimal:
        return self.bars[-1].close

    @property
    def volume(self) -> Decimal:
        return sum((bar.volume for bar in self.bars), Decimal("0"))


@dataclass(frozen=True)
class StockQuality:
    complete_sessions: int
    dropped_boundary_sessions: int
    incomplete_middle_sessions: tuple[str, ...]
    duplicates: int
    overnight_gap_flags: tuple[str, ...]
    passed: bool


@dataclass(frozen=True)
class OrbTrade:
    day: date
    symbol: str
    side: str
    relative_volume: Decimal
    atr: Decimal
    entry_time: datetime
    exit_time: datetime
    entry_price: Decimal
    exit_price: Decimal
    quantity: int
    pnl: Decimal
    risk_amount: Decimal
    r_multiple: Decimal
    reason: str


def _number(value: Decimal) -> float:
    return round(float(value), 8)


def _serialize_bars(bars: Iterable[Bar]) -> list[dict[str, str]]:
    return [
        {
            "time": bar.time.isoformat(),
            "open": str(bar.open),
            "high": str(bar.high),
            "low": str(bar.low),
            "close": str(bar.close),
            "volume": str(bar.volume),
        }
        for bar in bars
    ]


def _parse_bar(row: dict[str, Any]) -> Bar:
    return Bar(
        time=parse_time(str(row["time"])),
        open=Decimal(str(row["open"])),
        high=Decimal(str(row["high"])),
        low=Decimal(str(row["low"])),
        close=Decimal(str(row["close"])),
        volume=Decimal(str(row.get("volume", "0"))),
    )


def webull_stock_bars(
    api: WebullAPI,
    *,
    symbols: Iterable[str] = DATA_SYMBOLS,
    days: int = 90,
    now: Optional[datetime] = None,
    timespan: str = "M5",
    cache_dir: Path = CACHE_DIR,
) -> dict[str, list[Bar]]:
    interval_seconds = {"M1": 60, "M5": 300}.get(timespan)
    if interval_seconds is None:
        raise ValueError("Stock ORB data supports only M1 or M5")
    symbols = tuple(symbols)
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    end = floor_time(now, interval_seconds)
    start = end - timedelta(days=days)
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_key = hashlib.sha256(
        f"{','.join(symbols)}:{int(start.timestamp())}:{int(end.timestamp())}:{timespan}:v4".encode()
    ).hexdigest()[:16]
    cache_file = cache_dir / f"webull-stock-{cache_key}.json"
    if cache_file.exists():
        payload = json.loads(cache_file.read_text(encoding="utf-8"))
        return {symbol: [_parse_bar(row) for row in payload[symbol]] for symbol in symbols}

    collected: dict[str, list[Bar]] = {symbol: [] for symbol in symbols}
    cursor = end
    while cursor > start:
        response = normalize_result(api.data.market_data.get_batch_history_bar(
            list(symbols),
            "US_STOCK",
            timespan,
            "1200",
            end_time=int(cursor.timestamp() * 1000),
        ))
        if not response.ok or not isinstance(response.data, dict):
            raise RuntimeError(f"Unable to retrieve Webull stock bars: {response.status}")
        items = response.data.get("result", [])
        by_symbol = {str(item.get("symbol")): item.get("result", []) for item in items}
        if any(symbol not in by_symbol or not by_symbol[symbol] for symbol in symbols):
            raise RuntimeError("Webull stock bar response omitted a requested symbol")
        page_oldest: list[datetime] = []
        for symbol in symbols:
            page = [_parse_bar(row) for row in by_symbol[symbol]]
            collected[symbol].extend(page)
            page_oldest.append(min(bar.time for bar in page))
        # Symbols can have different page tails when a minute has no prints.
        # Use the latest tail so no symbol's missing interval is skipped; the
        # resulting overlap is removed below by timestamp.
        oldest = max(page_oldest)
        if oldest <= start:
            break
        # The endpoint treats end_time as exclusive. Reusing the oldest bar's
        # timestamp returns the immediately preceding bar without a gap.
        next_cursor = oldest
        if next_cursor >= cursor:
            raise RuntimeError("Webull stock bar pagination did not advance")
        cursor = next_cursor

    output: dict[str, list[Bar]] = {}
    for symbol, raw in collected.items():
        unique = {bar.time: bar for bar in raw}
        output[symbol] = [
            bar for bar in sorted(unique.values(), key=lambda item: item.time)
            if start <= bar.time < end and bar.time + timedelta(seconds=interval_seconds) <= now
        ]
    temporary = cache_file.with_suffix(".tmp")
    temporary.write_text(
        json.dumps({symbol: _serialize_bars(bars) for symbol, bars in output.items()}, separators=(",", ":")),
        encoding="utf-8",
    )
    temporary.replace(cache_file)
    return output


def build_sessions(
    bars: list[Bar],
    *,
    interval_seconds: int = INTERVAL_SECONDS,
) -> tuple[list[Session], StockQuality]:
    if interval_seconds not in {60, 300}:
        raise ValueError("Stock sessions support only one- or five-minute bars")
    unique: dict[datetime, Bar] = {}
    duplicates = 0
    for bar in bars:
        if bar.time in unique:
            duplicates += 1
        unique[bar.time] = bar
    grouped: dict[date, list[Bar]] = {}
    for bar in sorted(unique.values(), key=lambda item: item.time):
        local = bar.time.astimezone(EASTERN)
        if (local.hour, local.minute) < (9, 30) or (local.hour, local.minute) > (15, 59):
            continue
        grouped.setdefault(local.date(), []).append(bar)

    complete: list[Session] = []
    incomplete: list[date] = []
    ordered_days = sorted(grouped)
    for day in ordered_days:
        day_bars = grouped[day]
        local_times = [bar.time.astimezone(EASTERN) for bar in day_bars]
        normal_count = 23400 // interval_seconds
        early_count = 12600 // interval_seconds
        interval_minutes = interval_seconds // 60
        normal_last = divmod(16 * 60 - interval_minutes, 60)
        early_last = divmod(13 * 60 - interval_minutes, 60)
        contiguous = all(
            int((right.time - left.time).total_seconds()) == interval_seconds
            for left, right in zip(day_bars, day_bars[1:])
        )
        # Webull can include the first post-market bar stamped exactly 13:00
        # after an early close. Its normal-session bars are start-stamped, so
        # the final regular bar is 12:55 for M5 or 12:59 for M1.
        if (
            contiguous
            and len(day_bars) == early_count + 1
            and (local_times[0].hour, local_times[0].minute) == (9, 30)
            and (local_times[-1].hour, local_times[-1].minute) == (13, 0)
        ):
            day_bars = day_bars[:-1]
            local_times = local_times[:-1]
        normal = len(day_bars) == normal_count and (local_times[0].hour, local_times[0].minute) == (9, 30) and (
            local_times[-1].hour, local_times[-1].minute
        ) == normal_last
        early = len(day_bars) == early_count and (local_times[0].hour, local_times[0].minute) == (9, 30) and (
            local_times[-1].hour, local_times[-1].minute
        ) == early_last
        if contiguous and (normal or early):
            complete.append(Session(day, tuple(day_bars)))
        else:
            incomplete.append(day)

    boundary = {ordered_days[0], ordered_days[-1]} if ordered_days else set()
    middle_incomplete = tuple(day.isoformat() for day in incomplete if day not in boundary)
    gap_flags = []
    for previous, current in zip(complete, complete[1:]):
        if previous.close and abs(current.open / previous.close - Decimal("1")) > Decimal("0.30"):
            gap_flags.append(current.day.isoformat())
    quality = StockQuality(
        complete_sessions=len(complete),
        dropped_boundary_sessions=sum(day in boundary for day in incomplete),
        incomplete_middle_sessions=middle_incomplete,
        duplicates=duplicates,
        overnight_gap_flags=tuple(gap_flags),
        passed=(
            len(complete) >= MIN_SESSIONS
            and not middle_incomplete
            and duplicates == 0
            and not gap_flags
        ),
    )
    return complete, quality


def _true_ranges(sessions: list[Session]) -> list[Decimal]:
    output = []
    for index, session in enumerate(sessions):
        previous_close = sessions[index - 1].close if index else session.close
        output.append(max(
            session.high - session.low,
            abs(session.high - previous_close),
            abs(session.low - previous_close),
        ))
    return output


def _opening_bar(session: Session) -> tuple[Bar, int]:
    if len(session.bars) < 2:
        raise ValueError("A complete session needs at least two bars")
    interval = int((session.bars[1].time - session.bars[0].time).total_seconds())
    count = 300 // interval
    opening = session.bars[:count]
    return Bar(
        time=opening[0].time,
        open=opening[0].open,
        high=max(bar.high for bar in opening),
        low=min(bar.low for bar in opening),
        close=opening[-1].close,
        volume=sum((bar.volume for bar in opening), Decimal("0")),
    ), count


def _features(sessions: list[Session]) -> dict[date, dict[str, Decimal]]:
    ranges = _true_ranges(sessions)
    result = {}
    for index in range(LOOKBACK, len(sessions)):
        history = sessions[index - LOOKBACK:index]
        result[sessions[index].day] = {
            "atr": sum(ranges[index - LOOKBACK:index], Decimal("0")) / Decimal(LOOKBACK),
            "average_volume": sum((item.volume for item in history), Decimal("0")) / Decimal(LOOKBACK),
            "average_opening_volume": sum((_opening_bar(item)[0].volume for item in history), Decimal("0")) / Decimal(LOOKBACK),
        }
    return result


def _candidate(
    day: date,
    sessions: dict[str, dict[date, Session]],
    features: dict[str, dict[date, dict[str, Decimal]]],
) -> Optional[tuple[str, Decimal, Decimal]]:
    eligible = []
    for symbol in SYMBOLS:
        session = sessions[symbol].get(day)
        feature = features[symbol].get(day)
        if not session or not feature or feature["average_opening_volume"] <= 0:
            continue
        relative_volume = _opening_bar(session)[0].volume / feature["average_opening_volume"]
        if (
            session.open > Decimal("5")
            and feature["average_volume"] >= Decimal("1000000")
            and feature["atr"] > Decimal("0.50")
            and relative_volume >= Decimal("1")
        ):
            eligible.append((symbol, relative_volume, feature["atr"]))
    return max(eligible, key=lambda item: (item[1], item[0])) if eligible else None


def _quantity(equity: Decimal, entry: Decimal, stop_distance: Decimal) -> int:
    if equity <= 0 or entry <= 0 or stop_distance <= 0:
        return 0
    by_risk = equity * RISK_PER_TRADE / stop_distance
    by_notional = equity * MAX_NOTIONAL / entry
    return int(min(by_risk, by_notional).to_integral_value(rounding=ROUND_DOWN))


def _trade_session(
    symbol: str,
    session: Session,
    relative_volume: Decimal,
    atr: Decimal,
    equity: Decimal,
    cost: Decimal,
) -> Optional[OrbTrade]:
    opening, opening_count = _opening_bar(session)
    if opening.close == opening.open:
        return None
    side = "LONG" if opening.close > opening.open else "SHORT"
    trigger = opening.high if side == "LONG" else opening.low
    stop_distance = atr * Decimal("0.10")
    for bar in session.bars[opening_count:]:
        triggered = bar.high >= trigger if side == "LONG" else bar.low <= trigger
        if not triggered:
            continue
        raw_entry = max(trigger, bar.open) if side == "LONG" else min(trigger, bar.open)
        quantity = _quantity(equity, raw_entry, stop_distance)
        if quantity <= 0:
            return None
        entry = raw_entry * (Decimal("1") + cost if side == "LONG" else Decimal("1") - cost)
        stop = raw_entry - stop_distance if side == "LONG" else raw_entry + stop_distance
        exit_time = session.bars[-1].time
        raw_exit = session.bars[-1].close
        reason = "eod"
        entry_index = session.bars.index(bar)
        for active in session.bars[entry_index:]:
            stopped = active.low <= stop if side == "LONG" else active.high >= stop
            if stopped:
                raw_exit = min(active.open, stop) if side == "LONG" else max(active.open, stop)
                exit_time = active.time
                reason = "stop"
                break
        exit_price = raw_exit * (Decimal("1") - cost if side == "LONG" else Decimal("1") + cost)
        pnl = Decimal(quantity) * (exit_price - entry if side == "LONG" else entry - exit_price)
        risk_amount = Decimal(quantity) * stop_distance
        return OrbTrade(
            day=session.day,
            symbol=symbol,
            side=side,
            relative_volume=relative_volume,
            atr=atr,
            entry_time=bar.time,
            exit_time=exit_time,
            entry_price=entry,
            exit_price=exit_price,
            quantity=quantity,
            pnl=pnl,
            risk_amount=risk_amount,
            r_multiple=pnl / risk_amount,
            reason=reason,
        )
    return None


def _trade_dict(trade: OrbTrade) -> dict[str, Any]:
    return {
        **asdict(trade),
        "day": trade.day.isoformat(),
        "entry_time": trade.entry_time.isoformat(),
        "exit_time": trade.exit_time.isoformat(),
        "relative_volume": _number(trade.relative_volume),
        "atr": _number(trade.atr),
        "entry_price": _number(trade.entry_price),
        "exit_price": _number(trade.exit_price),
        "pnl": _number(trade.pnl),
        "risk_amount": _number(trade.risk_amount),
        "r_multiple": _number(trade.r_multiple),
    }


def _benchmark(sessions: dict[date, Session], days: list[date], cost: Decimal) -> dict[str, float]:
    available = [day for day in days if day in sessions]
    if not available:
        return {"net_profit": 0.0, "account_return": 0.0, "allocated_return": 0.0}
    entry = sessions[available[0]].open * (Decimal("1") + cost)
    exit_price = sessions[available[-1]].close * (Decimal("1") - cost)
    quantity = int((INITIAL_CAPITAL * MAX_NOTIONAL / entry).to_integral_value(rounding=ROUND_DOWN))
    pnl = Decimal(quantity) * (exit_price - entry)
    return {
        "net_profit": _number(pnl),
        "account_return": _number(pnl / INITIAL_CAPITAL),
        "allocated_return": _number(pnl / (INITIAL_CAPITAL * MAX_NOTIONAL)),
    }


def backtest_orb(
    sessions_by_symbol: dict[str, list[Session]],
    *,
    days: list[date],
    cost: Decimal,
) -> dict[str, Any]:
    session_maps = {
        symbol: {session.day: session for session in sessions}
        for symbol, sessions in sessions_by_symbol.items()
    }
    features = {symbol: _features(sessions) for symbol, sessions in sessions_by_symbol.items()}
    equity = INITIAL_CAPITAL
    peak = equity
    max_drawdown = Decimal("0")
    trades = []
    for day in days:
        candidate = _candidate(day, session_maps, features)
        if not candidate:
            continue
        symbol, relative_volume, atr = candidate
        trade = _trade_session(symbol, session_maps[symbol][day], relative_volume, atr, equity, cost)
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
    benchmark = _benchmark(session_maps[BENCHMARK], days, cost)
    net_profit = equity - INITIAL_CAPITAL
    return {
        "cost_per_side": _number(cost),
        "net_profit": _number(net_profit),
        "account_return": _number(net_profit / INITIAL_CAPITAL),
        "trade_count": len(trades),
        "win_rate": round(len(wins) / len(trades), 6) if trades else 0.0,
        "profit_factor": round(float(gross_profit / gross_loss), 6) if gross_loss else None,
        "max_drawdown": _number(max_drawdown),
        "average_r_multiple": round(sum(float(trade.r_multiple) for trade in trades) / len(trades), 6) if trades else 0.0,
        "same_bar_stop_count": sum(
            trade.reason == "stop" and trade.entry_time == trade.exit_time
            for trade in trades
        ),
        "benchmark": benchmark,
        "excess_vs_benchmark": round(_number(net_profit) - benchmark["net_profit"], 8),
        "symbols_traded": sorted({trade.symbol for trade in trades}),
        "trades": [_trade_dict(trade) for trade in trades],
    }


def _compact(metrics: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in metrics.items() if key != "trades"}


def _gate(report: dict[str, Any]) -> dict[str, Any]:
    oos = report["out_of_sample"]["costs"]["0.0005"]
    stress = report["out_of_sample"]["costs"]["0.0010"]
    positive_folds = sum(fold["net_profit"] > 0 for fold in report["walk_forward"]["0.0005"])
    checks = {
        "data_quality": all(item["passed"] for item in report["quality"].values()),
        "oos_net_positive": oos["net_profit"] > 0,
        "oos_beats_qqq": oos["excess_vs_benchmark"] > 0,
        "oos_minimum_trades": oos["trade_count"] >= MIN_OOS_TRADES,
        "oos_profit_factor": (oos["profit_factor"] or 0) >= MIN_PROFIT_FACTOR,
        "oos_average_r_positive": oos["average_r_multiple"] > 0,
        "oos_drawdown_within_budget": Decimal(str(oos["max_drawdown"])) <= MAX_OOS_DRAWDOWN,
        "stress_cost_positive": stress["net_profit"] > 0,
        "walk_forward_stability": positive_folds >= 2,
    }
    return {
        "passed": all(checks.values()),
        "decision": "FORWARD_SHADOW" if all(checks.values()) else "NO_TRADE",
        "checks": checks,
        "positive_walk_forward_folds": positive_folds,
        "required_positive_walk_forward_folds": 2,
    }


def run_orb_backtest(
    api: WebullAPI,
    *,
    days: int = 90,
    now: Optional[datetime] = None,
    timespan: str = "M5",
) -> dict[str, Any]:
    if days != 90:
        raise ValueError("The preregistered ORB study is fixed to 90 days")
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    interval_seconds = {"M1": 60, "M5": 300}.get(timespan)
    if interval_seconds is None:
        raise ValueError("ORB backtest supports only M1 or M5")
    raw = webull_stock_bars(api, days=days, now=now, timespan=timespan)
    sessions_by_symbol: dict[str, list[Session]] = {}
    quality = {}
    for symbol in DATA_SYMBOLS:
        sessions, item_quality = build_sessions(raw[symbol], interval_seconds=interval_seconds)
        sessions_by_symbol[symbol] = sessions
        quality[symbol] = asdict(item_quality)
    common_days = sorted(set.intersection(*(
        {session.day for session in sessions_by_symbol[symbol]}
        for symbol in DATA_SYMBOLS
    )))
    if len(common_days) < MIN_SESSIONS:
        raise RuntimeError("Insufficient common complete stock sessions for the preregistered study")
    split = len(common_days) * 2 // 3
    development_days = common_days[:split]
    out_of_sample_days = common_days[split:]
    fold_size = len(common_days) // 3
    folds = [
        common_days[index * fold_size:(index + 1) * fold_size if index < 2 else len(common_days)]
        for index in range(3)
    ]
    costs = (BASE_COST, STRESS_COST)
    report = {
        "generated_at": now.isoformat(),
        "days": days,
        "timeframe": timespan,
        "preregistration": (
            "reports/equity-orb-m1-holdout-preregistration.md"
            if timespan == "M1"
            else "reports/equity-orb-preregistration.md"
        ),
        "candidate_symbols": list(SYMBOLS),
        "benchmark_symbol": BENCHMARK,
        "common_sessions": len(common_days),
        "first_session": common_days[0].isoformat(),
        "last_session": common_days[-1].isoformat(),
        "quality": quality,
        "strategy": {
            "opening_range_minutes": 5,
            "lookback_sessions": LOOKBACK,
            "relative_volume_minimum": 1.0,
            "daily_selection": "highest opening relative volume",
            "stop_atr_fraction": 0.10,
            "exit": "end of regular session",
            "risk_per_trade": float(RISK_PER_TRADE),
            "maximum_notional": float(MAX_NOTIONAL),
            "parameter_search": False,
            "tested_strategy_variants": 1,
        },
        "full_period": {
            "costs": {f"{float(cost):.4f}": backtest_orb(sessions_by_symbol, days=common_days, cost=cost) for cost in costs},
        },
        "development": {
            "first_session": development_days[0].isoformat(),
            "last_session": development_days[-1].isoformat(),
            "costs": {f"{float(cost):.4f}": _compact(backtest_orb(sessions_by_symbol, days=development_days, cost=cost)) for cost in costs},
        },
        "out_of_sample": {
            "first_session": out_of_sample_days[0].isoformat(),
            "last_session": out_of_sample_days[-1].isoformat(),
            "costs": {f"{float(cost):.4f}": backtest_orb(sessions_by_symbol, days=out_of_sample_days, cost=cost) for cost in costs},
        },
        "walk_forward": {
            f"{float(cost):.4f}": [_compact(backtest_orb(sessions_by_symbol, days=fold, cost=cost)) for fold in folds]
            for cost in costs
        },
    }
    report["professional_gate"] = _gate(report)
    return report


def run_orb_m1_holdout_backtest(api: WebullAPI) -> dict[str, Any]:
    return run_orb_backtest(
        api,
        days=90,
        now=datetime(2026, 6, 1, tzinfo=timezone.utc),
        timespan="M1",
    )


def markdown_report(report: dict[str, Any]) -> str:
    lines = [
        f"# Webull股票Sandbox ORB {report['timeframe']}专业审查",
        "",
        f"生成时间：{report['generated_at']}",
        f"范围：{report['first_session']}至{report['last_session']}，共{report['common_sessions']}个完整共同交易日。",
        "",
        f"规则已在回测前写入`{report['preregistration']}`；本次只测试一个固定版本。",
        "基准成本为每边5个基点，压力成本为每边10个基点；单笔风险预算0.05%，名义仓位上限10%。",
        "",
        "## 分段结果",
        "",
        "| 分段 | 成本/边 | 净收益 | 账户收益率 | 交易数 | 同K线止损 | 胜率 | 利润因子 | 最大回撤 | 平均R | QQQ基准 | 超额收益 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for label, key in (("全样本", "full_period"), ("开发段", "development"), ("最终样本外", "out_of_sample")):
        for cost, metrics in report[key]["costs"].items():
            factor = metrics["profit_factor"]
            lines.append(
                f"| {label} | {float(cost):.2%} | ${metrics['net_profit']:,.2f} | {metrics['account_return']:.3%} | "
                f"{metrics['trade_count']} | {metrics['same_bar_stop_count']} | {metrics['win_rate']:.1%} | "
                f"{'—' if factor is None else f'{factor:.2f}'} | "
                f"{metrics['max_drawdown']:.3%} | {metrics['average_r_multiple']:.2f} | "
                f"${metrics['benchmark']['net_profit']:,.2f} | ${metrics['excess_vs_benchmark']:,.2f} |"
            )
    gate = report["professional_gate"]
    lines.extend([
        "",
        "## 放行审查",
        "",
        f"结论：`{gate['decision']}`",
        f"三个连续时段正收益：{gate['positive_walk_forward_folds']}/3",
        "",
    ])
    for name, passed in gate["checks"].items():
        lines.append(f"- {'通过' if passed else '失败'}：`{name}`")
    lines.extend([
        "",
        "即使结论为`FORWARD_SHADOW`，也只允许进入20个交易日的无下单前向观察，不会直接自动交易。",
        "",
    ])
    return "\n".join(lines)


def save_report(report: dict[str, Any]) -> tuple[Path, Path]:
    json_path, markdown_path = (
        (M1_REPORT_JSON, M1_REPORT_MARKDOWN)
        if report.get("timeframe") == "M1"
        else (REPORT_JSON, REPORT_MARKDOWN)
    )
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    markdown_path.write_text(markdown_report(report), encoding="utf-8")
    return json_path, markdown_path

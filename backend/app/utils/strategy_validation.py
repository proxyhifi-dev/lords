from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from backend.app.core.config_loader import get_settings
from backend.app.storage.trade_store import TradeStore

settings = get_settings()
ROOT = Path(__file__).resolve().parents[3]
DOCS_DIR = ROOT / "docs"
MARKDOWN_PATH = DOCS_DIR / "strategy-validation-report.md"
SUMMARY_PATH = DOCS_DIR / "strategy-validation-summary.json"


def _to_float(value: Any, default: float = 0.0) -> float:
    if value is None or isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return default


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _trade_day(trade: dict[str, Any]) -> str:
    entry_time = _clean_text(trade.get("entry_time"))
    if entry_time:
        try:
            return datetime.fromisoformat(entry_time).date().isoformat()
        except ValueError:
            pass
    return _clean_text(trade.get("date")) or "unknown"


def _is_closed_iron_condor_trade(trade: dict[str, Any]) -> bool:
    strategy = _clean_text(trade.get("strategy") or trade.get("signal")).upper()
    status = _clean_text(trade.get("status")).upper()
    if strategy != "IRON_CONDOR":
        return False
    if status == "CLOSED":
        return True
    return any(_clean_text(trade.get(key)) for key in ("exit_time", "exit_reason", "reason"))


@dataclass
class StrategyReport:
    total_trades: int
    win_rate_pct: float
    gross_pnl: float
    total_charges: float
    net_pnl: float
    profit_factor: float
    max_drawdown: float
    average_win: float
    average_loss: float
    expectancy_per_trade: float
    target_trades_net_negative: int
    stop_loss_trades: int
    eod_trades: int
    expiry_forced_exits: int
    best_day: str
    best_day_pnl: float
    worst_day: str
    worst_day_pnl: float
    charges_aware_target_blocks: int

    def to_summary(self) -> dict[str, Any]:
        return {
            "generated_at": datetime.utcnow().isoformat() + "Z",
            "total_trades": self.total_trades,
            "win_rate_pct": round(self.win_rate_pct, 2),
            "gross_pnl": round(self.gross_pnl, 2),
            "total_charges": round(self.total_charges, 2),
            "net_pnl": round(self.net_pnl, 2),
            "profit_factor": round(self.profit_factor, 2),
            "max_drawdown": round(self.max_drawdown, 2),
            "average_win": round(self.average_win, 2),
            "average_loss": round(self.average_loss, 2),
            "expectancy_per_trade": round(self.expectancy_per_trade, 2),
            "target_trades_net_negative": self.target_trades_net_negative,
            "stop_loss_trades": self.stop_loss_trades,
            "eod_trades": self.eod_trades,
            "expiry_forced_exits": self.expiry_forced_exits,
            "best_day": self.best_day,
            "best_day_pnl": round(self.best_day_pnl, 2),
            "worst_day": self.worst_day,
            "worst_day_pnl": round(self.worst_day_pnl, 2),
            "charges_aware_target_blocks": self.charges_aware_target_blocks,
        }


def build_strategy_report(trades: list[dict[str, Any]]) -> StrategyReport:
    closed = [trade for trade in trades if _is_closed_iron_condor_trade(trade)]
    net_values = [_to_float(trade.get("net_pnl") or trade.get("pnl"), 0.0) for trade in closed]
    gross_values = [_to_float(trade.get("gross_pnl"), 0.0) for trade in closed]
    charge_values = [_to_float(trade.get("total_charges"), 0.0) for trade in closed]

    wins = [value for value in net_values if value > 0]
    losses = [value for value in net_values if value < 0]
    total_trades = len(closed)
    gross_pnl = sum(gross_values)
    total_charges = sum(charge_values)
    net_pnl = sum(net_values)
    win_rate_pct = (len(wins) / total_trades * 100.0) if total_trades else 0.0
    profit_factor = (sum(wins) / abs(sum(losses))) if losses else float("inf") if wins else 0.0
    average_win = (sum(wins) / len(wins)) if wins else 0.0
    average_loss = (sum(losses) / len(losses)) if losses else 0.0
    expectancy = (net_pnl / total_trades) if total_trades else 0.0

    cumulative = 0.0
    peak = 0.0
    max_drawdown = 0.0
    daily_pnl: defaultdict[str, float] = defaultdict(float)
    target_trades_net_negative = 0
    stop_loss_trades = 0
    eod_trades = 0
    expiry_forced_exits = 0
    charges_aware_target_blocks = 0

    for trade, net_value in zip(closed, net_values):
        reason = _clean_text(trade.get("reason") or trade.get("exit_reason")).upper()
        day = _trade_day(trade)
        daily_pnl[day] += net_value

        cumulative += net_value
        peak = max(peak, cumulative)
        max_drawdown = max(max_drawdown, peak - cumulative)

        if "TARGET" in reason and net_value < 0:
            target_trades_net_negative += 1
            charges_aware_target_blocks += 1
        if "STOP" in reason or reason == "SL":
            stop_loss_trades += 1
        if "EOD" in reason:
            eod_trades += 1
        if "EXPIRY" in reason or "FORCED_EXIT" in reason:
            expiry_forced_exits += 1

    if daily_pnl:
        best_day, best_day_pnl = max(daily_pnl.items(), key=lambda item: item[1])
        worst_day, worst_day_pnl = min(daily_pnl.items(), key=lambda item: item[1])
    else:
        best_day, best_day_pnl = "n/a", 0.0
        worst_day, worst_day_pnl = "n/a", 0.0

    return StrategyReport(
        total_trades=total_trades,
        win_rate_pct=win_rate_pct,
        gross_pnl=gross_pnl,
        total_charges=total_charges,
        net_pnl=net_pnl,
        profit_factor=profit_factor if profit_factor != float("inf") else 9999.0,
        max_drawdown=max_drawdown,
        average_win=average_win,
        average_loss=average_loss,
        expectancy_per_trade=expectancy,
        target_trades_net_negative=target_trades_net_negative,
        stop_loss_trades=stop_loss_trades,
        eod_trades=eod_trades,
        expiry_forced_exits=expiry_forced_exits,
        best_day=best_day,
        best_day_pnl=best_day_pnl,
        worst_day=worst_day,
        worst_day_pnl=worst_day_pnl,
        charges_aware_target_blocks=charges_aware_target_blocks,
    )


def render_strategy_report_markdown(report: StrategyReport) -> str:
    summary = report.to_summary()
    return "\n".join(
        [
            "# Strategy Validation Report",
            "",
            "## Summary",
            f"- Total trades: `{summary['total_trades']}`",
            f"- Win rate (net): `{summary['win_rate_pct']}%`",
            f"- Gross P&L: `Rs {summary['gross_pnl']}`",
            f"- Charges: `Rs {summary['total_charges']}`",
            f"- Net P&L: `Rs {summary['net_pnl']}`",
            f"- Profit factor (net): `{summary['profit_factor']}`",
            f"- Max drawdown: `Rs {summary['max_drawdown']}`",
            f"- Average win: `Rs {summary['average_win']}`",
            f"- Average loss: `Rs {summary['average_loss']}`",
            f"- Expectancy per trade: `Rs {summary['expectancy_per_trade']}`",
            "",
            "## Exit Quality",
            f"- TARGET trades net negative after charges: `{summary['target_trades_net_negative']}`",
            f"- STOP/SL trades: `{summary['stop_loss_trades']}`",
            f"- EOD trades: `{summary['eod_trades']}`",
            f"- Expiry forced exits: `{summary['expiry_forced_exits']}`",
            f"- Charges-aware target would likely have blocked: `{summary['charges_aware_target_blocks']}`",
            "",
            "## Daily Range",
            f"- Best day: `{summary['best_day']}` (`Rs {summary['best_day_pnl']}`)",
            f"- Worst day: `{summary['worst_day']}` (`Rs {summary['worst_day_pnl']}`)",
            "",
            "## Verdict",
            (
                "- Net-P&L evidence is supportive of a `70+` strategy-readiness score."
                if summary["net_pnl"] > 0
                and summary["profit_factor"] >= 1.1
                and summary["target_trades_net_negative"] == 0
                else "- Net-P&L evidence is not yet strong enough to honestly claim a `70+` strategy-readiness score."
            ),
        ]
    ) + "\n"


def generate_strategy_validation_artifacts() -> StrategyReport:
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    store = TradeStore(settings.trades_file)
    trades = store.get_all_trades()
    report = build_strategy_report(trades)
    MARKDOWN_PATH.write_text(render_strategy_report_markdown(report), encoding="utf-8")
    SUMMARY_PATH.write_text(json.dumps(report.to_summary(), indent=2), encoding="utf-8")
    return report


if __name__ == "__main__":
    generate_strategy_validation_artifacts()

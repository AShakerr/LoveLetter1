"""The three live guards (docs/BRIEF.md 8b) plus the kill switch that also halts paper execution.

1. DESK_LIVE=1 in the environment, absent by default. Without it every submit() to a live adapter raises.
2. config/limits.yaml live.max_daily_notional_eur / live.max_order_notional_eur. Exceeding either rejects the
   order and writes a rules_fired row with severity mandatory so it is visible.
3. Kill switch: if data/KILL exists, the scheduler skips execution entirely and the UI shows a red banner.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

import yaml
from sqlmodel import Session, select

from desk.config import Settings, get_settings
from desk.models import OrderRow, RuleFired


class GuardError(Exception):
    pass


@dataclass
class LiveLimits:
    max_daily_notional_eur: float = 2000.0
    max_order_notional_eur: float = 1000.0

    @classmethod
    def load(cls, settings: Settings) -> LiveLimits:
        doc = (
            yaml.safe_load((settings.config_dir / "limits.yaml").read_text(encoding="utf-8")) or {}
        )
        live = doc.get("live") or {}
        return cls(
            float(live.get("max_daily_notional_eur", 2000)),
            float(live.get("max_order_notional_eur", 1000)),
        )


def kill_switch_active(settings: Settings | None = None) -> bool:
    settings = settings or get_settings()
    return settings.kill_file.exists()


def engage_kill_switch(settings: Settings | None = None, reason: str = "engaged from UI") -> None:
    settings = settings or get_settings()
    settings.ensure_dirs()
    settings.kill_file.write_text(f"{dt.datetime.now().isoformat()} {reason}\n", encoding="utf-8")


def release_kill_switch(confirm: str, settings: Settings | None = None) -> None:
    """Removing the file requires typing the word CONFIRM (or deleting data/KILL by hand)."""
    settings = settings or get_settings()
    if confirm.strip() != "CONFIRM":
        raise GuardError("type CONFIRM to release the kill switch")
    settings.kill_file.unlink(missing_ok=True)


def live_allowed(settings: Settings | None = None) -> bool:
    settings = settings or get_settings()
    return bool(settings.live)


def live_notional_today(session: Session, today: dt.date, broker: str) -> float:
    rows = session.exec(
        select(OrderRow).where(
            OrderRow.broker == broker,
            OrderRow.order_date == today,
            OrderRow.status.in_(["submitted", "filled", "partial"]),
        )
    ).all()
    return sum(r.notional or 0.0 for r in rows)


def check_live_order(
    session: Session,
    *,
    broker: str,
    notional_eur: float,
    today: dt.date,
    instrument_id: int,
    settings: Settings | None = None,
) -> None:
    """Raise GuardError (and log a mandatory rules_fired row) when a live order breaks a guard."""
    settings = settings or get_settings()
    if not live_allowed(settings):
        raise GuardError("DESK_LIVE is not set; live submit refused")
    limits = LiveLimits.load(settings)
    reason = None
    if notional_eur > limits.max_order_notional_eur:
        reason = f"order notional €{notional_eur:,.0f} exceeds live.max_order_notional_eur €{limits.max_order_notional_eur:,.0f}"
    elif live_notional_today(session, today, broker) + notional_eur > limits.max_daily_notional_eur:
        reason = f"daily live notional would exceed live.max_daily_notional_eur €{limits.max_daily_notional_eur:,.0f}"
    if reason:
        session.add(
            RuleFired(
                instrument_id=instrument_id,
                date=today,
                rule="live_notional_cap",
                severity="mandatory",
                detail_json={"summary": reason, "notional_eur": notional_eur, "broker": broker},
            )
        )
        session.commit()
        raise GuardError(reason)

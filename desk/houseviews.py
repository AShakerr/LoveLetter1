"""Read model for the house-views page and, later, the Safra-alignment factor."""

from __future__ import annotations

from dataclasses import dataclass

from sqlmodel import Session, select

from desk.ingest.safra import STANCE_RANK, TACTICAL_KINDS
from desk.models import HouseView, Report

SCOPE_ORDER = ["asset", "region", "sector", "stock", "index_target", "rate", "fx", "commodity"]


@dataclass
class ViewRow:
    view: HouseView
    report: Report

    @property
    def tactical(self) -> bool:
        return self.report.kind in TACTICAL_KINDS

    @property
    def direction(self) -> str:
        """upgrade | downgrade | changed | none"""
        cf = self.view.changed_from
        if not cf:
            return "none"
        a, b = STANCE_RANK.get(cf), STANCE_RANK.get(self.view.stance or "")
        if a is None or b is None:
            try:
                fa, fb = float(cf.replace("'", "")), float((self.view.value or "").replace("'", ""))
            except ValueError:
                return "changed"
            return "upgrade" if fb > fa else "downgrade" if fb < fa else "changed"
        return "upgrade" if b > a else "downgrade" if b < a else "changed"


def all_views(session: Session) -> list[ViewRow]:
    stmt = (
        select(HouseView, Report)
        .join(Report, HouseView.report_id == Report.id)
        .order_by(Report.date.desc(), Report.id.desc(), HouseView.id)
    )
    return [ViewRow(v, r) for v, r in session.exec(stmt).all()]


def latest_views(session: Session, include_tactical: bool = False) -> dict[str, list[ViewRow]]:
    """Newest view per (scope, key) from the strategic tier, grouped by scope in display order."""
    seen: set[tuple[str, str]] = set()
    grouped: dict[str, list[ViewRow]] = {s: [] for s in SCOPE_ORDER}
    for row in all_views(session):
        if row.tactical and not include_tactical:
            continue
        k = (row.view.scope, row.view.key)
        if k in seen:
            continue
        seen.add(k)
        grouped.setdefault(row.view.scope, []).append(row)
    for rows in grouped.values():
        rows.sort(key=lambda r: (-(STANCE_RANK.get(r.view.stance or "", -1)), r.view.key))
    return {s: rows for s, rows in grouped.items() if rows}


def current_stance(session: Session, scope: str, key: str) -> ViewRow | None:
    for row in all_views(session):
        if row.tactical:
            continue
        if row.view.scope == scope and row.view.key == key:
            return row
    return None


def change_log(session: Session) -> list[ViewRow]:
    return [r for r in all_views(session) if r.view.changed_from]


def reports(session: Session) -> list[Report]:
    return list(session.exec(select(Report).order_by(Report.date.desc(), Report.id.desc())).all())


def latest_headline(session: Session) -> tuple[Report | None, dict]:
    for rep in reports(session):
        if rep.flagged or rep.kind in TACTICAL_KINDS or not rep.raw_json:
            continue
        hs = rep.raw_json.get("headline_stance")
        if hs:
            return rep, hs
    return None, {}


def risks(session: Session, limit: int = 12) -> list[dict]:
    out = []
    for rep in reports(session):
        for r in (rep.raw_json or {}).get("risks", []):
            out.append({"date": rep.date, "kind": rep.kind, **r})
    return out[:limit]

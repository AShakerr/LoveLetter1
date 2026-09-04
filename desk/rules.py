"""Rules engine (docs/BRIEF.md section 8). Runs after scoring.

MANDATORY rules produce SELL/TRIM actions the user must respond to. REVIEW rules produce flags.
The two are never merged: `severity` is on every flag and the UI renders them differently.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

import yaml
from sqlmodel import Session, select

from desk.config import Settings, get_settings
from desk.houseviews import current_stance
from desk.kill_conditions import kill_block
from desk.models import Instrument, InstrumentKind, RuleFired
from desk.portfolio import Limits, PortfolioView, PositionView
from desk.predicates import Context, PredicateError, evaluate
from desk.score import ScoreResult, score_history
from desk.tape import TAPE, load_tape

MANDATORY, REVIEW = "mandatory", "review"


@dataclass
class Flag:
    rule: str
    severity: str
    instrument_id: int | None
    position_id: int | None = None
    action: str | None = None  # SELL | TRIM for mandatory
    size_pct: float | None = None  # fraction of portfolio to trim/sell
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def summary(self) -> str:
        return self.detail.get("summary", self.rule)


@dataclass
class RuleConfig:
    max_single_position: float = 0.15
    max_single_theme: float = 0.35
    min_diversified_core_warn: float = 0.25
    stop_loss: dict[str, float | None] = field(
        default_factory=lambda: {"stock": 0.18, "etf": 0.12, "private": None}
    )
    score_decay_points: float = 15
    score_decay_days: int = 30
    score_floor: float = 45
    momentum_sentiment_floor: float = -0.15
    stale_days: int = 7
    stale_days_manual: int = 14

    @classmethod
    def load(cls, settings: Settings) -> RuleConfig:
        doc = (
            yaml.safe_load((settings.config_dir / "limits.yaml").read_text(encoding="utf-8")) or {}
        )
        rc = cls()
        for k in ("max_single_position", "max_single_theme", "min_diversified_core_warn"):
            if k in doc:
                setattr(rc, k, float(doc[k]))
        if isinstance(doc.get("stop_loss"), dict):
            rc.stop_loss = {
                k: (None if v is None else float(v)) for k, v in doc["stop_loss"].items()
            }
        for k, v in (doc.get("review") or {}).items():
            if hasattr(rc, k):
                setattr(rc, k, type(getattr(rc, k))(v))
        return rc


def _unconfirmed_pot(p: PositionView) -> bool:
    return p.instrument.composition_confirmed is False


# ---------------------------------------------------------------------------------------- mandatory rules
def rule_stop_loss(p: PositionView, cfg: RuleConfig) -> Flag | None:
    stop = p.position.stop_pct
    if stop is None:
        stop = cfg.stop_loss.get(p.instrument.kind.value)
    if stop is None or p.price is None or not p.position.avg_cost:
        return None
    level = p.position.avg_cost * (1 - stop)
    if p.price < level:
        return Flag(
            "stop_loss",
            MANDATORY,
            p.instrument.id,
            p.position.id,
            "SELL",
            p.weight,
            {
                "summary": f"{p.instrument.ticker} {p.price:.2f} below stop {level:.2f} ({stop:.0%} under avg cost {p.position.avg_cost:.2f})",
                "price": p.price,
                "avg_cost": p.position.avg_cost,
                "stop_pct": stop,
                "stop_level": level,
                "price_source": p.price_source,
                "price_as_of": str(p.price_as_of),
            },
        )
    return None


def rule_max_position(p: PositionView, cfg: RuleConfig) -> Flag | None:
    if p.instrument.kind == InstrumentKind.cash or p.weight <= cfg.max_single_position:
        return None
    excess = p.weight - cfg.max_single_position
    if _unconfirmed_pot(p):
        return Flag(
            "max_position",
            REVIEW,
            p.instrument.id,
            p.position.id,
            None,
            None,
            {
                "summary": f"confirm pot composition: {p.instrument.ticker} is {p.weight:.1%} of the book (limit {cfg.max_single_position:.0%}); "
                f"the TRIM becomes mandatory once the composition is confirmed",
                "weight": p.weight,
                "limit": cfg.max_single_position,
                "excess": excess,
                "composition_confirmed": False,
            },
        )
    return Flag(
        "max_position",
        MANDATORY,
        p.instrument.id,
        p.position.id,
        "TRIM",
        excess,
        {
            "summary": f"{p.instrument.ticker} is {p.weight:.1%} of the book; trim {excess:.1%} to reach {cfg.max_single_position:.0%}",
            "weight": p.weight,
            "limit": cfg.max_single_position,
            "excess": excess,
        },
    )


def rule_max_theme(view: PortfolioView, cfg: RuleConfig) -> list[Flag]:
    flags = []
    for theme, w in view.by_theme.items():
        if theme == "cash" or w <= cfg.max_single_theme:
            continue
        members = [
            p
            for p in view.positions
            if p.theme == theme and p.instrument.kind != InstrumentKind.cash
        ]
        if not members:
            continue
        largest = max(members, key=lambda p: p.weight)
        excess = w - cfg.max_single_theme
        detail = {
            "theme": theme,
            "theme_weight": w,
            "limit": cfg.max_single_theme,
            "excess": excess,
            "largest": largest.instrument.ticker,
        }
        if _unconfirmed_pot(largest):
            flags.append(
                Flag(
                    "max_theme",
                    REVIEW,
                    largest.instrument.id,
                    largest.position.id,
                    None,
                    None,
                    {
                        **detail,
                        "composition_confirmed": False,
                        "summary": f"confirm pot composition: theme {theme} is {w:.1%} (limit {cfg.max_single_theme:.0%}) on the assumption "
                        f"that {largest.instrument.ticker} is {theme}; the TRIM becomes mandatory once confirmed",
                    },
                )
            )
        else:
            flags.append(
                Flag(
                    "max_theme",
                    MANDATORY,
                    largest.instrument.id,
                    largest.position.id,
                    "TRIM",
                    min(excess, largest.weight),
                    {
                        **detail,
                        "summary": f"theme {theme} is {w:.1%}; trim {largest.instrument.ticker} by {excess:.1%} to bring the theme to {cfg.max_single_theme:.0%}",
                    },
                )
            )
    return flags


def rule_thesis(
    session: Session, p: PositionView, view: PortfolioView, today: dt.date
) -> list[Flag]:
    """Evaluate every kill entry on the position.

    predicate true + mandatory -> MANDATORY SELL (thesis_invalidated)
    predicate true + review    -> REVIEW (thesis_review)
    human entry                -> REVIEW (thesis_human): the DSL cannot check it, so it is shown, never dropped
    evaluation error           -> REVIEW (thesis_unevaluable) with the thesis text
    While a pot's composition is unconfirmed, its mandatory hits are downgraded to REVIEW with the
    "confirm pot composition" prefix (the pre_condition in the kill file)."""
    block = kill_block(p.position)
    if not block:
        return []
    thesis = block.get("thesis") or "(no thesis text)"
    ctx = Context(session, p.instrument, p.position, view.by_theme, today)
    out: list[Flag] = []
    for k in block.get("kills") or []:
        sev = k.get("severity", "mandatory")
        note = k.get("note")
        if k.get("human"):
            out.append(
                Flag(
                    "thesis_human",
                    REVIEW,
                    p.instrument.id,
                    p.position.id,
                    None,
                    None,
                    {
                        "summary": f"{p.instrument.ticker}: needs a human check ({sev}): {k['human']}"
                        + (f" — {note}" if note else "")
                        + f". Thesis: {thesis}",
                        "human": k["human"],
                        "severity_declared": sev,
                        "thesis": thesis,
                        "note": note,
                    },
                )
            )
            continue
        pred = k.get("predicate")
        try:
            fired = evaluate(pred, ctx)
        except PredicateError as exc:
            out.append(
                Flag(
                    "thesis_unevaluable",
                    REVIEW,
                    p.instrument.id,
                    p.position.id,
                    None,
                    None,
                    {
                        "summary": f"{p.instrument.ticker}: kill condition cannot be evaluated ({exc}): `{pred}`. Thesis: {thesis}",
                        "predicate": pred,
                        "error": str(exc),
                        "thesis": thesis,
                        "severity_declared": sev,
                        "note": note,
                    },
                )
            )
            continue
        if not fired:
            continue
        detail = {"predicate": pred, "thesis": thesis, "note": note, "severity_declared": sev}
        if sev == "mandatory" and _unconfirmed_pot(p):
            out.append(
                Flag(
                    "thesis_invalidated",
                    REVIEW,
                    p.instrument.id,
                    p.position.id,
                    None,
                    None,
                    {
                        **detail,
                        "composition_confirmed": False,
                        "summary": f"confirm pot composition: {p.instrument.ticker} kill condition `{pred}` is true"
                        + (f" — {note}" if note else "")
                        + "; it becomes a mandatory SELL once the composition is confirmed",
                    },
                )
            )
        elif sev == "mandatory":
            out.append(
                Flag(
                    "thesis_invalidated",
                    MANDATORY,
                    p.instrument.id,
                    p.position.id,
                    "SELL",
                    p.weight,
                    {
                        **detail,
                        "summary": f"{p.instrument.ticker}: kill condition true: `{pred}`"
                        + (f" — {note}" if note else "")
                        + f". Thesis: {thesis}",
                    },
                )
            )
        else:
            out.append(
                Flag(
                    "thesis_review",
                    REVIEW,
                    p.instrument.id,
                    p.position.id,
                    None,
                    None,
                    {
                        **detail,
                        "summary": f"{p.instrument.ticker}: review trigger true: `{pred}`"
                        + (f" — {note}" if note else ""),
                    },
                )
            )
    return out


def add_blocked(
    session: Session, p: PositionView, view: PortfolioView, today: dt.date
) -> str | None:
    """The kill file's add_blocked_while predicate, when it is true. Errors count as blocked (with the reason)."""
    block = kill_block(p.position)
    pred = (block or {}).get("add_blocked_while")
    if not pred:
        return None
    try:
        return (
            pred
            if evaluate(pred, Context(session, p.instrument, p.position, view.by_theme, today))
            else None
        )
    except PredicateError as exc:
        return f"{pred} (cannot evaluate: {exc})"


def rule_house_downgrade(session: Session, p: PositionView) -> Flag | None:
    for scope, key in (("sector", p.instrument.sector), ("region", p.instrument.region)):
        if not key:
            continue
        row = current_stance(session, scope, key)
        if row is not None and row.view.stance == "least_preferred" and row.view.changed_from:
            return Flag(
                "house_downgrade_to_least",
                MANDATORY,
                p.instrument.id,
                p.position.id,
                "SELL",
                p.weight,
                {
                    "summary": f"Safra moved {scope} {key} to least preferred (from {row.view.changed_from}) on {row.report.date}",
                    "scope": scope,
                    "key": key,
                    "changed_from": row.view.changed_from,
                    "report_date": str(row.report.date),
                    "quote": row.view.quote,
                },
            )
    return None


# ------------------------------------------------------------------------------------------- review rules
def rule_score_decay(
    session: Session, p: PositionView, res: ScoreResult | None, cfg: RuleConfig, today: dt.date
) -> Flag | None:
    if res is None:
        return None
    total = res.row.total
    hist = score_history(session, p.instrument.id, today - dt.timedelta(days=cfg.score_decay_days))
    past = hist[0].total if hist and hist[0].date < today else None
    drop = (past - total) if past is not None else None
    if total < cfg.score_floor or (drop is not None and drop > cfg.score_decay_points):
        why = (
            f"score {total:.0f} below {cfg.score_floor:.0f}"
            if total < cfg.score_floor
            else f"score fell {drop:.0f} points in {cfg.score_decay_days} days"
        )
        return Flag(
            "score_decay",
            REVIEW,
            p.instrument.id,
            p.position.id,
            None,
            None,
            {
                "summary": f"{p.instrument.ticker}: {why}",
                "score": total,
                "past": past,
                "drop": drop,
                "provisional": res.provisional,
            },
        )
    return None


def rule_momentum_break(p: PositionView, res: ScoreResult | None, cfg: RuleConfig) -> Flag | None:
    if res is None:
        return None
    m = res.factors["momentum"].inputs
    ret = (m.get("return_3m") or {}).get("return_pct")
    sent = (m.get("sentiment_14d") or {}).get("mean")
    if ret is not None and sent is not None and ret < 0 and sent < cfg.momentum_sentiment_floor:
        return Flag(
            "momentum_break",
            REVIEW,
            p.instrument.id,
            p.position.id,
            None,
            None,
            {
                "summary": f"{p.instrument.ticker}: 3-month return {ret:.1f}% with sentiment {sent:.2f}",
                "return_3m_pct": ret,
                "sentiment_14d": sent,
            },
        )
    return None


def rule_stale_position(p: PositionView, cfg: RuleConfig, today: dt.date) -> Flag | None:
    if p.price_as_of is None or p.instrument.kind in (InstrumentKind.cash, InstrumentKind.other):
        return None
    manual = (p.price_source or "").endswith(("manual", "seed", "screenshot"))
    limit = cfg.stale_days_manual if manual else cfg.stale_days
    age = (today - p.price_as_of).days
    if age > limit:
        return Flag(
            "stale_data",
            REVIEW,
            p.instrument.id,
            p.position.id,
            None,
            None,
            {
                "summary": f"{p.instrument.ticker}: price is {age} days old ({p.price_source}, limit {limit})",
                "age_days": age,
                "limit": limit,
                "source": p.price_source,
            },
        )
    return None


def rule_stale_inputs(session: Session, cfg: RuleConfig) -> Flag | None:
    stale = [
        f"{i.spec.label} ({i.age_days}d, {i.source})"
        for i in load_tape(session, TAPE)
        if i.freshness == "stale" or i.value is None
    ]
    if stale:
        return Flag(
            "stale_data",
            REVIEW,
            None,
            None,
            None,
            None,
            {"summary": "stale or missing market inputs: " + ", ".join(stale), "items": stale},
        )
    return None


def rule_concentration(
    view: PortfolioView, cfg: RuleConfig, core_themes: tuple[str, ...]
) -> Flag | None:
    core = sum(p.weight for p in view.positions if p.theme in core_themes)
    if view.positions and core < cfg.min_diversified_core_warn:
        return Flag(
            "concentration_warning",
            REVIEW,
            None,
            None,
            None,
            None,
            {
                "summary": f"diversified core ({', '.join(core_themes)}) is {core:.1%} of the book (warn below {cfg.min_diversified_core_warn:.0%})",
                "core": core,
                "warn": cfg.min_diversified_core_warn,
            },
        )
    return None


# ------------------------------------------------------------------------------------------------ driver
def run_rules(
    session: Session,
    view: PortfolioView,
    scores: dict[int, ScoreResult],
    *,
    settings: Settings | None = None,
    today: dt.date | None = None,
    persist: bool = True,
) -> list[Flag]:
    settings = settings or get_settings()
    today = today or dt.date.today()
    cfg = RuleConfig.load(settings)
    core_themes = Limits.load(settings.config_dir / "limits.yaml").core_themes
    flags: list[Flag] = []
    for p in view.positions:
        if p.instrument.kind in (InstrumentKind.cash, InstrumentKind.other):
            continue
        res = scores.get(p.instrument.id)
        for f in (
            rule_stop_loss(p, cfg),
            rule_max_position(p, cfg),
            rule_house_downgrade(session, p),
            rule_score_decay(session, p, res, cfg, today),
            rule_momentum_break(p, res, cfg),
            rule_stale_position(p, cfg, today),
        ):
            if f is not None:
                flags.append(f)
        flags.extend(rule_thesis(session, p, view, today))
    flags.extend(rule_max_theme(view, cfg))
    for f in (rule_stale_inputs(session, cfg), rule_concentration(view, cfg, core_themes)):
        if f is not None:
            flags.append(f)
    if persist:
        for old in session.exec(select(RuleFired).where(RuleFired.date == today)).all():
            session.delete(old)
        for f in flags:
            session.add(
                RuleFired(
                    position_id=f.position_id,
                    instrument_id=f.instrument_id,
                    date=today,
                    rule=f.rule,
                    severity=f.severity,
                    detail_json={**f.detail, "action": f.action, "size_pct": f.size_pct},
                )
            )
        session.commit()
    return flags


def flags_for(flags: list[Flag], instrument_id: int) -> list[Flag]:
    return [f for f in flags if f.instrument_id == instrument_id]


def global_flags(flags: list[Flag]) -> list[Flag]:
    return [f for f in flags if f.instrument_id is None]


def instrument_by_id(session: Session, instrument_id: int) -> Instrument | None:
    return session.get(Instrument, instrument_id)

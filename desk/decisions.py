"""Decision generation (docs/BRIEF.md section 8): regime -> scores -> rules -> decisions -> paper broker.

`decisions` is append-only. A decision's content is never updated; the user's response goes in
user_status / user_note / executed_at. Re-running the pipeline on the same day is idempotent per
(date, instrument, action).
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field
from typing import Any

from sqlmodel import Session, select

from desk.config import Settings, get_settings
from desk.crowd import deferral_reason
from desk.events import load_events_config
from desk.execution import seed_paper_book, settle_paper, submit_decision
from desk.kill_conditions import (
    candidate_conditions,
    condition_for,
    first_mandatory_predicate,
    kill_block,
)
from desk.models import Decision, Instrument, InstrumentKind, Position, Pot, Regime
from desk.narrative import add_narratives
from desk.portfolio import Limits, PortfolioView, build_portfolio
from desk.regime import classify
from desk.rules import Flag, RuleConfig, add_blocked, flags_for, global_flags, run_rules
from desk.score import WEIGHTS, ScoreResult, _latest_price, band, score_universe
from desk.sources.base import utcnow

log = logging.getLogger(__name__)

MAX_BUY_PER_DECISION = 0.05


@dataclass
class PipelineResult:
    date: dt.date
    regime: Regime
    view: PortfolioView
    scores: dict[int, ScoreResult]
    flags: list[Flag]
    decisions: list[Decision] = field(default_factory=list)
    created: int = 0
    orders_submitted: int = 0
    deferred: int = 0
    notes: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------------------------- reasoning
def factor_table(res: ScoreResult) -> str:
    lines = ["| Factor | Score (0-5) | Weight | Points | Basis |", "|---|---|---|---|---|"]
    for name, f in res.factors.items():
        pts = f.effective / 5 * WEIGHTS[name]
        basis = f.note or _basis(name, f.inputs)
        val = "—" if f.value is None else f"{f.value:.2f}"
        lines.append(f"| {name} | {val} | {WEIGHTS[name]} | {pts:.1f} | {basis} |")
    lines.append(
        f"| **total** | | 100 | **{res.row.total:.1f}** | band: {res.band}{' (provisional)' if res.provisional else ''} |"
    )
    return "\n".join(lines)


def _basis(name: str, inputs: dict[str, Any]) -> str:
    try:
        if name == "safra":
            if "stock_rating" in inputs:
                return f"stock rating {inputs['stock_rating']['stance']}"
            comps = inputs.get("components", {})
            return ", ".join(f"{k} {v['key']}={v['stance']}" for k, v in comps.items()) + (
                f"; recent change {inputs.get('recent_change_adjustment', 0):+.1f}"
                if inputs.get("recent_change_adjustment")
                else ""
            )
        if name == "regime":
            return f"fit now {inputs.get('fit_current')} / reverse {inputs.get('fit_reverse')} ({inputs.get('regime')})"
        if name == "portfolio":
            return f"{inputs.get('why')}; corr with {inputs.get('largest_holding')} {inputs.get('corr_90d')}"
        if name == "valuation":
            parts = []
            if "pe" in inputs:
                parts.append(
                    f"P/E {inputs['pe']['current']} vs median {inputs['pe']['median']} (n={inputs['pe']['n']})"
                )
            if "target" in inputs:
                parts.append(
                    f"{inputs['target']['key']} {inputs['target']['target']} vs {inputs['target']['current']} ({inputs['target']['upside_pct']:+.1f}%)"
                )
            return "; ".join(parts) or "—"
        if name == "momentum":
            r, s = inputs.get("return_3m", {}), inputs.get("sentiment_14d", {})
            parts = []
            if "return_pct" in r:
                parts.append(f"3m {r['return_pct']:+.1f}% (pct {r['percentile']:.2f})")
            if "mean" in s:
                parts.append(f"sentiment {s['mean']:+.2f} (n={s['n']})")
            return "; ".join(parts) or "—"
        if name == "season":
            return (
                inputs.get("rule")
                or f"Nov-Dec {inputs.get('nov_dec_concentration', 0)} + cyclical {inputs.get('cyclical_tilt', 0)}"
            )
    except (KeyError, TypeError):
        pass
    return "—"


def reasoning_markdown(
    action: str,
    inst: Instrument,
    regime: Regime,
    res: ScoreResult | None,
    flags: list[Flag],
    size_pct: float | None,
    kill: dict[str, Any] | None,
    reverse: str,
    basis_note: str | None,
) -> str:
    out = [f"# {action} {inst.ticker} — {inst.name}", "", f"**Regime:** {regime.label}", ""]
    if basis_note:
        out += [f"> {basis_note}", ""]
    if size_pct:
        out += [f"**Size:** {size_pct:.1%} of the portfolio", ""]
    if res is not None:
        out += ["## Score", "", factor_table(res), ""]
        if res.notes:
            out += ["Notes: " + "; ".join(res.notes), ""]
    mand = [f for f in flags if f.severity == "mandatory"]
    rev = [f for f in flags if f.severity == "review"]
    out += ["## Rules", ""]
    if mand:
        out += ["**MANDATORY**", ""] + [f"- `{f.rule}`: {f.summary}" for f in mand] + [""]
    if rev:
        out += ["**REVIEW**", ""] + [f"- `{f.rule}`: {f.summary}" for f in rev] + [""]
    if not mand and not rev:
        out += ["No rules fired.", ""]
    if kill and (kill.get("thesis") or kill.get("kills")):
        out += ["## Kill condition", "", f"{kill.get('thesis') or ''}", ""]
        for k in kill.get("kills") or []:
            sev = k.get("severity", "mandatory").upper()
            if k.get("predicate"):
                out.append(
                    f"- **{sev}** `{k['predicate']}`" + (f" — {k['note']}" if k.get("note") else "")
                )
            elif k.get("human"):
                out.append(
                    f"- **{sev}, human check** {k['human']}"
                    + (f" — {k['note']}" if k.get("note") else "")
                )
        if kill.get("add_blocked_while"):
            out.append(
                f"- ADD blocked while `{kill['add_blocked_while']}`"
                + (f" — {kill['add_note']}" if kill.get("add_note") else "")
            )
        if kill.get("pre_condition"):
            out.append(f"- Pre-condition: {kill['pre_condition']}")
        out.append("")
    out += ["## What would reverse this", "", reverse, ""]
    out += [
        "",
        "_Crowd measures what the crowd has already done and what it already expects; it does not predict what the crowd will do._",
        "",
    ]
    return "\n".join(out)


# ------------------------------------------------------------------------------------------------ driver
def _exists(session: Session, day: dt.date, instrument_id: int, action: str) -> bool:
    return (
        session.exec(
            select(Decision).where(
                Decision.date == day,
                Decision.instrument_id == instrument_id,
                Decision.action == action,
            )
        ).first()
        is not None
    )


def run_pipeline(
    session: Session, settings: Settings | None = None, today: dt.date | None = None
) -> PipelineResult:
    settings = settings or get_settings()
    today = today or dt.date.today()
    limits = Limits.load(settings.config_dir / "limits.yaml")
    cfg = RuleConfig.load(settings)
    load_events_config(session, settings)
    settle_paper(session, settings)
    view = build_portfolio(session, settings, limits)
    regime = classify(session, today)
    results = score_universe(session, view, regime, settings=settings, today=today)
    scores = {r.row.instrument_id: r for r in results}
    flags = run_rules(session, view, scores, settings=settings, today=today)
    out = PipelineResult(today, regime, view, scores, flags)
    basis_note = None
    if view.basis.startswith("pending"):
        basis_note = f"Positions are an UNCONFIRMED snapshot ({view.basis[8:]}); confirm it on the Portfolio page."
        out.notes.append(basis_note)
    if any(r.provisional for r in results):
        out.notes.append(
            "Scores are provisional: config/regime_fit.yaml is missing, f_regime contributes 0."
        )
    kills = candidate_conditions(settings)
    held = {p.instrument.id: p for p in view.positions}
    mandatory_ids = {f.instrument_id for f in flags if f.severity == "mandatory"}
    new: list[Decision] = []

    # 1. held positions: SELL / TRIM from mandatory rules, otherwise HOLD (written after the buy side,
    #    so an ADD on a held position supersedes its HOLD)
    proceeds = 0.0
    held_decisions: list[tuple] = []
    add_ids: set[int] = set()
    for p in view.positions:
        inst = p.instrument
        if inst.kind in (InstrumentKind.cash, InstrumentKind.other):
            continue
        my_flags = flags_for(flags, inst.id)
        res = scores.get(inst.id)
        mand = [f for f in my_flags if f.severity == "mandatory"]
        if mand:
            sells = [f for f in mand if f.action == "SELL"]
            if sells:
                action, size = "SELL", p.weight
                reverse = "Only a new thesis with its own kill condition; a mandatory SELL is not reversed by score."
            else:
                action, size = "TRIM", max(f.size_pct or 0 for f in mand)
                reverse = (
                    "Weight back inside the limit through price or new inflows; re-scored daily."
                )
            proceeds += size
        else:
            action, size = "HOLD", None
            reverse = (
                f"A mandatory rule firing (stop {p.position.stop_pct or cfg.stop_loss.get(inst.kind.value) or 'n/a'}, "
                f"limit breach, kill condition) or the score falling below {cfg.score_floor:.0f}."
            )
        kill = kill_block(p.position)
        held_decisions.append((p, inst, action, size, res, my_flags, kill, reverse))

    def _held_decision(inst, action, size, res, my_flags, kill, reverse) -> Decision:
        return Decision(
            date=today,
            instrument_id=inst.id,
            action=action,
            size_pct=size,
            score_id=res.row.id if res else None,
            rules_json={
                "flags": [
                    {"rule": f.rule, "severity": f.severity, "summary": f.summary, **f.detail}
                    for f in my_flags
                ],
                "kill_condition": (kill or {}).get("thesis"),
                "kill_predicate": first_mandatory_predicate(kill),
                "kill_json": kill,
                "score": res.row.total if res else None,
                "band": res.band if res else None,
                "provisional": res.provisional if res else None,
                "basis": view.basis,
                "reference_price": next(
                    (p.price for p in view.positions if p.instrument.id == inst.id), None
                ),
                "reference_date": str(today),
                "global_flags": [g.summary for g in global_flags(flags)],
            },
            reasoning_md=reasoning_markdown(
                action, inst, regime, res, my_flags, size, kill, reverse, basis_note
            ),
            created_at=utcnow(),
        )

    # mandatory SELL / TRIM first
    for _p, inst, action, size, res, my_flags, kill, reverse in held_decisions:
        if action != "HOLD" and not _exists(session, today, inst.id, action):
            new.append(_held_decision(inst, action, size, res, my_flags, kill, reverse))

    # 2. buy side: score >= 75, tradable, no mandatory conflict; sized by cash + limits + 5% cap
    cash_avail = (view.cash_eur / view.total_eur if view.total_eur else 0.0) + proceeds
    candidates = sorted((r for r in results if r.band == "act"), key=lambda r: -r.row.total)
    for res in candidates:
        inst = session.get(Instrument, res.row.instrument_id)
        if not inst.tradable or inst.id in mandatory_ids:
            continue
        if cash_avail <= 0.005:
            out.notes.append(f"{inst.ticker} scores {res.row.total:.0f} but no cash is available")
            break
        pv = held.get(inst.id)
        weight = pv.weight if pv else 0.0
        theme_w = view.by_theme.get(inst.theme or "unassigned", 0.0)
        headroom = [
            limits.max_single_position - weight,
            limits.max_single_theme - theme_w,
            MAX_BUY_PER_DECISION,
            cash_avail,
        ]
        if inst.kind == InstrumentKind.private:
            headroom.append(
                limits.max_illiquid_private
                - sum(
                    p.weight for p in view.positions if p.instrument.kind == InstrumentKind.private
                )
            )
        if inst.kind == InstrumentKind.crypto:
            headroom.append(
                limits.max_crypto
                - sum(
                    p.weight for p in view.positions if p.instrument.kind == InstrumentKind.crypto
                )
            )
        size = min(headroom)
        if size <= 0.005:
            out.notes.append(f"{inst.ticker} scores {res.row.total:.0f} but has no limit headroom")
            continue
        action = "ADD" if pv else "BUY"
        ref_px = (
            pv.price
            if pv and pv.price is not None
            else (
                _latest_price(session, inst.id).close if _latest_price(session, inst.id) else None
            )
        )
        crowd_p = res.factors["crowd"].inputs.get("percentile")
        defer = deferral_reason(session, inst, today, crowd_p)
        if pv is not None:
            blocked = add_blocked(session, pv, view, today)
            if blocked:
                out.notes.append(
                    f"{inst.ticker} scores {res.row.total:.0f} but ADD is blocked while {blocked}"
                )
                continue
        kill = (
            (kill_block(pv.position) if pv else None)
            or condition_for(inst, kills)
            or {
                "thesis": f"Score falls below {cfg.score_floor:.0f} or a mandatory rule fires.",
                "kills": [],
                "add_blocked_while": None,
                "pre_condition": None,
                "theme": inst.theme,
            }
        )
        my_flags = flags_for(flags, inst.id)
        if _exists(session, today, inst.id, action):
            add_ids.add(inst.id)
            cash_avail -= size
            continue
        d = Decision(
            date=today,
            instrument_id=inst.id,
            action=action,
            size_pct=size,
            score_id=res.row.id,
            rules_json={
                "flags": [
                    {"rule": f.rule, "severity": f.severity, "summary": f.summary} for f in my_flags
                ],
                "kill_condition": (kill or {}).get("thesis"),
                "kill_predicate": first_mandatory_predicate(kill),
                "kill_json": kill,
                "score": res.row.total,
                "band": res.band,
                "provisional": res.provisional,
                "basis": view.basis,
                "reference_price": ref_px,
                "reference_date": str(today),
                "crowd_percentile": crowd_p,
                "deferred_reason": defer,
                "headroom": {
                    "position": limits.max_single_position - weight,
                    "theme": limits.max_single_theme - theme_w,
                    "cash": cash_avail,
                    "cap": MAX_BUY_PER_DECISION,
                },
            },
            reasoning_md=reasoning_markdown(
                action,
                inst,
                regime,
                res,
                my_flags,
                size,
                kill,
                f"Score below {cfg.score_floor:.0f}, or the kill condition above becoming true.",
                basis_note,
            ),
            created_at=utcnow(),
        )
        if defer:
            d.user_status, d.user_note = "deferred", defer
            out.deferred += 1
        new.append(d)
        add_ids.add(inst.id)
        cash_avail -= size

    # HOLDs for held positions that got neither a mandatory action nor an ADD
    for _p, inst, action, size, res, my_flags, kill, reverse in held_decisions:
        if (
            action == "HOLD"
            and inst.id not in add_ids
            and not _exists(session, today, inst.id, "HOLD")
        ):
            new.append(_held_decision(inst, action, size, res, my_flags, kill, reverse))

    # 3. AVOID: watchlist instruments (not held) scoring below the watch band
    for res in results:
        if res.row.instrument_id in held or band(res.row.total) != "avoid":
            continue
        inst = session.get(Instrument, res.row.instrument_id)
        if not inst.tradable or _exists(session, today, inst.id, "AVOID"):
            continue
        new.append(
            Decision(
                date=today,
                instrument_id=inst.id,
                action="AVOID",
                size_pct=None,
                score_id=res.row.id,
                rules_json={
                    "flags": [],
                    "score": res.row.total,
                    "band": "avoid",
                    "provisional": res.provisional,
                    "basis": view.basis,
                },
                reasoning_md=reasoning_markdown(
                    "AVOID",
                    inst,
                    regime,
                    res,
                    [],
                    None,
                    None,
                    f"Score above {cfg.score_floor:.0f} with no mandatory rule.",
                    basis_note,
                ),
                created_at=utcnow(),
            )
        )

    for d in new:
        session.add(d)
    session.commit()
    for d in new:
        session.refresh(d)
    out.decisions = new
    out.created = len(new)

    # 4. execution (8b): seed the paper book from the confirmed manual book, then submit mandatory exits now.
    #    BUY/ADD stay pending (or deferred) until the user approves them.
    if view.basis == "confirmed":
        seed_paper_book(session, settings)
    submitted = 0
    for d in new:
        if d.action in ("SELL", "TRIM"):
            try:
                row = submit_decision(session, d, view=view, settings=settings, today=today)
                if row.status == "submitted":
                    submitted += 1
                elif row.error:
                    out.notes.append(f"{d.action} #{d.id}: {row.error}")
            except Exception as exc:  # noqa: BLE001
                log.exception("submit failed for decision %s", d.id)
                out.notes.append(f"execution: {d.action} {d.instrument_id} failed: {exc}")
    out.orders_submitted = submitted
    settle_paper(session, settings)
    try:
        add_narratives(session, new, settings)
    except Exception as exc:  # noqa: BLE001
        log.warning("narratives skipped: %s", exc)
    return out


# --------------------------------------------------------------------------------------- user responses
def respond(
    session: Session,
    decision: Decision,
    status: str,
    note: str | None = None,
    settings: Settings | None = None,
) -> Decision:
    """Record the user's response. Marking executed updates the confirmed positions (never the decision text)."""
    if status not in ("pending", "approved", "executed", "skipped", "overridden", "deferred"):
        raise ValueError("bad status")
    settings = settings or get_settings()
    decision.user_status = status
    decision.user_note = note
    if status == "executed":
        decision.executed_at = utcnow()
        _apply_execution(session, decision, settings)
    session.add(decision)
    session.commit()
    session.refresh(decision)
    if status in ("approved", "executed") and decision.action in ("BUY", "ADD", "SELL", "TRIM"):
        # the approved order goes through the configured broker; paper fills at the next open
        row = submit_decision(session, decision, settings=settings, today=dt.date.today())
        if row.error:
            decision.user_note = ((decision.user_note or "") + f" [order: {row.error}]").strip()
            session.add(decision)
            session.commit()
        settle_paper(session, settings)
    return decision


def _apply_execution(session: Session, decision: Decision, settings: Settings) -> None:
    view = build_portfolio(session, settings)
    inst = session.get(Instrument, decision.instrument_id)
    pv = next((p for p in view.positions if p.instrument.id == inst.id), None)
    open_rows = session.exec(
        select(Position).where(
            Position.instrument_id == inst.id,
            Position.closed_at.is_(None),
            Position.confirmed_by_user.is_(True),
        )
    ).all()
    now = utcnow()
    if decision.action == "SELL":
        for r in open_rows:
            r.closed_at = now
            session.add(r)
        return
    price = pv.price if pv and pv.price is not None else None
    if price is None:
        px = _latest_price(session, inst.id)
        price = px.close if px else None
    if price is None or not view.total_eur:
        raise ValueError(f"cannot size {decision.action} {inst.ticker}: no price")
    ccy = pv.position.currency if pv else inst.currency
    rate = view.fx.get(ccy)
    per_eur = rate.per_eur if rate and rate.per_eur else 1.0
    qty = (decision.size_pct or 0) * view.total_eur * per_eur / price
    if decision.action == "TRIM":
        remaining = qty
        for r in sorted(open_rows, key=lambda r: -r.quantity):
            take = min(r.quantity, remaining)
            r.quantity -= take
            remaining -= take
            if r.quantity <= 1e-9:
                r.closed_at = now
            session.add(r)
        return
    rj = decision.rules_json or {}
    session.add(
        Position(
            instrument_id=inst.id,
            quantity=qty,
            avg_cost=price,
            currency=ccy,
            pot=pv.position.pot if pv else Pot.brokerage,
            as_of=dt.date.today(),
            confirmed_by_user=True,
            last_price=price,
            value_native=qty * price,
            source="decision",
            batch=f"decision:{decision.id}",
            kill_condition=rj.get("kill_condition"),
            kill_predicate=rj.get("kill_predicate"),
            kill_json=rj.get("kill_json"),
        )
    )


def list_decisions(
    session: Session, day: dt.date | None = None, status: str | None = None, limit: int = 200
) -> list[Decision]:
    stmt = select(Decision)
    if day is not None:
        stmt = stmt.where(Decision.date == day)
    if status:
        stmt = stmt.where(Decision.user_status == status)
    return list(
        session.exec(stmt.order_by(Decision.date.desc(), Decision.id.desc()).limit(limit)).all()
    )


def latest_decision_date(session: Session) -> dt.date | None:
    d = session.exec(select(Decision).order_by(Decision.date.desc()).limit(1)).first()
    return d.date if d else None

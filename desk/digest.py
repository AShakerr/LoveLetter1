"""Weekly digest (phase 4): what happened, what is pending, where the paper book diverged, the promotion
checklist and the screener's top names. Markdown first; HTML is derived. Sent by SMTP when configured, otherwise
written to data/digests/."""

from __future__ import annotations

import datetime as dt
import logging
import smtplib
from email.message import EmailMessage
from pathlib import Path

import markdown as md
from sqlmodel import Session, select

from desk.config import Settings, get_settings
from desk.execution import paper_vs_actual
from desk.models import Decision, Instrument, ScreenerRow
from desk.regime import latest_regime
from desk.tape import TAPE, load_tape
from desk.trackrecord import decision_outcomes, hit_rate, promotion_checklist, screener_track

log = logging.getLogger(__name__)


def build_digest(
    session: Session, settings: Settings | None = None, today: dt.date | None = None
) -> tuple[str, str]:
    """Returns (subject, markdown)."""
    settings = settings or get_settings()
    today = today or dt.date.today()
    since = today - dt.timedelta(days=7)
    inst = {i.id: i for i in session.exec(select(Instrument)).all()}
    regime = latest_regime(session)
    lines = [f"# desk weekly digest — {today.isoformat()}", ""]
    if regime:
        lines += [f"**Regime ({regime.date}):** {regime.label}", ""]
    stale = [
        f"{i.spec.label} ({i.age_days}d)"
        for i in load_tape(session, TAPE)
        if i.freshness == "stale" or i.value is None
    ]
    if stale:
        lines += ["**Stale inputs:** " + ", ".join(stale), ""]
    week = session.exec(
        select(Decision)
        .where(Decision.date > since)
        .order_by(Decision.date.desc(), Decision.id.desc())
    ).all()
    lines += ["## Decisions this week", ""]
    if not week:
        lines.append("None.")
    for d in week:
        rj = d.rules_json or {}
        size = f" {d.size_pct:.1%}" if d.size_pct else ""
        lines.append(
            f"- {d.date} **{d.action}** {inst[d.instrument_id].ticker}{size} · score {rj.get('score', 0) or 0:.0f} · {d.user_status}"
        )
    pending = [
        d
        for d in session.exec(
            select(Decision).where(Decision.user_status.in_(["pending", "deferred"]))
        ).all()
    ]
    old = [d for d in pending if (today - d.date).days > 7]
    lines += ["", f"## Pending: {len(pending)} ({len(old)} older than 7 days)", ""]
    for d in sorted(pending, key=lambda d: d.date)[:15]:
        lines.append(f"- {d.date} {d.action} {inst[d.instrument_id].ticker} ({d.user_status})")
    try:
        _a, _p, rows = paper_vs_actual(session, settings)
        gaps = [r for r in rows if abs(r["diff_eur"]) > 1]
        lines += ["", "## Paper vs actual", ""]
        lines.append("No divergence." if not gaps else "")
        for r in gaps:
            lines.append(
                f"- {r['ticker']}: paper €{r['paper_eur']:,.0f} vs actual €{r['actual_eur']:,.0f} ({r['diff_eur']:+,.0f})"
            )
    except Exception as exc:  # noqa: BLE001
        lines += ["", f"Paper vs actual unavailable: {exc}"]
    hr = hit_rate(decision_outcomes(session, today, settings, windows=(30, 90)))
    lines += ["", "## Track record", ""]
    if not hr:
        lines.append("No matured decisions yet.")
    for w, b in sorted(hr.items()):
        lines.append(
            f"- {w}d: {b['hits']}/{b['n']} hits ({b['rate']:.0%}), avg excess {b['avg_excess'] * 100:+.1f}pp"
        )
    st = screener_track(session, today)["summary"]
    for w, b in st.items():
        if b["n"]:
            lines.append(
                f"- screener top-15 {w}d: {b['hits']}/{b['n']} beat the benchmark, avg excess {b['avg_excess'] * 100:+.1f}pp"
            )
    lines += ["", "## Promotion checklist", ""]
    for c in promotion_checklist(session, today, settings):
        lines.append(f"- {'PASS' if c.passed else 'FAIL'} — {c.name}: {c.value}")
    latest = session.exec(select(ScreenerRow).order_by(ScreenerRow.date.desc()).limit(1)).first()
    if latest:
        top = session.exec(
            select(ScreenerRow)
            .where(ScreenerRow.date == latest.date)
            .order_by(ScreenerRow.rank)
            .limit(5)
        ).all()
        lines += ["", f"## Screener top 5 ({latest.date})", ""]
        for r in top:
            g = r.gates_json or {}
            lines.append(
                f"- #{r.rank} {inst[r.instrument_id].ticker} {r.total:.0f}{'' if g.get('passed') else ' (gated)'}"
            )
    lines += [
        "",
        "_Decisions are logged before anything is executed. Nothing here is investment advice._",
    ]
    return f"desk digest {today.isoformat()}", "\n".join(lines)


def send_digest(subject: str, body_md: str, settings: Settings | None = None) -> str:
    """SMTP when DESK_SMTP_HOST and DESK_DIGEST_TO are set; otherwise a file under data/digests/."""
    settings = settings or get_settings()
    if settings.smtp_host and settings.digest_to:
        msg = EmailMessage()
        msg["Subject"], msg["From"], msg["To"] = (
            subject,
            settings.digest_from or settings.smtp_user or "desk@localhost",
            settings.digest_to,
        )
        msg.set_content(body_md)
        msg.add_alternative(md.markdown(body_md), subtype="html")
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30) as smtp:
            if settings.smtp_starttls:
                smtp.starttls()
            if settings.smtp_user:
                smtp.login(settings.smtp_user, settings.smtp_pass or "")
            smtp.send_message(msg)
        return f"sent to {settings.digest_to}"
    out = Path(settings.data_dir) / "digests"
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{subject.split()[-1]}.md"
    path.write_text(body_md, encoding="utf-8")
    return f"SMTP not configured; written to {path}"


def run_digest(settings: Settings | None = None, send: bool = True) -> dict:
    from desk.db import init_db, session_scope

    settings = settings or get_settings()
    init_db(settings)
    with session_scope(settings) as session:
        subject, body = build_digest(session, settings)
    result = send_digest(subject, body, settings) if send else "not sent"
    return {"subject": subject, "result": result, "chars": len(body)}

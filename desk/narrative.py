"""Optional Claude-written reasoning (phase 4). The template reasoning is always stored; this adds a readable
paragraph in Decision.narrative_md when DESK_LLM_REASONING=1 and an API key exists. Never blocks the pipeline."""

from __future__ import annotations

import logging

from desk.config import Settings, get_settings
from desk.llm import TextCompleter, get_completer
from desk.models import Decision

log = logging.getLogger(__name__)

SYSTEM = (
    "You write one plain paragraph (max 120 words) for a private investment decision journal. Restate the decision's "
    "argument from the markdown you are given: the regime, the strongest factors, any rule that fired, the kill condition, "
    "and what would reverse it. No new facts, no advice, no hedging boilerplate. Plain prose, no headings, no bullets."
)


def write_narrative(decision: Decision, completer: TextCompleter) -> str:
    return completer.complete(
        SYSTEM, [{"type": "text", "text": decision.reasoning_md}], max_tokens=400
    ).strip()


def add_narratives(
    session,
    decisions: list[Decision],
    settings: Settings | None = None,
    completer: TextCompleter | None = None,
) -> int:
    settings = settings or get_settings()
    if not settings.llm_reasoning:
        return 0
    try:
        completer = completer or get_completer(settings)
    except RuntimeError as exc:
        log.warning("narrative skipped: %s", exc)
        return 0
    n = 0
    for d in decisions:
        if d.narrative_md:
            continue
        try:
            d.narrative_md = write_narrative(d, completer)
            session.add(d)
            n += 1
        except Exception as exc:  # noqa: BLE001
            log.warning("narrative for decision %s failed: %s", d.id, exc)
    session.commit()
    return n

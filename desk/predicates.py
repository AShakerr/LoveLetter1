"""The kill-condition predicate DSL (docs/BRIEF.md section 8, amended).

A predicate is a small Python-syntax expression evaluated over a whitelisted AST. Functions available:

    house_view(scope, key)   -> object with .stance / .value / .date, e.g. house_view('sector', 'Materials');
    house_view(key)             the one-argument form takes a literal key or one of the position's own
                                attributes: sector, region, theme
    observation(series)      -> latest value of an observation series (DGS30, ECB_DEPO, DXY ...); falls back
                                to the latest close of an instrument with that ticker (BZ=F, GC=F ...)
    close(ticker=None)       -> latest close (default: the position's instrument)
    change_pct(ticker, days) -> % change of the close over N trading days (observation series: N rows)
    theme_weight(theme)      -> current portfolio weight of a theme, in percent (35 means 35%)
    days_since(series)       -> age in days of the latest observation/close of a series or ticker;
                                a 'YYYY-MM-DD' literal gives days since that date
    avg_cost(ticker=None)    -> the position's average cost
    sentiment(ticker=None, days=14) -> mean news sentiment over the window

Bare names that are not functions or context variables are treated as string literals, so
`house_view(sector).stance == least_preferred` reads naturally. Anything the DSL cannot evaluate raises
PredicateError; the rules engine turns that into a REVIEW flag showing the thesis text - never a silent drop.
"""

from __future__ import annotations

import ast
import datetime as dt
import operator
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from sqlmodel import Session, select

from desk.houseviews import all_views
from desk.models import Instrument, NewsSentiment, Observation, Position, Price


class PredicateError(Exception):
    pass


@dataclass
class ViewResult:
    stance: str | None
    value: float | str | None
    date: dt.date | None
    key: str

    def __bool__(self) -> bool:
        return self.stance is not None or self.value is not None


def _num(v: str | None) -> float | str | None:
    if v is None:
        return None
    t = v.replace("'", "").replace(",", "").replace("%", "").strip()
    try:
        return float(t)
    except ValueError:
        return v


class Context:
    """Data access for one evaluation. Everything is looked up lazily from the session."""

    def __init__(
        self,
        session: Session,
        instrument: Instrument | None = None,
        position: Position | None = None,
        theme_weights: dict[str, float] | None = None,
        today: dt.date | None = None,
    ) -> None:
        self.session, self.instrument, self.position = session, instrument, position
        self.theme_weights = theme_weights or {}
        self.today = today or dt.date.today()

    # -- helpers --------------------------------------------------------------------------
    def _instrument(self, ticker: str | None) -> Instrument:
        if ticker is None:
            if self.instrument is None:
                raise PredicateError("no instrument in context; pass a ticker")
            return self.instrument
        inst = self.session.exec(select(Instrument).where(Instrument.ticker == ticker)).first()
        if inst is None:
            raise PredicateError(f"unknown instrument {ticker!r}")
        return inst

    def _latest_price(self, ticker: str | None) -> Price:
        inst = self._instrument(ticker)
        px = self.session.exec(
            select(Price).where(Price.instrument_id == inst.id).order_by(Price.date.desc()).limit(1)
        ).first()
        if px is None:
            if (
                self.position is not None
                and self.position.instrument_id == inst.id
                and self.position.last_price
            ):
                return Price(
                    instrument_id=inst.id,
                    date=self.position.as_of,
                    close=self.position.last_price,
                    source=self.position.source,
                    fetched_at=dt.datetime.now(),
                )
            raise PredicateError(f"no price for {inst.ticker}")
        return px

    def _price_at(self, ticker: str | None, on: dt.date) -> Price | None:
        inst = self._instrument(ticker)
        return self.session.exec(
            select(Price)
            .where(Price.instrument_id == inst.id, Price.date <= on)
            .order_by(Price.date.desc())
            .limit(1)
        ).first()

    # -- DSL functions ------------------------------------------------------------------------
    def house_view(self, scope_or_key: str, key: str | None = None) -> ViewResult:
        """house_view('sector', 'Materials') or house_view('Materials') or house_view(sector)."""
        scope: str | None = None
        if key is not None:
            scope, key = scope_or_key, key
        else:
            key = scope_or_key
        attr = None
        if scope is None:
            attr = {
                "sector": "sector",
                "region": "region",
                "theme": "theme",
                "ticker": "ticker",
            }.get(key)
        if attr:
            if self.instrument is None:
                raise PredicateError(f"house_view({key}) needs an instrument in context")
            key = getattr(self.instrument, attr)
            if not key:
                raise PredicateError(f"instrument has no {attr}")
        for row in all_views(self.session):
            if row.tactical:
                continue
            if row.view.key == key and (scope is None or row.view.scope == scope):
                return ViewResult(row.view.stance, _num(row.view.value), row.report.date, key)
        raise PredicateError(
            f"no house view for {key!r}" + (f" in scope {scope!r}" if scope else "")
        )

    def observation(self, series: str) -> float:
        obs = self.session.exec(
            select(Observation)
            .where(Observation.series == series)
            .order_by(Observation.date.desc(), Observation.fetched_at.desc())
            .limit(1)
        ).first()
        if obs is not None:
            return obs.value
        inst = self.session.exec(select(Instrument).where(Instrument.ticker == series)).first()
        if inst is not None:
            return self._latest_price(series).close
        raise PredicateError(f"no observation or price series {series!r}")

    def close(self, ticker: str | None = None) -> float:
        return self._latest_price(ticker).close

    def change_pct(self, series: str, days: float) -> float:
        """% change over N trading days: the latest close versus the close N rows earlier."""
        n = int(days)
        inst = self.session.exec(select(Instrument).where(Instrument.ticker == series)).first()
        if inst is not None:
            rows = self.session.exec(
                select(Price)
                .where(Price.instrument_id == inst.id)
                .order_by(Price.date.desc())
                .limit(n + 1)
            ).all()
            if len(rows) < n + 1 or not rows[-1].close:
                raise PredicateError(
                    f"not enough price history for {series} ({len(rows)} rows, need {n + 1})"
                )
            return (rows[0].close / rows[-1].close - 1) * 100
        rows = self.session.exec(
            select(Observation)
            .where(Observation.series == series)
            .order_by(Observation.date.desc())
            .limit(n + 1)
        ).all()
        if not rows:
            raise PredicateError(f"no series {series!r}")
        if len(rows) < n + 1 or not rows[-1].value:
            raise PredicateError(
                f"not enough history for {series} ({len(rows)} rows, need {n + 1})"
            )
        return (rows[0].value / rows[-1].value - 1) * 100

    def theme_weight(self, theme: str) -> float:
        return float(self.theme_weights.get(theme, 0.0)) * 100

    def days_since(self, series: str) -> int:
        try:
            return (self.today - dt.date.fromisoformat(str(series))).days
        except ValueError:
            pass
        obs = self.session.exec(
            select(Observation)
            .where(Observation.series == series)
            .order_by(Observation.date.desc())
            .limit(1)
        ).first()
        if obs is not None:
            return (self.today - obs.date).days
        inst = self.session.exec(select(Instrument).where(Instrument.ticker == series)).first()
        if inst is not None:
            return (self.today - self._latest_price(series).date).days
        raise PredicateError(f"no series, ticker or date {series!r}")

    def avg_cost(self, ticker: str | None = None) -> float:
        if ticker is None or (self.instrument is not None and ticker == self.instrument.ticker):
            if self.position is None:
                raise PredicateError("avg_cost needs a position in context")
            return self.position.avg_cost
        inst = self._instrument(ticker)
        pos = self.session.exec(
            select(Position).where(
                Position.instrument_id == inst.id,
                Position.closed_at.is_(None),
                Position.confirmed_by_user.is_(True),
            )
        ).first()
        if pos is None:
            raise PredicateError(f"no open position in {ticker}")
        return pos.avg_cost

    def sentiment(self, ticker: str | None = None, days: float = 14) -> float:
        inst = self._instrument(ticker)
        since = self.today - dt.timedelta(days=int(days))
        rows = self.session.exec(
            select(NewsSentiment).where(
                NewsSentiment.instrument_id == inst.id, NewsSentiment.date >= since
            )
        ).all()
        if not rows:
            raise PredicateError(f"no sentiment for {inst.ticker} in the last {int(days)} days")
        return sum(r.score for r in rows) / len(rows)

    def functions(self) -> dict[str, Callable[..., Any]]:
        return {
            "house_view": self.house_view,
            "observation": self.observation,
            "close": self.close,
            "change_pct": self.change_pct,
            "theme_weight": self.theme_weight,
            "days_since": self.days_since,
            "avg_cost": self.avg_cost,
            "sentiment": self.sentiment,
            "abs": abs,
            "min": min,
            "max": max,
        }


_CMP = {
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
    ast.In: lambda a, b: a in b,
    ast.NotIn: lambda a, b: a not in b,
}
_BIN = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
}


class _Evaluator(ast.NodeVisitor):
    def __init__(self, ctx: Context) -> None:
        self.funcs = ctx.functions()
        self.consts = {
            "true": True,
            "false": False,
            "none": None,
            "True": True,
            "False": False,
            "None": None,
        }

    def visit(self, node: ast.AST) -> Any:  # noqa: D102
        method = getattr(self, f"visit_{type(node).__name__}", None)
        if method is None:
            raise PredicateError(f"unsupported syntax: {type(node).__name__}")
        return method(node)

    def visit_Expression(self, node: ast.Expression) -> Any:
        return self.visit(node.body)

    def visit_Constant(self, node: ast.Constant) -> Any:
        return node.value

    def visit_Name(self, node: ast.Name) -> Any:
        if node.id in self.consts:
            return self.consts[node.id]
        if node.id in self.funcs:
            return self.funcs[node.id]
        return node.id  # bare name -> string literal (least_preferred, sector, ...)

    def visit_Attribute(self, node: ast.Attribute) -> Any:
        obj = self.visit(node.value)
        if not isinstance(obj, ViewResult) or node.attr.startswith("_"):
            raise PredicateError(f"attribute .{node.attr} not available on {type(obj).__name__}")
        return getattr(obj, node.attr)

    def visit_Call(self, node: ast.Call) -> Any:
        if not isinstance(node.func, ast.Name) or node.func.id not in self.funcs:
            raise PredicateError("only the DSL functions may be called")
        fn = self.funcs[node.func.id]
        args = [self.visit(a) for a in node.args]
        kwargs = {k.arg: self.visit(k.value) for k in node.keywords if k.arg}
        try:
            return fn(*args, **kwargs)
        except PredicateError:
            raise
        except TypeError as exc:
            raise PredicateError(f"{node.func.id}: {exc}") from exc

    def visit_Compare(self, node: ast.Compare) -> bool:
        left = self.visit(node.left)
        for op, comp in zip(node.ops, node.comparators, strict=True):
            right = self.visit(comp)
            fn = _CMP.get(type(op))
            if fn is None:
                raise PredicateError(f"unsupported comparison {type(op).__name__}")
            if left is None or right is None:
                raise PredicateError("comparison against missing value")
            try:
                ok = fn(left, right)
            except TypeError as exc:
                raise PredicateError(f"cannot compare {left!r} with {right!r}") from exc
            if not ok:
                return False
            left = right
        return True

    def visit_BoolOp(self, node: ast.BoolOp) -> bool:
        vals = [self.visit(v) for v in node.values]
        return all(vals) if isinstance(node.op, ast.And) else any(vals)

    def visit_UnaryOp(self, node: ast.UnaryOp) -> Any:
        v = self.visit(node.operand)
        if isinstance(node.op, ast.Not):
            return not v
        if isinstance(node.op, ast.USub):
            return -v
        raise PredicateError("unsupported unary operator")

    def visit_BinOp(self, node: ast.BinOp) -> Any:
        fn = _BIN.get(type(node.op))
        if fn is None:
            raise PredicateError("unsupported operator")
        a, b = self.visit(node.left), self.visit(node.right)
        try:
            return fn(a, b)
        except (TypeError, ZeroDivisionError) as exc:
            raise PredicateError(str(exc)) from exc


def evaluate(predicate: str, ctx: Context) -> bool:
    """Return the predicate's truth value; raise PredicateError when it cannot be evaluated."""
    try:
        tree = ast.parse(predicate.strip(), mode="eval")
    except SyntaxError as exc:
        raise PredicateError(f"syntax error: {exc.msg}") from exc
    result = _Evaluator(ctx).visit(tree)
    if not isinstance(result, bool):
        raise PredicateError(f"predicate did not produce a boolean (got {result!r})")
    return result

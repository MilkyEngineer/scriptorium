"""OCR spend accounting and the monthly caps (Architecture §8.1, §6.2 ``ocr_spend``).

One ``ocr_spend`` row per page transcribed through a key, in ``scriptorium.sqlite``. Caps are
sums over the UTC calendar month of ``at``:

- ``OCR_MONTHLY_CAP_USD``: every user's ``server``-mode spend together (the operator's bill);
- ``OCR_USER_MONTHLY_CAP_USD``: one user's ``server``-mode spend;
- ``users.ocr_cap_usd``: one user's ``user``-mode spend, a cap they chose for their own key.

A cap has room while its remainder is above zero, so a cap of 0 stops that spend entirely.
The check happens before a call and the row is written after it, so concurrent calls can
overshoot a cap by at most one page each: the worker bounds OCR concurrency (§7).

Rows hold counts and money only, never content; they stay when a transcript is replaced or a
key is cleared. A Message Batches item's row also names the batch and the item's custom id,
unique per user, so reading a batch's results twice never bills an item twice.
"""

from __future__ import annotations

import math
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Final, Literal

from scriptorium.db import migrations
from scriptorium.db.connect import immediate, open_sqlite, to_db_time
from scriptorium.db.schema_global import SCRIPTORIUM_STEPS
from scriptorium.errors import InvalidArgumentError
from scriptorium.users.store import UID_RE

if TYPE_CHECKING:
    from scriptorium.config import Settings
    from scriptorium.ocr.modes import Mode
    from scriptorium.users.store import GlobalStore

__all__ = ["BATCH_DISCOUNT", "Allowance", "Degraded", "Spend", "SpendLedger", "cost_usd"]

#: The Message Batches API bills every token at half the list price.
BATCH_DISCOUNT: Final = 0.5
_MODEL_RE: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@-]{0,99}")
_BATCH_ID_RE: Final = re.compile(r"[A-Za-z0-9_-]{1,128}")
_CUSTOM_ID_RE: Final = re.compile(r"[A-Za-z0-9_-]{1,64}")
_KEY_MODES: Final = ("user", "server")

Degraded = Literal[False, "global", "user", "own"]


@dataclass(frozen=True)
class Spend:
    """Spend over one UTC calendar month: USD, pages (rows) and tokens."""

    cost_usd: float
    pages: int
    input_tokens: int
    output_tokens: int


@dataclass(frozen=True)
class Allowance:
    """What is left this month, and which cap (if any) stops the next page.

    ``own_remaining_usd`` is None when the user set no cap on their own key. ``degraded`` is
    False while every cap that applies has room, else the first one that does not:
    ``"global"`` (``OCR_MONTHLY_CAP_USD``), ``"user"`` (``OCR_USER_MONTHLY_CAP_USD``) or
    ``"own"`` (the user's cap on their own key).
    """

    global_remaining_usd: float
    user_remaining_usd: float
    own_remaining_usd: float | None
    degraded: Degraded


def _check_tokens(*counts: int) -> None:
    for n in counts:
        if isinstance(n, bool) or not isinstance(n, int) or n < 0:
            raise InvalidArgumentError("token counts must be non-negative integers")


def cost_usd(
    settings: Settings, input_tokens: int, output_tokens: int, *, batch: bool = False
) -> float:
    """USD for one call at the configured prices; ``output_tokens`` includes thinking tokens.

    ``batch=True`` applies the Message Batches discount (half price).
    """
    _check_tokens(input_tokens, output_tokens)
    cost = (
        input_tokens * settings.ocr_price_in_per_mtok
        + output_tokens * settings.ocr_price_out_per_mtok
    ) / 1_000_000
    return cost * BATCH_DISCOUNT if batch else cost


def month_bounds(at: datetime) -> tuple[datetime, datetime]:
    """Start (inclusive) and end (exclusive) of the UTC calendar month containing ``at``."""
    if at.tzinfo is None:
        raise InvalidArgumentError("timestamps must carry a time zone")
    utc = at.astimezone(UTC)
    start = utc.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if start.month == 12:
        return start, start.replace(year=start.year + 1, month=1)
    return start, start.replace(month=start.month + 1)


def _remaining(cap: float, spent: float) -> float:
    return max(0.0, cap - spent)


class SpendLedger:
    """The ``ocr_spend`` table of ``scriptorium.sqlite``.

    Like :class:`~scriptorium.users.store.GlobalStore`, every method opens a short-lived
    connection, so one instance may be shared by threads; the server migrates, the worker
    opens with ``migrate=False``.
    """

    def __init__(self, path: Path) -> None:
        self._path = Path(path)

    @classmethod
    def open(cls, path: Path, *, migrate: bool) -> SpendLedger:
        """Open the global database; ``migrate=True`` (the server only) applies migrations."""
        ledger = cls(path)
        if not migrate and not ledger._path.is_file():
            raise migrations.SchemaMismatch(0, len(SCRIPTORIUM_STEPS))
        with ledger._connect() as conn:
            if migrate:
                migrations.migrate(conn, SCRIPTORIUM_STEPS)
            else:
                migrations.check_version(conn, SCRIPTORIUM_STEPS)
        return ledger

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = open_sqlite(self._path)
        try:
            yield conn
        finally:
            conn.close()

    def record(
        self,
        *,
        uid: str,
        mode: Mode,
        model: str,
        input_tokens: int,
        output_tokens: int,
        cost_usd: float,
        at: datetime,
        batch_id: str | None = None,
        custom_id: str | None = None,
    ) -> bool:
        """Append one page's spend. ``mode`` is ``user`` or ``server``: lazy mode costs nothing.

        A batch item names its ``batch_id`` and ``custom_id`` (both or neither): it is recorded
        once, so collecting a batch's results again never bills an item twice. Returns False
        when that item was recorded already, True when a row was added.
        """
        if not isinstance(uid, str) or not UID_RE.fullmatch(uid):
            raise InvalidArgumentError("user id is malformed")
        if mode not in _KEY_MODES:
            raise InvalidArgumentError("only user- and server-mode calls are recorded")
        if not isinstance(model, str) or not _MODEL_RE.fullmatch(model):
            raise InvalidArgumentError("model is not a model id")
        _check_tokens(input_tokens, output_tokens)
        if (
            isinstance(cost_usd, bool)
            or not isinstance(cost_usd, int | float)
            or not math.isfinite(cost_usd)
            or cost_usd < 0
        ):
            raise InvalidArgumentError("cost must be a non-negative number of USD")
        if (batch_id is None) != (custom_id is None):
            raise InvalidArgumentError("a batch item names its batch and its custom id")
        if batch_id is not None and not (
            isinstance(batch_id, str)
            and _BATCH_ID_RE.fullmatch(batch_id)
            and isinstance(custom_id, str)
            and _CUSTOM_ID_RE.fullmatch(custom_id)
        ):
            raise InvalidArgumentError("batch or custom id is malformed")
        with self._connect() as conn, immediate(conn):
            cur = conn.execute(
                "INSERT INTO ocr_spend (uid, mode, model, input_tokens, output_tokens, cost_usd, "
                "at, batch_id, custom_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (uid, batch_id, custom_id) WHERE batch_id IS NOT NULL DO NOTHING",
                (
                    uid,
                    mode,
                    model,
                    input_tokens,
                    output_tokens,
                    float(cost_usd),
                    to_db_time(at),
                    batch_id,
                    custom_id,
                ),
            )
            return cur.rowcount == 1

    def month(self, *, uid: str | None, mode: Mode | None, at: datetime) -> Spend:
        """Spend in the UTC calendar month of ``at``; ``uid=None`` is everyone, ``mode=None``
        both key modes."""
        start, end = month_bounds(at)
        sql = (
            "SELECT COALESCE(SUM(cost_usd), 0.0), COUNT(*), COALESCE(SUM(input_tokens), 0), "
            "COALESCE(SUM(output_tokens), 0) FROM ocr_spend WHERE at >= ? AND at < ?"
        )
        params: list[object] = [to_db_time(start), to_db_time(end)]
        if uid is not None:
            if not isinstance(uid, str) or not UID_RE.fullmatch(uid):
                raise InvalidArgumentError("user id is malformed")
            sql += " AND uid = ?"
            params.append(uid)
        if mode is not None:
            if mode not in _KEY_MODES:
                raise InvalidArgumentError("only user- and server-mode spend is recorded")
            sql += " AND mode = ?"
            params.append(mode)
        with self._connect() as conn:
            row = conn.execute(sql, params).fetchone()
        return Spend(
            cost_usd=float(row[0]),
            pages=int(row[1]),
            input_tokens=int(row[2]),
            output_tokens=int(row[3]),
        )

    def allowance(
        self,
        settings: Settings,
        store: GlobalStore,
        uid: str,
        at: datetime,
        *,
        mode: Mode | None = None,
    ) -> Allowance:
        """What the user may still spend this month, and whether a cap stops the next page.

        ``mode="server"`` checks the global cap, then the per-user cap; ``mode="user"``
        checks the user's own cap; the default checks all three (the conservative answer).
        Callers that know the resolved mode pass it, so a user on their own key is never
        stopped by the server's caps.
        """
        server = self.month(uid=uid, mode="server", at=at).cost_usd
        global_remaining = _remaining(
            settings.ocr_monthly_cap_usd, self.month(uid=None, mode="server", at=at).cost_usd
        )
        user_remaining = _remaining(settings.ocr_user_monthly_cap_usd, server)
        row = store.user(uid)
        own_cap = row.ocr_cap_usd if row is not None else None
        own_remaining = (
            None
            if own_cap is None
            else _remaining(own_cap, self.month(uid=uid, mode="user", at=at).cost_usd)
        )
        degraded: Degraded = False
        checks: list[tuple[Degraded, float | None]] = []
        if mode in (None, "server"):
            checks += [("global", global_remaining), ("user", user_remaining)]
        if mode in (None, "user"):
            checks.append(("own", own_remaining))
        for name, remaining in checks:
            if remaining is not None and remaining <= 0:
                degraded = name
                break
        return Allowance(global_remaining, user_remaining, own_remaining, degraded)

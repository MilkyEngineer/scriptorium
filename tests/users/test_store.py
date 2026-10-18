from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from scriptorium.auth.oidc import User, uid_hash_for
from scriptorium.db.migrations import GLOBAL_STEPS, SchemaMismatch
from scriptorium.errors import (
    InvalidArgumentError,
    NotFoundError,
    PathEscapeError,
    RefusedError,
)
from scriptorium.users.store import (
    ACCT_RE,
    AccountPaths,
    GlobalStore,
    account_paths,
    global_db_path,
    new_ulid,
    uid_for,
    user_dir,
)

ISS = "https://auth.example.com/realms/scriptorium"
UID = "0123456789abcdef"
ACCT = "01J9Z8Y7X6W5V4T3S2R1Q0P0N0"
DOC = "00000000-0000-4000-8000-000000000001"


def make_user(sub: str = "sub-1", name: str = "reader", uid_hash: str | None = None) -> User:
    return User(
        iss=ISS,
        sub=sub,
        uid_hash=uid_hash or uid_hash_for(ISS, sub),
        display_name=name,
        email=None,
        email_verified=False,
        is_admin=False,
        scopes=frozenset(),
    )


# ---- paths ----------------------------------------------------------------------------------


def test_account_paths_layout(tmp_path: Path) -> None:
    p = account_paths(tmp_path, UID, ACCT)
    root = tmp_path / "users" / UID / "accounts" / ACCT
    assert p == AccountPaths(
        root=root,
        blobs=root / "blobs",
        library=root / "library",
        tombstones=root / "tombstones",
        pages=root / "pages",
        staging=root / "staging",
        index=root / "index.sqlite",
    )
    assert user_dir(tmp_path, UID) == tmp_path / "users" / UID
    assert global_db_path(tmp_path) == tmp_path / "scriptorium.sqlite"
    assert not root.exists()  # deriving a path creates nothing


def test_paths_are_absolute_even_from_a_relative_data_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    assert account_paths(Path("data"), UID, ACCT).root.is_absolute()


@pytest.mark.parametrize(
    "uid",
    [
        "",
        UID.upper().replace("0", "A"),
        "0123456789ABCDEF",
        UID[:-1],
        UID + "0",
        UID + "\n",
        UID[:-1] + "\x00",
        "../../../../etc",
        "0123456789abcdeg",
        "..",
        "/" + UID[1:],
    ],
)
def test_bad_uid_is_refused(tmp_path: Path, uid: str) -> None:
    with pytest.raises(InvalidArgumentError):
        user_dir(tmp_path, uid)
    with pytest.raises(InvalidArgumentError):
        account_paths(tmp_path, uid, ACCT)


@pytest.mark.parametrize(
    "acct",
    [
        "",
        ACCT.lower(),
        ACCT[:-1],
        ACCT + "0",
        ACCT + "\n",
        ACCT[:-1] + "I",  # Crockford excludes I, L, O, U
        ACCT[:-1] + "L",
        ACCT[:-1] + "O",
        ACCT[:-1] + "U",
        "../" + ACCT[3:],
        ACCT[:-1] + "\x00",
    ],
)
def test_bad_acct_is_refused(tmp_path: Path, acct: str) -> None:
    with pytest.raises(InvalidArgumentError):
        account_paths(tmp_path, UID, acct)


def test_symlinked_user_directory_is_refused(tmp_path: Path) -> None:
    (tmp_path / "data" / "users").mkdir(parents=True)
    (tmp_path / "elsewhere").mkdir()
    (tmp_path / "data" / "users" / UID).symlink_to(tmp_path / "elsewhere")
    with pytest.raises(PathEscapeError):
        user_dir(tmp_path / "data", UID)
    with pytest.raises(PathEscapeError):
        account_paths(tmp_path / "data", UID, ACCT)


def test_symlinked_account_directory_is_refused(tmp_path: Path) -> None:
    accounts = tmp_path / "data" / "users" / UID / "accounts"
    accounts.mkdir(parents=True)
    (tmp_path / "elsewhere").mkdir()
    (accounts / ACCT).symlink_to(tmp_path / "elsewhere")
    with pytest.raises(PathEscapeError):
        account_paths(tmp_path / "data", UID, ACCT)


def test_inside(tmp_path: Path) -> None:
    p = account_paths(tmp_path, UID, ACCT)
    p.library.mkdir(parents=True)
    real_root = Path(os.path.realpath(p.root))
    assert p.inside(p.library / f"{DOC}.content") == real_root / "library" / f"{DOC}.content"
    assert p.inside(p.root) == real_root
    for escape in (
        p.root / "..",
        p.library / ".." / ".." / ACCT.replace("0", "1"),
        p.root.parent / (ACCT + "X"),  # sibling sharing a prefix
        Path("library"),  # relative
        Path(str(p.library) + "\x00"),
    ):
        with pytest.raises(PathEscapeError):
            p.inside(escape)
    (tmp_path / "outside").mkdir()
    (p.library / DOC).symlink_to(tmp_path / "outside")
    with pytest.raises(PathEscapeError):
        p.inside(p.library / DOC / "x.rm")
    (p.library / "loop").symlink_to(p.blobs)  # inside to inside is fine
    assert p.inside(p.library / "loop") == real_root / "blobs"


def test_new_ulid() -> None:
    values = {new_ulid() for _ in range(100)}
    assert len(values) == 100
    assert all(ACCT_RE.fullmatch(v) for v in values)
    assert new_ulid(1_000) < new_ulid(2_000)  # sortable by time
    assert new_ulid(0).startswith("0000000000")
    with pytest.raises(ValueError):
        new_ulid(1 << 48)


def test_uid_for() -> None:
    h = uid_hash_for(ISS, "x")
    assert uid_for(h) == h[:16]
    for bad in ("", h[:16], h.upper(), h + "0", h[:-1] + "\n"):
        with pytest.raises(InvalidArgumentError):
            uid_for(bad)


# ---- global store ---------------------------------------------------------------------------


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        self.now += timedelta(seconds=1)
        return self.now


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return global_db_path(tmp_path)


@pytest.fixture
def gs(db: Path) -> Iterator[GlobalStore]:
    yield GlobalStore.open(db, migrate=True, clock=Clock())


def test_open_migrates_and_sets_pragmas(db: Path, gs: GlobalStore) -> None:
    conn = sqlite3.connect(db)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == len(GLOBAL_STEPS)
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"users", "accounts", "audit"} <= tables
    conn.close()
    GlobalStore.open(db, migrate=False)  # current: the worker may open it


def test_open_without_migrate_refuses_an_old_schema(db: Path) -> None:
    with pytest.raises(SchemaMismatch):
        GlobalStore.open(db, migrate=False)


def test_ensure_user(gs: GlobalStore) -> None:
    user = make_user()
    row = gs.ensure_user(user)
    assert (row.uid, row.iss, row.sub, row.display_name) == (
        user.uid_hash[:16],
        ISS,
        "sub-1",
        "reader",
    )
    assert row.disabled_at is None and row.quota_gb is None and not row.server_key_allowed
    again = gs.ensure_user(make_user(name="renamed"))
    assert again.display_name == "renamed"
    assert again.created_at == row.created_at
    assert gs.user(row.uid) == again
    assert gs.user("f" * 16) is None


def test_ensure_user_refuses_a_uid_prefix_collision(gs: GlobalStore) -> None:
    first = make_user("a")
    gs.ensure_user(first)
    impostor = make_user("b", uid_hash=first.uid_hash[:16] + "0" * 48)
    with pytest.raises(RefusedError):
        gs.ensure_user(impostor)


def test_link_account_lifecycle(gs: GlobalStore) -> None:
    uid = gs.ensure_user(make_user()).uid
    a = gs.link_account(uid, "rm-identity-1", "Scriptorium (test)")
    assert ACCT_RE.fullmatch(a.acct) and a.active and a.uid == uid
    assert a.rm_identity == "rm-identity-1"

    with pytest.raises(RefusedError):  # already linked: replace means deactivate first
        gs.link_account(uid, "rm-identity-2", "Scriptorium (test)")

    same = gs.link_account(uid, "rm-identity-1", "Scriptorium (relinked)")  # e.g. after relink
    assert same.acct == a.acct and same.device_desc == "Scriptorium (relinked)"

    gs.deactivate_account(uid, a.acct)
    b = gs.link_account(uid, "rm-identity-2", "Scriptorium (test)")
    assert b.acct != a.acct
    listed = gs.accounts(uid)
    assert [r.acct for r in listed] == [a.acct, b.acct]
    assert [r.active for r in listed] == [False, True]

    gs.deactivate_account(uid, b.acct)
    back = gs.link_account(uid, "rm-identity-1", "Scriptorium (back)")
    assert back.acct == a.acct and back.active  # one directory per reMarkable account


def test_link_without_identity_always_mints_a_new_account(gs: GlobalStore) -> None:
    uid = gs.ensure_user(make_user()).uid
    a = gs.link_account(uid, None, "d")
    gs.deactivate_account(uid, a.acct)
    b = gs.link_account(uid, None, "d")
    assert a.acct != b.acct


def test_link_refuses_an_identity_active_for_another_user(gs: GlobalStore) -> None:
    alice = gs.ensure_user(make_user("alice")).uid
    bob = gs.ensure_user(make_user("bob")).uid
    a = gs.link_account(alice, "shared-rm", "d")
    with pytest.raises(RefusedError):
        gs.link_account(bob, "shared-rm", "d")
    gs.deactivate_account(alice, a.acct)
    assert gs.link_account(bob, "shared-rm", "d").uid == bob


def test_link_validates_input(gs: GlobalStore) -> None:
    uid = gs.ensure_user(make_user()).uid
    with pytest.raises(NotFoundError):
        gs.link_account("f" * 16, None, "d")
    for desc in ("x" * 129, "tab\there", "line\nbreak"):
        with pytest.raises(InvalidArgumentError):
            gs.link_account(uid, None, desc)
    for identity in ("", "x" * 257, "a\x00b"):
        with pytest.raises(InvalidArgumentError):
            gs.link_account(uid, identity, "d")
    with pytest.raises(InvalidArgumentError):
        gs.link_account("../x", None, "d")


def test_deactivate_is_scoped_to_the_user(gs: GlobalStore) -> None:
    alice = gs.ensure_user(make_user("alice")).uid
    bob = gs.ensure_user(make_user("bob")).uid
    a = gs.link_account(alice, None, "d")
    with pytest.raises(NotFoundError):
        gs.deactivate_account(bob, a.acct)
    with pytest.raises(NotFoundError):
        gs.deactivate_account(alice, new_ulid())
    with pytest.raises(InvalidArgumentError):
        gs.deactivate_account(alice, "not-an-acct")
    assert gs.accounts(alice)[0].active


def test_one_active_account_per_user_is_enforced_by_the_schema(db: Path, gs: GlobalStore) -> None:
    uid = gs.ensure_user(make_user()).uid
    gs.link_account(uid, None, "d")
    conn = sqlite3.connect(db)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO accounts VALUES (?, ?, NULL, 1, '2026-01-01T00:00:00Z', 'd')",
            (new_ulid(), uid),
        )
    conn.close()


def _audit_rows(db: Path) -> list[tuple[str, str, str, float, str, str]]:
    conn = sqlite3.connect(db)
    rows = conn.execute(
        "SELECT ts, uid, tool, duration_ms, outcome, identifiers FROM audit ORDER BY id"
    ).fetchall()
    conn.close()
    return rows


def test_audit_writes_identifiers_only(db: Path, gs: GlobalStore) -> None:
    user = make_user()
    gs.audit(
        uid_hash=user.uid_hash,
        tool="read_page",
        duration_ms=12.5,
        outcome="ok",
        identifiers={"uuid": DOC, "page": 3, "job": 7, "bytes": 2048, "acct": ACCT},
    )
    gs.audit(
        uid_hash=user.uid_hash[:16],
        tool="push_file",
        duration_ms=0,
        outcome="confirm_required",
        identifiers={"host": "example.org", "uuids": [DOC], "snapshot": "2026-10-18T00:00Z"},
    )
    rows = _audit_rows(db)
    assert [r[1] for r in rows] == [user.uid_hash[:16]] * 2
    assert json.loads(rows[0][5]) == {
        "acct": ACCT,
        "bytes": 2048,
        "job": 7,
        "page": 3,
        "uuid": DOC,
    }
    assert rows[0][0].endswith("Z")


@pytest.mark.parametrize(
    "identifiers",
    [
        {"name": "Meeting notes"},  # not an allowed key
        {"arguments": {"text": "x"}},
        {"uuid": "Meeting notes"},  # allowed key, content-shaped value
        {"uuid": DOC.upper().replace("0", "A")},
        {"uuids": [DOC, "secret text"]},
        {"uuids": [DOC] * 101},
        {"host": "evil host with spaces"},
        {"host": "https://example.org/path"},
        {"acct": "acct-1"},
        {"job": "7"},
        {"page": True},
        {"bytes": -1},
        {"snapshot": "../x y"},
    ],
)
def test_audit_refuses_anything_but_identifiers(
    db: Path, gs: GlobalStore, identifiers: dict[str, object]
) -> None:
    with pytest.raises(ValueError) as info:
        gs.audit(
            uid_hash=make_user().uid_hash,
            tool="list",
            duration_ms=1,
            outcome="ok",
            identifiers=identifiers,
        )
    assert "Meeting" not in str(info.value) and "secret" not in str(info.value)
    assert _audit_rows(db) == []


@pytest.mark.parametrize(
    ("tool", "outcome", "duration"),
    [
        ("Read Page", "ok", 1.0),
        ("read_page", "OK!", 1.0),
        ("read_page", "", 1.0),
        ("read_page", "ok", -1.0),
        ("read_page", "ok", float("nan")),
        ("read_page", "ok", float("inf")),
    ],
)
def test_audit_validates_its_fields(
    gs: GlobalStore, tool: str, outcome: str, duration: float
) -> None:
    with pytest.raises(ValueError):
        gs.audit(
            uid_hash=make_user().uid_hash,
            tool=tool,
            duration_ms=duration,
            outcome=outcome,
            identifiers={},
        )


def test_audit_refuses_a_malformed_user_hash(gs: GlobalStore) -> None:
    with pytest.raises(InvalidArgumentError):
        gs.audit(uid_hash="someone", tool="list", duration_ms=1, outcome="ok", identifiers={})

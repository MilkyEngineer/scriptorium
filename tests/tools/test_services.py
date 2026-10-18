"""Services: the server owns migrations and opens each account's index once (§6.1)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from scriptorium.tools._services import Services
from scriptorium.users.store import GlobalStore, account_paths, global_db_path
from tests.conftest import make_settings
from tests.worker.helpers import make_user as _user


def _version(path: Path) -> int:
    conn = sqlite3.connect(path)
    try:
        (version,) = conn.execute("PRAGMA user_version").fetchone()
        return int(version)
    finally:
        conn.close()


def test_open_migrates_the_global_database_and_every_account_index(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    settings = make_settings(data_dir=data)
    store = GlobalStore.open(global_db_path(data), migrate=True)
    uid = store.ensure_user(_user()).uid
    row = store.link_account(uid, "c" * 64, "desktop-linux")
    paths = account_paths(data, uid, row.acct)
    paths.root.mkdir(parents=True)
    sqlite3.connect(paths.index).close()  # an index left at version 0 by an older release
    assert _version(paths.index) == 0
    services = Services.open(settings)
    try:
        assert _version(paths.index) > 0
        index = services.index(uid, row.acct)
        assert services.index(uid, row.acct) is index  # opened once per process
    finally:
        services.close()


def test_uid_is_created_once_and_cached(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    services = Services.open(make_settings(data_dir=data))
    try:
        user = _user()
        uid = services.uid(user)
        assert services.uid(user) == uid
        assert services.store.ensure_user(user).uid == uid
        index = services.index(uid, "01J0000000000000000000000A")
        assert index.counts()["documents"] == 0
        assert (data / "users" / uid / "accounts" / "01J0000000000000000000000A").is_dir()
    finally:
        services.close()


def test_uid_records_the_email_digest_only_while_listed(tmp_path: Path) -> None:
    import dataclasses
    import hashlib

    data = tmp_path / "data"
    data.mkdir()
    listed = make_settings(data_dir=data, server_key_users="Reader@Example.com")
    services = Services.open(listed)
    try:
        user = dataclasses.replace(_user(), email="reader@example.COM", email_verified=True)
        uid = services.uid(user)
        row = services.store.user(uid)
        assert row is not None
        assert row.server_key_email_sha256 == hashlib.sha256(b"reader@example.com").hexdigest()
        # An unverified email (or none) clears it on the next request.
        services.uid(dataclasses.replace(user, email_verified=False))
        row = services.store.user(uid)
        assert row is not None and row.server_key_email_sha256 is None
    finally:
        services.close()
    unlisted = Services.open(make_settings(data_dir=data, server_key_users="other@example.com"))
    try:
        services.store.set_server_key_email(uid, "ab" * 32)
        unlisted.uid(user)
        row = unlisted.store.user(uid)
        assert row is not None and row.server_key_email_sha256 is None
    finally:
        unlisted.close()


def test_key_checks_are_rate_limited_per_user(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    now = [0.0]
    services = Services.open(make_settings(data_dir=data), monotonic=lambda: now[0])
    try:
        assert [services.allow_key_check("a" * 16) for _ in range(6)] == [True] * 5 + [False]
        assert services.allow_key_check("b" * 16)  # per user
        now[0] = 15 * 60 + 1
        assert services.allow_key_check("a" * 16)
    finally:
        services.close()

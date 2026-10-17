"""The per-request user context variable."""

from __future__ import annotations

import pytest

from scriptorium.auth.context import (
    NoUserContextError,
    current_user,
    maybe_current_user,
    user_context,
)
from scriptorium.auth.oidc import User, uid_hash_for


def _user(sub: str) -> User:
    iss = "https://auth.example.com"
    return User(
        iss=iss,
        sub=sub,
        uid_hash=uid_hash_for(iss, sub),
        display_name=sub,
        email=None,
        email_verified=False,
        is_admin=False,
        scopes=frozenset(),
    )


def test_no_user_outside_a_request() -> None:
    assert maybe_current_user() is None
    with pytest.raises(NoUserContextError):
        current_user()


def test_context_nests_and_resets() -> None:
    alice, bob = _user("a"), _user("b")
    with user_context(alice):
        assert current_user() is alice
        with user_context(bob):
            assert current_user() is bob
        assert current_user() is alice
    assert maybe_current_user() is None


def test_uid_hash_is_iss_and_sub() -> None:
    assert uid_hash_for("https://a.example.com", "x") != uid_hash_for("https://b.example.com", "x")
    # The issuer cannot contain the separator (config rejects it), so the pair is unambiguous.
    iss = "https://a.example.com"
    assert uid_hash_for(iss, "x|y") != uid_hash_for(iss, "x")

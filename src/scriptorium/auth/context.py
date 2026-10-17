"""The authenticated user of the current request, as a context variable.

The bearer-auth middleware sets it before the MCP SDK sees the request; tools
read it with :func:`current_user`. Nothing else carries user identity, so no
tool can be pointed at another user's data by its arguments.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from scriptorium.auth.oidc import User

_current_user: ContextVar[User | None] = ContextVar("scriptorium_current_user", default=None)


class NoUserContextError(RuntimeError):
    """Raised when user-scoped code runs outside an authenticated request."""


def current_user() -> User:
    """The user of the request being served; raises if there is none."""
    user = _current_user.get()
    if user is None:
        raise NoUserContextError("no authenticated user in this context")
    return user


def maybe_current_user() -> User | None:
    """The user of the request being served, or None outside a request."""
    return _current_user.get()


@contextmanager
def user_context(user: User) -> Iterator[User]:
    """Bind ``user`` for the duration of the block (and tasks spawned within it)."""
    token = _current_user.set(user)
    try:
        yield user
    finally:
        _current_user.reset(token)

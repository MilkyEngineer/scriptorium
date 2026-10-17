"""ToolSpec / ToolRegistry invariants."""

from __future__ import annotations

import pytest
from mcp_types import CallToolResult

from scriptorium.auth.oidc import User, uid_hash_for
from scriptorium.tools import ToolContext, ToolRegistry, ToolSpec
from scriptorium.tools._conventions import READ_ONLY, NoArgs, text_result


async def documented(ctx: ToolContext, args: NoArgs) -> CallToolResult:
    """Does a thing.

    Over two paragraphs.
    """
    return text_result("ok")


async def undocumented(ctx: ToolContext, args: NoArgs) -> CallToolResult:
    return text_result("ok")


async def admin_thing(ctx: ToolContext, args: NoArgs) -> CallToolResult:
    """Administrators only."""
    return text_result("ok")


def _user(*, admin: bool) -> User:
    iss = "https://auth.example.com"
    return User(
        iss=iss,
        sub="s",
        uid_hash=uid_hash_for(iss, "s"),
        display_name="s",
        email=None,
        email_verified=False,
        is_admin=admin,
        scopes=frozenset(),
    )


def test_description_from_docstring() -> None:
    spec = ToolSpec.from_handler(documented, args=NoArgs, annotations=READ_ONLY)
    assert spec.description == "Does a thing. Over two paragraphs."
    assert spec.to_mcp().input_schema == {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }


def test_docstring_required() -> None:
    with pytest.raises(ValueError, match="docstring"):
        ToolSpec.from_handler(undocumented, args=NoArgs, annotations=READ_ONLY)


@pytest.mark.parametrize("name", ["Bad-Name", "has space", "listDocuments", "_private"])
def test_names_are_snake_case(name: str) -> None:
    with pytest.raises(ValueError, match="snake_case"):
        ToolSpec.from_handler(documented, args=NoArgs, annotations=READ_ONLY, name=name)


def test_admin_prefix_iff_admin_only() -> None:
    with pytest.raises(ValueError, match="admin"):
        ToolSpec.from_handler(admin_thing, args=NoArgs, annotations=READ_ONLY)
    with pytest.raises(ValueError, match="admin"):
        ToolSpec.from_handler(documented, args=NoArgs, annotations=READ_ONLY, admin_only=True)


def test_registry_visibility_and_duplicates() -> None:
    registry = ToolRegistry()
    registry.add(ToolSpec.from_handler(documented, args=NoArgs, annotations=READ_ONLY))
    registry.add(
        ToolSpec.from_handler(admin_thing, args=NoArgs, annotations=READ_ONLY, admin_only=True)
    )
    with pytest.raises(ValueError, match="duplicate"):
        registry.add(ToolSpec.from_handler(documented, args=NoArgs, annotations=READ_ONLY))
    assert [s.name for s in registry.visible_to(_user(admin=False))] == ["documented"]
    assert [s.name for s in registry.visible_to(_user(admin=True))] == [
        "documented",
        "admin_thing",
    ]
    assert registry.input_schema("missing") is None

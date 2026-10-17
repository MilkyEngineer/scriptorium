"""Tool registry: one :class:`ToolSpec` per MCP tool, filtered per user.

A tool is an async handler ``(ToolContext, Args) -> CallToolResult`` whose
arguments are a :class:`~scriptorium.tools._conventions.ToolArgs` model and whose
docstring becomes its description. The server lists only the tools visible to
the requesting user and refuses calls to the rest, so visibility and
authorisation are the same rule.
"""

from __future__ import annotations

import inspect
import re
from collections.abc import Awaitable, Callable, Iterator, Mapping
from dataclasses import dataclass
from typing import Any, Final, Protocol

from mcp_types import CallToolResult, Tool, ToolAnnotations

from scriptorium.auth.oidc import User
from scriptorium.config import Settings
from scriptorium.tools._conventions import ToolArgs

TOOL_NAME_RE: Final = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
ADMIN_PREFIX: Final = "admin_"


class DownloadMinter(Protocol):
    """Mints short-lived capability tokens for ``/dl/<token>`` (§11.0 "Files")."""

    def mint(self, data: bytes, *, media_type: str, owner: str, filename: str) -> str:
        """Store ``data`` for ``owner`` (a uid hash) and return the URL token."""
        ...


@dataclass(frozen=True, slots=True)
class ToolContext:
    """What a tool handler may use. The user comes from the bearer token only."""

    user: User
    settings: Settings
    downloads: DownloadMinter

    def download_url(self, token: str) -> str:
        return f"{self.settings.public_origin}/dl/{token}"


type ToolHandler[A: ToolArgs] = Callable[[ToolContext, A], Awaitable[CallToolResult]]


def _description(handler: Callable[..., object]) -> str:
    doc = inspect.getdoc(handler)
    if not doc:
        raise ValueError(f"tool {handler.__name__!r} needs a docstring (it is the description)")
    return " ".join(doc.split())


@dataclass(frozen=True, slots=True)
class ToolSpec[ArgsT: ToolArgs]:
    name: str
    description: str
    args_model: type[ArgsT]
    handler: ToolHandler[ArgsT]
    annotations: ToolAnnotations
    admin_only: bool = False

    def __post_init__(self) -> None:
        if not TOOL_NAME_RE.fullmatch(self.name):
            raise ValueError(f"tool name {self.name!r} is not flat snake_case")
        if self.name.startswith(ADMIN_PREFIX) != self.admin_only:
            raise ValueError(f"tool {self.name!r}: admin tools are exactly the admin_* tools")

    @classmethod
    def from_handler(
        cls,
        handler: ToolHandler[ArgsT],
        *,
        args: type[ArgsT],
        annotations: ToolAnnotations,
        admin_only: bool = False,
        name: str | None = None,
    ) -> ToolSpec[ArgsT]:
        return cls(
            name=name or handler.__name__,
            description=_description(handler),
            args_model=args,
            handler=handler,
            annotations=annotations,
            admin_only=admin_only,
        )

    def visible_to(self, user: User) -> bool:
        return user.is_admin or not self.admin_only

    def input_schema(self) -> dict[str, Any]:
        schema = self.args_model.model_json_schema()
        # The tool description is the handler's docstring; the model's own is for developers.
        schema.pop("title", None)
        schema.pop("description", None)
        schema.setdefault("properties", {})
        return schema

    def to_mcp(self) -> Tool:
        return Tool(
            name=self.name,
            description=self.description,
            input_schema=self.input_schema(),
            annotations=self.annotations,
        )

    async def invoke(self, ctx: ToolContext, arguments: Mapping[str, Any]) -> CallToolResult:
        """Validate ``arguments`` against the model and run the handler.

        Raises pydantic's ``ValidationError`` for bad arguments; the server turns
        that into ``invalid_argument``.
        """
        args = self.args_model.model_validate(dict(arguments))
        return await self.handler(ctx, args)


class ToolRegistry:
    """The set of tools this server offers, keyed by name."""

    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec[Any]] = {}

    def add(self, spec: ToolSpec[Any]) -> None:
        if spec.name in self._tools:
            raise ValueError(f"duplicate tool {spec.name!r}")
        self._tools[spec.name] = spec

    def get(self, name: str) -> ToolSpec[Any] | None:
        return self._tools.get(name)

    def visible_to(self, user: User) -> list[ToolSpec[Any]]:
        return [spec for spec in self._tools.values() if spec.visible_to(user)]

    def input_schema(self, name: str) -> dict[str, Any] | None:
        spec = self._tools.get(name)
        return None if spec is None else spec.input_schema()

    def __iter__(self) -> Iterator[ToolSpec[Any]]:
        return iter(self._tools.values())

    def __len__(self) -> int:
        return len(self._tools)


__all__ = [
    "DownloadMinter",
    "ToolContext",
    "ToolHandler",
    "ToolRegistry",
    "ToolSpec",
]

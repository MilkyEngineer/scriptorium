"""Spike S6 test tools: what do Claude clients render and accept?

Registered only when ``SCRIPTORIUM_SPIKE_TOOLS=true``. They touch no user data:
images are random noise generated on the fly, text is a fixed pattern, and
``spike_echo_size`` reports only the size of what it received.
"""

from __future__ import annotations

import base64
import math
import os
import struct
import zlib
from typing import Final

import anyio.to_thread
from mcp_types import CallToolResult, ImageContent, ResourceLink, TextContent
from pydantic import Field

from scriptorium.tools import ToolContext, ToolRegistry, ToolSpec
from scriptorium.tools._conventions import (
    READ_ONLY,
    TEXT_CAP_BYTES,
    NoArgs,
    ToolArgs,
    invalid_argument,
    json_result,
    text_result,
)

MIN_IMAGE_KB: Final = 50
MAX_IMAGE_KB: Final = 2048
PNG_MEDIA_TYPE: Final = "image/png"

# ---- payload generators (pure, run in a worker thread) -----------------------------------

_PNG_SIGNATURE: Final = b"\x89PNG\r\n\x1a\n"
_STORED_BLOCK: Final = 65535  # zlib level 0 emits stored blocks of at most this many bytes


def _chunk(kind: bytes, data: bytes) -> bytes:
    crc = zlib.crc32(kind + data) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", crc)


def _png_size(width: int, rows: int) -> int:
    raw = rows * (width * 3 + 1)
    blocks = max(1, math.ceil(raw / _STORED_BLOCK))
    idat = 2 + raw + 5 * blocks + 4  # zlib header, stored blocks, adler32
    return len(_PNG_SIGNATURE) + (12 + 13) + (12 + idat) + 12


def make_noise_png(target_bytes: int) -> bytes:
    """A valid RGB PNG of random noise whose file size is close to ``target_bytes``.

    Noise does not compress, so the image is stored with zlib level 0 and its size
    is computed exactly: the result is within one pixel row of the target.
    """
    width = max(16, min(2048, int(math.sqrt(target_bytes / 3)) // 16 * 16))
    rows = max(1, (target_bytes - _png_size(width, 0)) // (width * 3 + 1))
    # Largest image that does not exceed the target (stored-block overhead is non-linear).
    while rows > 1 and _png_size(width, rows) > target_bytes:
        rows -= 1
    while _png_size(width, rows + 1) <= target_bytes:
        rows += 1
    noise = os.urandom(rows * width * 3)
    stride = width * 3
    raw = b"".join(b"\x00" + noise[y * stride : (y + 1) * stride] for y in range(rows))
    ihdr = struct.pack(">IIBBBBB", width, rows, 8, 2, 0, 0, 0)  # 8-bit RGB, no interlace
    return (
        _PNG_SIGNATURE
        + _chunk(b"IHDR", ihdr)
        + _chunk(b"IDAT", zlib.compress(raw, 0))
        + _chunk(b"IEND", b"")
    )


def make_text(kib: int) -> str:
    """Exactly ``kib`` KiB of ASCII: numbered 64-byte lines."""
    lines = kib * 1024 // 64
    return "".join(f"Scriptorium S6 text line {n:06d} ".ljust(63, ".") + "\n" for n in range(lines))


# ---- argument models ----------------------------------------------------------------------


class ImageArgs(ToolArgs):
    size_kb: int = Field(
        ge=MIN_IMAGE_KB,
        le=MAX_IMAGE_KB,
        description="Approximate PNG size in KB (50-2048; the S6 cases are 200 and 1536).",
    )


class TextArgs(ToolArgs):
    kb: int = Field(ge=1, description="Text size in KiB (1-64; larger is invalid_argument).")


class EchoArgs(ToolArgs):
    payload: str = Field(description="Any text; only its size is reported back.")


# ---- tools --------------------------------------------------------------------------------


async def spike_whoami(ctx: ToolContext, args: NoArgs) -> CallToolResult:
    """Spike S6: report who the server thinks you are. Returns your hashed user id, display
    name, whether you are an administrator, and the scopes in your access token; it never
    returns your email address."""
    user = ctx.user
    return json_result(
        {
            "uid_hash": user.uid_hash,
            "display_name": user.display_name,
            "is_admin": user.is_admin,
            "scopes": sorted(user.scopes),
        }
    )


async def _png(size_kb: int) -> bytes:
    return await anyio.to_thread.run_sync(make_noise_png, size_kb * 1024)


async def spike_image(ctx: ToolContext, args: ImageArgs) -> CallToolResult:
    """Spike S6: return a PNG image content block of roughly size_kb kilobytes (random
    noise, 50-2048 KB) to measure how each Claude client displays inline images."""
    png = await _png(args.size_kb)
    return CallToolResult(
        content=[ImageContent(data=base64.b64encode(png).decode("ascii"), mime_type=PNG_MEDIA_TYPE)]
    )


async def spike_resource_link(ctx: ToolContext, args: ImageArgs) -> CallToolResult:
    """Spike S6: return an MCP resource link to a PNG of roughly size_kb kilobytes served
    from a short-lived download URL, to see whether clients offer the linked file."""
    png = await _png(args.size_kb)
    filename = f"spike-{args.size_kb}kb.png"
    token = ctx.downloads.mint(
        png, media_type=PNG_MEDIA_TYPE, owner=ctx.user.uid_hash, filename=filename
    )
    link = ResourceLink(
        uri=ctx.download_url(token),
        name=filename,
        description="Random-noise PNG for spike S6; the link expires in 10 minutes.",
        mime_type=PNG_MEDIA_TYPE,
        size=len(png),
    )
    return CallToolResult(content=[link])


async def spike_download_url(ctx: ToolContext, args: ImageArgs) -> CallToolResult:
    """Spike S6: return a plain-text https download URL for a PNG of roughly size_kb
    kilobytes, valid for 10 minutes, to see whether clients make it clickable."""
    png = await _png(args.size_kb)
    filename = f"spike-{args.size_kb}kb.png"
    token = ctx.downloads.mint(
        png, media_type=PNG_MEDIA_TYPE, owner=ctx.user.uid_hash, filename=filename
    )
    return CallToolResult(
        content=[
            TextContent(
                text=f"Download {filename} ({len(png)} bytes, expires in 10 minutes): "
                f"{ctx.download_url(token)}"
            )
        ]
    )


async def spike_text(ctx: ToolContext, args: TextArgs) -> CallToolResult:
    """Spike S6: return a text result of exactly kb KiB (1-64, the text cap) to measure how
    clients handle large text results."""
    if args.kb * 1024 > TEXT_CAP_BYTES:
        raise invalid_argument(
            f"Text results are capped at {TEXT_CAP_BYTES // 1024} KiB.", "Use kb between 1 and 64."
        )
    return text_result(make_text(args.kb))


async def spike_echo_size(ctx: ToolContext, args: EchoArgs) -> CallToolResult:
    """Spike S6: accept a text argument of any size and report only its length in UTF-8
    bytes, to measure the largest tool argument a client can send."""
    return json_result({"bytes": len(args.payload.encode("utf-8"))})


async def admin_spike_ping(ctx: ToolContext, args: NoArgs) -> CallToolResult:
    """Spike S6 (administrators only): reply "pong". Used to check that tools/list hides
    admin tools from other users without a reconnect."""
    return text_result("pong")


def register_spike_tools(registry: ToolRegistry) -> None:
    """Add the S6 test tools to ``registry``."""
    registry.add(ToolSpec.from_handler(spike_whoami, args=NoArgs, annotations=READ_ONLY))
    registry.add(ToolSpec.from_handler(spike_image, args=ImageArgs, annotations=READ_ONLY))
    registry.add(ToolSpec.from_handler(spike_resource_link, args=ImageArgs, annotations=READ_ONLY))
    registry.add(ToolSpec.from_handler(spike_download_url, args=ImageArgs, annotations=READ_ONLY))
    registry.add(ToolSpec.from_handler(spike_text, args=TextArgs, annotations=READ_ONLY))
    registry.add(ToolSpec.from_handler(spike_echo_size, args=EchoArgs, annotations=READ_ONLY))
    registry.add(
        ToolSpec.from_handler(admin_spike_ping, args=NoArgs, annotations=READ_ONLY, admin_only=True)
    )

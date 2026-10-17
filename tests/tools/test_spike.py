"""S6 spike tools: payload sizes and the registry they produce."""

from __future__ import annotations

import struct
import zlib

import pytest

from scriptorium.tools import ToolRegistry
from scriptorium.tools.spike import (
    MAX_IMAGE_KB,
    MIN_IMAGE_KB,
    make_noise_png,
    make_text,
    register_spike_tools,
)


@pytest.mark.parametrize("size_kb", [MIN_IMAGE_KB, 200, 1536, MAX_IMAGE_KB])
def test_noise_png_size(size_kb: int) -> None:
    target = size_kb * 1024
    png = make_noise_png(target)
    # Within one pixel row (at most 2048 px * 3 bytes + filter byte) of the target.
    assert target - (2048 * 3 + 1) <= len(png) <= target


def test_noise_png_is_valid() -> None:
    png = make_noise_png(60 * 1024)
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    width, height = struct.unpack(">II", png[16:24])
    # Walk the chunks and check every CRC, then inflate the image data.
    offset, idat = 8, b""
    kinds = []
    while offset < len(png):
        (length,) = struct.unpack(">I", png[offset : offset + 4])
        kind = png[offset + 4 : offset + 8]
        data = png[offset + 8 : offset + 8 + length]
        (crc,) = struct.unpack(">I", png[offset + 8 + length : offset + 12 + length])
        assert zlib.crc32(kind + data) & 0xFFFFFFFF == crc
        kinds.append(kind)
        if kind == b"IDAT":
            idat += data
        offset += 12 + length
    assert kinds == [b"IHDR", b"IDAT", b"IEND"]
    assert len(zlib.decompress(idat)) == height * (width * 3 + 1)


@pytest.mark.parametrize("kib", [1, 17, 64])
def test_make_text_exact(kib: int) -> None:
    assert len(make_text(kib).encode()) == kib * 1024


def _registry() -> ToolRegistry:
    registry = ToolRegistry()
    register_spike_tools(registry)
    return registry


def test_spike_tools() -> None:
    registry = _registry()
    names = {spec.name for spec in registry}
    assert names == {
        "spike_whoami",
        "spike_image",
        "spike_resource_link",
        "spike_download_url",
        "spike_text",
        "spike_echo_size",
        "admin_spike_ping",
    }
    for spec in registry:
        assert spec.description and "\n" not in spec.description
        assert spec.annotations.read_only_hint is True
        assert spec.admin_only is spec.name.startswith("admin_")
        schema = spec.input_schema()
        assert schema["type"] == "object"
        assert schema.get("additionalProperties") is False


def test_image_schema_bounds() -> None:
    registry = _registry()
    spec = registry.get("spike_image")
    assert spec is not None
    size = spec.input_schema()["properties"]["size_kb"]
    assert (size["minimum"], size["maximum"]) == (MIN_IMAGE_KB, MAX_IMAGE_KB)

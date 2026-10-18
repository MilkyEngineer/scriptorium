"""Rendering against the S3 fixture and goldens (Architecture §8.3, docs/Spikes/S3.md)."""

from __future__ import annotations

import dataclasses
import hashlib
import importlib.util
import io
import json
import sys
from functools import cache
from pathlib import Path
from typing import Any

import pypdfium2 as pdfium
import pytest
from PIL import Image, ImageChops
from remarkable_mcp.strokes import append_strokes
from rmscene import read_blocks, write_blocks
from rmscene.scene_stream import SceneLineItemBlock

from scriptorium.errors import InvalidArgumentError
from scriptorium.library import render
from scriptorium.library.render import (
    PX_PER_UNIT,
    RenderResult,
    extract_pdf_page,
    page_image_bytes,
    render_in_pool,
    render_pdf_page,
    render_pool,
    render_rm_page,
)

FIXTURE = Path(__file__).parents[1] / "fixtures" / "move-test-notebook"
GOLDEN = FIXTURE / "golden"
MANIFEST: dict[str, Any] = json.loads((FIXTURE / "fixture.json").read_text())
GOLDEN_JSON: dict[str, Any] = json.loads((GOLDEN / "golden.json").read_text())


def _notebook_rm(n: int) -> bytes:
    doc = MANIFEST["notebook"]["doc"]
    page = MANIFEST["notebook"]["pages"][n - 1]
    return (FIXTURE / "notebook" / "xochitl" / doc / f"{page}.rm").read_bytes()


def _pdf_rm() -> bytes:
    doc = MANIFEST["pdf"]["doc"]
    page = MANIFEST["pdf"]["pages"][0]
    return (FIXTURE / "pdf" / "xochitl" / doc / f"{page}.rm").read_bytes()


def _pdf() -> bytes:
    return (FIXTURE / "pdf" / "xochitl" / f"{MANIFEST['pdf']['doc']}.pdf").read_bytes()


@cache
def _rendered(n: int, scale: float = 1.0) -> RenderResult:
    return render_rm_page(_notebook_rm(n), scale=scale)


@cache
def _rendered_pdf() -> RenderResult:
    return render_rm_page(_pdf_rm(), pdf_page=extract_pdf_page(_pdf(), 0))


def _pixels(png: bytes) -> Image.Image:
    with Image.open(io.BytesIO(png)) as im:
        return im.convert("RGB")


def _difference_mask(a: Image.Image, b: Image.Image) -> Image.Image:
    """An ``L`` image: the largest channel difference at each pixel (0 where ``a == b``)."""
    r, g, b_ = ImageChops.difference(a, b).split()
    return ImageChops.lighter(ImageChops.lighter(r, g), b_)


def _count_nonzero(mask: Image.Image) -> int:
    return mask.width * mask.height - mask.histogram()[0]


MAX_PAGE_TEXT_FRACTION = 0.01
"""Share of pixels that may differ from the golden where the PDF page itself is drawn."""


def _assert_matches_golden(
    result: RenderResult, name: str, *, plain_page: RenderResult | None = None
) -> None:
    """``result`` is the golden picture.

    Byte equality holds on the machine that made the goldens; elsewhere the PNG bytes depend
    on the zlib and resvg builds in the wheels, so the decoded pixels are compared instead.
    Notebook pages must be pixel-identical. For an annotated PDF page, ``plain_page`` is the
    same page rendered without ink on this platform: every pixel the ink touches must be
    identical to the golden, while the page raster underneath may differ a little, because the
    fixture PDF does not embed its fonts and PDFium substitutes whatever the platform has
    (on Linux the first CI run differed in 0.41% of pixels, all inside the two lines of text).
    """
    golden = GOLDEN_JSON["pages"][name]
    x, y, w, h = golden["viewbox_units"]
    assert (result.width, result.height) == tuple(golden["px"])
    assert result.extent_units == pytest.approx((x, y, x + w, y + h), abs=0.06)
    golden_png = (GOLDEN / f"{name}.png").read_bytes()
    assert hashlib.sha256(golden_png).hexdigest() == golden["sha256"], "golden file changed"
    if result.png == golden_png:
        return
    actual, expected = _pixels(result.png), _pixels(golden_png)
    diff = _difference_mask(actual, expected)
    if plain_page is None:
        assert diff.getbbox() is None, (
            f"{name}: {_count_nonzero(diff)} pixels differ from the golden "
            f"(largest channel difference {diff.getextrema()[1]})"
        )
        return
    assert (plain_page.width, plain_page.height) == (result.width, result.height)
    ink = _difference_mask(actual, _pixels(plain_page.png)).point(lambda v: 255 if v else 0)
    on_ink = ImageChops.multiply(diff, ink)
    assert on_ink.getbbox() is None, (
        f"{name}: {_count_nonzero(on_ink)} ink pixels differ from the golden"
    )
    fraction = _count_nonzero(diff) / (result.width * result.height)
    assert fraction <= MAX_PAGE_TEXT_FRACTION, (
        f"{name}: {fraction:.4%} of page pixels differ from the golden "
        f"(largest channel difference {diff.getextrema()[1]})"
    )


# ----------------------------------------------------------------------------- goldens


@pytest.mark.parametrize("n", range(1, len(MANIFEST["notebook"]["pages"]) + 1))
def test_notebook_page_matches_golden(n: int) -> None:
    _assert_matches_golden(_rendered(n), f"notebook-{n:02d}")


def test_annotated_pdf_page_matches_golden() -> None:
    result = _rendered_pdf()
    _assert_matches_golden(result, "pdf-01", plain_page=render_pdf_page(_pdf(), 0))
    # The page is laid out at 226/72 units per point, not at the .rm's 1404 x 1872 paper.
    w_pt, h_pt = 612.0, 792.0  # US Letter, the fixture PDF
    x0, y0, x1, y1 = result.extent_units
    assert (x1 - x0, y1 - y0) == pytest.approx((w_pt * 226 / 72, h_pt * 226 / 72), abs=0.01)
    assert (x0 + x1) == pytest.approx(0.0, abs=0.01)  # top centre at the origin


def test_annotated_pdf_page_accepts_a_whole_single_page_document() -> None:
    # The fixture PDF has one page, so the document itself is a valid ``pdf_page``.
    assert render_rm_page(_pdf_rm(), pdf_page=_pdf()).png == _rendered_pdf().png


def test_unscrolled_page_is_exactly_the_paper_rectangle() -> None:
    result = _rendered(1)
    assert result.extent_units == (-410.0, 0.0, 410.0, 1458.0)
    assert (result.width, result.height) == (954, 1696)


def test_scrolled_page_extent_exceeds_the_paper_rectangle() -> None:
    x0, y0, x1, y1 = _rendered(7).extent_units
    assert x0 < -410.0 and x1 > 410.0  # panned left and right
    assert y1 > 1458.0  # scrolled down
    assert y0 == 0.0
    assert _rendered(7).width > 954 and _rendered(7).height > 1696


# ----------------------------------------------------------------------------- scale


# Page 7 (the scrolled page) at 2x is a 29 MP canvas and takes seconds; 0.5x covers its extent.
@pytest.mark.parametrize(("n", "scale"), [(1, 0.5), (1, 2.0), (7, 0.5)])
def test_scale_is_proportional(n: int, scale: float) -> None:
    base = _rendered(n)
    scaled = _rendered(n, scale)
    assert scaled.extent_units == base.extent_units
    x0, y0, x1, y1 = base.extent_units
    assert scaled.width == round((x1 - x0) * PX_PER_UNIT * scale)
    assert scaled.height == round((y1 - y0) * PX_PER_UNIT * scale)
    assert abs(scaled.width - base.width * scale) <= 1
    assert abs(scaled.height - base.height * scale) <= 1
    assert _pixels(scaled.png).size == (scaled.width, scaled.height)


def test_pdf_page_scales_with_the_same_geometry() -> None:
    pdf = _pdf()
    plain = render_pdf_page(pdf, 0)
    annotated = _rendered_pdf()
    assert (plain.width, plain.height) == (annotated.width, annotated.height)
    assert plain.extent_units == annotated.extent_units
    half = render_pdf_page(pdf, 0, scale=0.5)
    assert abs(half.width - plain.width / 2) <= 1 and abs(half.height - plain.height / 2) <= 1


@pytest.mark.parametrize("scale", [0.0, -1.0, 0.05, 4.5, float("nan"), float("inf")])
def test_scale_out_of_range_is_refused(scale: float) -> None:
    with pytest.raises(InvalidArgumentError):
        render_rm_page(_notebook_rm(1), scale=scale)


# ----------------------------------------------------------------------------- compositing


def _dark_box(image: Image.Image, top: int, bottom: int) -> tuple[int, int, int, int]:
    band = image.crop((0, top, image.width, bottom)).convert("L").point(lambda v: 255 * (v < 96))
    box = band.getbbox()
    assert box is not None
    return box[0], box[1] + top, box[2], box[3] + top


def test_highlighter_on_a_pdf_keeps_the_text_readable() -> None:
    """Highlighter multiplies with the PDF page; it must not paint over the text."""
    pdf = _pdf()
    plain = _pixels(render_pdf_page(pdf, 0).png)
    # The title line: the first dark pixels in the top fifth of the page.
    tx0, ty0, tx1, _ = _dark_box(plain, 0, plain.height // 5)
    title = _dark_box(plain, ty0, ty0 + 60)
    mid_y = (title[1] + title[3]) // 2
    x0_units = render_pdf_page(pdf, 0).extent_units[0]

    def to_norm(px: float, py: float) -> list[float]:  # .rm paper of the PDF page: 1404 x 1872
        return [(px / PX_PER_UNIT + x0_units) / 1404 + 0.5, (py / PX_PER_UNIT) / 1872]

    stroke = {
        "tool": "highlighter",
        "color": "yellow",
        "points": [to_norm(tx0 + 40, mid_y), to_norm(tx1 - 40, mid_y)],
    }
    rm = append_strokes(_pdf_rm(), [stroke])
    highlighted = _pixels(render_rm_page(rm, pdf_page=pdf).png)

    band = (tx0 + 80, mid_y - 8, tx1 - 80, mid_y + 8)
    before = plain.crop(band)
    after = highlighted.crop(band)
    grey = before.convert("L").tobytes()
    text = [i for i, v in enumerate(grey) if v < 64]
    paper = [i for i, v in enumerate(grey) if v > 250]
    assert text and paper
    rgb = after.tobytes()
    after_px = [tuple(rgb[3 * i : 3 * i + 3]) for i in range(len(grey))]
    assert all(sum(after_px[i]) / 3 < 64 for i in text), "text under the highlighter was covered"
    assert all(after_px[i][2] < 200 < after_px[i][0] for i in paper), "highlighter not drawn"


# ----------------------------------------------------------------------------- page_image


@pytest.mark.parametrize("n", range(1, len(MANIFEST["notebook"]["pages"]) + 1))
def test_page_image_bytes_is_the_200kb_class(n: int) -> None:
    result = _rendered(n)
    small = page_image_bytes(result)
    image = _pixels(small)
    assert max(image.size) == 1024
    ratio = result.width / result.height
    assert image.size[0] / image.size[1] == pytest.approx(ratio, rel=0.01)
    assert 10_000 < len(small) <= 250_000
    assert page_image_bytes(result) == small  # deterministic


def test_page_image_bytes_annotated_pdf_is_the_200kb_class() -> None:
    small = page_image_bytes(_rendered_pdf())
    assert max(_pixels(small).size) == 1024
    assert len(small) <= 250_000


def test_page_image_bytes_honours_max_side_and_never_upscales() -> None:
    result = _rendered(1)
    assert max(_pixels(page_image_bytes(result, max_side_px=300)).size) == 300
    assert page_image_bytes(result, max_side_px=5000) == result.png
    with pytest.raises(InvalidArgumentError):
        page_image_bytes(result, max_side_px=0)


def test_page_image_bytes_refuses_a_corrupt_png() -> None:
    bad = RenderResult(
        png=b"\x89PNG\r\n\x1a\nnot really", width=4000, height=4000, extent_units=(0, 0, 1, 1)
    )
    with pytest.raises(InvalidArgumentError):
        page_image_bytes(bad)


# ----------------------------------------------------------------------------- hostile input


def test_oversized_rm_is_refused_before_parsing() -> None:
    with pytest.raises(InvalidArgumentError, match="too large"):
        render_rm_page(b"\0" * (render.MAX_RM_BYTES + 1))


def test_oversized_pdf_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(render, "MAX_PDF_BYTES", 1024)
    with pytest.raises(InvalidArgumentError, match="too large"):
        render_pdf_page(_pdf(), 0)
    with pytest.raises(InvalidArgumentError, match="too large"):
        render_rm_page(_pdf_rm(), pdf_page=_pdf())
    with pytest.raises(InvalidArgumentError, match="too large"):
        extract_pdf_page(_pdf(), 0)


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"not an rm file at all",
        b"reMarkable .lines file, version=6          " + bytes(range(256)) * 8,
        b"reMarkable .lines file, version=5          " + b"\0" * 64,
    ],
    ids=["empty", "text", "v6-garbage", "v5"],
)
def test_corrupt_rm_raises_invalid_argument(data: bytes) -> None:
    with pytest.raises(InvalidArgumentError) as info:
        render_rm_page(data)
    assert info.value.code == "invalid_argument"


def test_truncated_rm_never_escapes_as_another_exception() -> None:
    data = _notebook_rm(2)
    for cut in (50, len(data) // 3, len(data) // 2, len(data) - 7):
        try:
            result = render_rm_page(data[:cut])
        except InvalidArgumentError:
            continue
        assert result.png.startswith(b"\x89PNG")


def _with_point_at(rm: bytes, x: float) -> bytes:
    blocks = list(read_blocks(io.BytesIO(rm)))
    for i, block in enumerate(blocks):
        if isinstance(block, SceneLineItemBlock) and block.item.value is not None:
            line = block.item.value
            point = dataclasses.replace(line.points[0], x=x)
            line = dataclasses.replace(line, points=[point, *line.points[1:]])
            item = dataclasses.replace(block.item, value=line)
            blocks[i] = dataclasses.replace(block, item=item)
            break
    out = io.BytesIO()
    write_blocks(out, blocks, options={"version": "3.1"})
    return out.getvalue()


def test_runaway_ink_extent_is_refused_before_rasterising() -> None:
    with pytest.raises(InvalidArgumentError, match="too large"):
        render_rm_page(_with_point_at(_notebook_rm(1), 1e7))


def test_non_finite_coordinates_are_refused() -> None:
    with pytest.raises(InvalidArgumentError):
        render_rm_page(_with_point_at(_notebook_rm(1), float("nan")))


def test_huge_canvas_is_refused_at_high_scale() -> None:
    with pytest.raises(InvalidArgumentError, match="too large") as info:
        render_rm_page(_notebook_rm(7), scale=4.0)
    assert info.value.hint == "render at a smaller scale"


def test_corrupt_pdf_raises_invalid_argument() -> None:
    with pytest.raises(InvalidArgumentError):
        render_pdf_page(b"%PDF-1.7\nthis is not a pdf", 0)
    with pytest.raises(InvalidArgumentError):
        render_rm_page(_pdf_rm(), pdf_page=b"%PDF-1.7\ngarbage")


@pytest.mark.parametrize("index", [-1, 1, 10_000])
def test_pdf_page_index_out_of_range(index: int) -> None:
    with pytest.raises(InvalidArgumentError, match="out of range"):
        render_pdf_page(_pdf(), index)
    with pytest.raises(InvalidArgumentError, match="out of range"):
        extract_pdf_page(_pdf(), index)


def test_multi_page_pdf_is_refused_as_pdf_page() -> None:
    src = pdfium.PdfDocument(_pdf())
    two = pdfium.PdfDocument.new()
    two.import_pages(src, [0, 0])
    buf = io.BytesIO()
    two.save(buf)
    two.close()
    src.close()
    with pytest.raises(InvalidArgumentError, match="single-page"):
        render_rm_page(_pdf_rm(), pdf_page=buf.getvalue())
    assert render_pdf_page(buf.getvalue(), 1).png == render_pdf_page(_pdf(), 0).png


def test_temporary_files_are_removed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("tempfile.tempdir", str(tmp_path))
    render_rm_page(_notebook_rm(1), scale=0.25)
    with pytest.raises(InvalidArgumentError):
        render_rm_page(b"reMarkable .lines file, version=6          garbage")
    assert list(tmp_path.iterdir()) == []


# ----------------------------------------------------------------------------- process pool


def test_rendering_works_through_the_process_pool() -> None:
    with render_pool(1) as pool:
        notebook = render_in_pool(pool, render_rm_page, _notebook_rm(1))
        annotated = render_in_pool(pool, render_rm_page, _pdf_rm(), pdf_page=_pdf())
        broken = render_in_pool(pool, render_rm_page, _notebook_rm(7), scale=4.0)
        assert notebook.result(timeout=120) == _rendered(1)
        assert annotated.result(timeout=120).png == _rendered_pdf().png
        with pytest.raises(InvalidArgumentError) as info:
            broken.result(timeout=120)
        assert info.value.hint == "render at a smaller scale"
        small = render_in_pool(pool, page_image_bytes, _rendered(1), max_side_px=512)
        assert max(_pixels(small.result(timeout=120)).size) == 512


def test_render_in_pool_runs_only_render_functions() -> None:
    with render_pool(1) as pool, pytest.raises(ValueError):
        render_in_pool(pool, print, "x")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        render_pool(0)


# ----------------------------------------------------------------------------- licence


@pytest.mark.parametrize("module", ["pymupdf", "fitz", "ebooklib", "rmink", "rmc"])
def test_copyleft_and_dropped_packages_are_not_importable(module: str) -> None:
    """CLAUDE.md: PyMuPDF (fitz), ebooklib and rmink are AGPL; rmc is dropped."""
    assert importlib.util.find_spec(module) is None
    assert module not in sys.modules


@pytest.mark.parametrize("index", [1.5, True, "0"])
def test_pdf_page_index_must_be_an_integer(index: Any) -> None:
    with pytest.raises(InvalidArgumentError, match="integer"):
        render_pdf_page(_pdf(), index)

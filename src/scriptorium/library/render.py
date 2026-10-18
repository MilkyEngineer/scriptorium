"""Render `.rm` pages and PDF pages to PNG (Architecture §8.3, spike S3).

Geometry, as measured in S3:

- ``.rm`` v6 strokes are in device-independent units (about 226 per inch), x centred on 0, y
  from the top. The Paper Pro Move panel shows 820 x 1458 units as 954 x 1696 px, so the
  archive scale is :data:`PX_PER_UNIT` = 954/820 px per unit.
- Ink is not confined to the paper rectangle (scrolled and zoomed-out pages), so a notebook
  page renders the union of the paper rectangle and the ink bounding box grown by
  :data:`INK_MARGIN_UNITS`. That is exactly the fork's ``render_rm_file_extent_png``, which
  produced the S3 goldens, so notebooks go through it unchanged.
- An annotated PDF page is laid out at its physical size, :data:`UNITS_PER_PT` = 226/72 units
  per point, with the page's top centre at the origin. The `.rm` of such a page reports the
  legacy 1404 x 1872 paper size, which is ignored. The canvas is the union of the PDF page
  rectangle and the ink extent; the page is rasterised by pypdfium2, ink is drawn over it.

Inputs are untrusted (they come from the cloud): sizes are bounded before parsing, the
rendered canvas is bounded before anything is rasterised, parser failures become
:class:`~scriptorium.errors.InvalidArgumentError` with a message that names no content, and the
only file written is the `.rm` the fork's renderer needs, inside a ``TemporaryDirectory``.

Every public function is pure, synchronous and top level, so a ``ProcessPoolExecutor`` can run
it (:func:`render_pool`, :func:`render_in_pool`). The one module-level object is a lock that
serialises PDFium inside a process, because PDFium is not thread-safe.

Only ``remarkable_mcp.extract`` is imported from the fork. Annotated pages use its private v6
helpers (``_v6_blocks``, ``_v6_text_elements_with_bounds``, ``_v6_paths_from_blocks``,
``_v6_paper_size``) because the public extent renderer cannot be told to ignore the `.rm`'s
paper size; the fork is pinned by tag, and the goldens catch any drift.
"""

from __future__ import annotations

import io
import logging
import math
import multiprocessing
import os
import re
import stat
import struct
import tempfile
import threading
from collections.abc import Callable, Sequence
from concurrent.futures import Executor, Future, ProcessPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import pypdfium2 as pdfium
import resvg_py
from PIL import Image, ImageChops
from remarkable_mcp import extract as _fork

from scriptorium.errors import InvalidArgumentError, NotFoundError, PathEscapeError

__all__ = [
    "INK_MARGIN_UNITS",
    "MAX_CANVAS_PIXELS",
    "MAX_CANVAS_SIDE_PX",
    "MAX_PDF_BYTES",
    "MAX_RM_BYTES",
    "MAX_SCALE",
    "MIN_SCALE",
    "PX_PER_UNIT",
    "UNITS_PER_PT",
    "RenderResult",
    "downscale_png",
    "extract_pdf_page",
    "extract_pdf_page_from_blob",
    "page_image_bytes",
    "render_in_pool",
    "render_pdf_page",
    "render_pool",
    "render_rm_page",
]

PX_PER_UNIT: Final[float] = 954 / 820
"""Archive (1x) pixels per `.rm` unit: the Move's panel, 954 px across 820 units."""

UNITS_PER_PT: Final[float] = 226 / 72
"""`.rm` units per PDF point on annotated PDF pages."""

INK_MARGIN_UNITS: Final[float] = 20.0
"""Units added around the ink bounding box before it is unioned with the page (S3 goldens)."""

MAX_RM_BYTES: Final[int] = 16 * 1024 * 1024
"""Largest `.rm` accepted. Real pages are tens of KB; a dense page a few MB."""

MAX_PDF_BYTES: Final[int] = 256 * 1024 * 1024
"""Largest PDF accepted (whole documents for :func:`render_pdf_page`)."""

MAX_CANVAS_PIXELS: Final[int] = 40_000_000
"""Largest canvas rendered (about 160 MB as RGBA). A scrolled page at 2x is about 30 M."""

MAX_CANVAS_SIDE_PX: Final[int] = 32_000
"""Longest canvas side rendered."""

MIN_SCALE: Final[float] = 0.1
MAX_SCALE: Final[float] = 4.0

MAX_TASKS_PER_CHILD: Final[int] = 200
"""Pool workers are recycled after this many renders, bounding heap growth from big pages."""

_BACKGROUND: Final[str] = "#FFFFFF"
_WHITE: Final[tuple[int, int, int]] = (255, 255, 255)
_MULTIPLY: Final[str] = "mix-blend-mode:multiply"  # how the fork marks highlighter/shader ink
_PNG_SIGNATURE: Final[bytes] = b"\x89PNG\r\n\x1a\n"

_PDFIUM_LOCK = threading.Lock()
_BLOB_NAME: Final = re.compile("[0-9a-f]{64}")
_BLOBS_DIR: Final[str] = "blobs"


@dataclass(frozen=True)
class RenderResult:
    """A rendered page: PNG bytes, pixel size, and the rendered rectangle in `.rm` units.

    ``extent_units`` is ``(x0, y0, x1, y1)``; pixel ``(px, py)`` maps to unit
    ``(x0 + px * (x1 - x0) / width, y0 + py * (y1 - y0) / height)``.
    """

    png: bytes
    width: int
    height: int
    extent_units: tuple[float, float, float, float]


@dataclass(frozen=True)
class _Ink:
    elements: list[str]  # SVG elements in unit coordinates, in drawing order
    coords: list[tuple[float, float]]
    paper: tuple[float, float]


Extent = tuple[float, float, float, float]


# --------------------------------------------------------------------------- public API


def render_rm_page(
    rm_bytes: bytes, *, scale: float = 1.0, pdf_page: bytes | None = None
) -> RenderResult:
    """Render one `.rm` v6 page at ``PX_PER_UNIT x scale`` px per unit.

    Without ``pdf_page`` the page is a notebook page: the union of the paper rectangle and the
    ink extent on white. With ``pdf_page`` (the bytes of a **single-page** PDF holding the page
    this `.rm` annotates; :func:`extract_pdf_page` cuts one out of a document) the PDF page is
    the background at 226/72 units per point, top centre at the origin, and the `.rm`'s own
    paper size is ignored. Raises :class:`InvalidArgumentError` for oversized, unparseable or
    unrenderable input.
    """
    _check_scale(scale)
    _check_bytes(rm_bytes, MAX_RM_BYTES, "page")
    if pdf_page is not None:
        _check_bytes(pdf_page, MAX_PDF_BYTES, "PDF")
    with tempfile.TemporaryDirectory(prefix="scriptorium-render-") as tmp:
        path = Path(tmp) / "page.rm"
        path.write_bytes(rm_bytes)
        ink = _parse_ink(path)
        if pdf_page is None:
            return _render_notebook(path, ink, scale)
    return _render_annotated(pdf_page, ink, scale)


def render_pdf_page(pdf_bytes: bytes, index: int, *, scale: float = 1.0) -> RenderResult:
    """Render page ``index`` (0-based) of a PDF that carries no ink.

    The geometry is the annotated-page geometry without ink: 226/72 units per point at
    ``PX_PER_UNIT x scale`` px per unit, so the same page renders at the same size whether or
    not it has been annotated. ``extent_units`` is the page rectangle, top centre at the origin.
    """
    _check_scale(scale)
    _check_bytes(pdf_bytes, MAX_PDF_BYTES, "PDF")
    ink = _Ink(elements=[], coords=[], paper=(0.0, 0.0))
    with _PDFIUM_LOCK, _open_pdf(pdf_bytes) as pdf:
        _check_index(index, len(pdf))
        page = _rasterise_pdf_page(pdf, index, ink, scale)
    return _compose(page, ink, scale)


def extract_pdf_page(pdf_bytes: bytes, index: int) -> bytes:
    """Return a single-page PDF holding page ``index`` (0-based) of ``pdf_bytes``.

    This is how a caller builds the ``pdf_page`` argument of :func:`render_rm_page`. Doing it
    once per page keeps what crosses into a pool worker small.
    """
    _check_bytes(pdf_bytes, MAX_PDF_BYTES, "PDF")
    with _PDFIUM_LOCK:
        return _extract_locked(pdf_bytes, index)


def extract_pdf_page_from_blob(blob_path: str, index: int) -> bytes:
    """Read an archived PDF blob and return page ``index`` (0-based) as a single-page PDF.

    This is the pool-side cut (§8.3): the worker passes the blob's path, never its bytes,
    so PDFium only ever parses a whole cloud-supplied PDF inside a render child. The path
    must be the archive's own resolved blob path: absolute and canonical (no symlink, no
    ``..``), named by a lowercase SHA-256, directly inside a directory named ``blobs``, and a
    regular file; anything else is :class:`~scriptorium.errors.PathEscapeError`. The file
    is opened without following a symlink, refused above :data:`MAX_PDF_BYTES` before it is
    read, and read under the PDFium lock. A missing blob is ``not_found``.
    """
    path = _check_blob_path(blob_path)
    with _PDFIUM_LOCK:
        data = _read_blob_file(path, MAX_PDF_BYTES)
        _check_bytes(data, MAX_PDF_BYTES, "PDF")
        return _extract_locked(data, index)


def page_image_bytes(result: RenderResult, *, max_side_px: int = 1024) -> bytes:
    """Return ``result`` as a PNG whose longest side is at most ``max_side_px``.

    This is the ``page_image`` size class of §11.2 (longest side 1024 px, about 200 KB for a
    handwritten page). Never upscales; a page that already fits is returned unchanged. The
    downscale (Lanczos) and the encode are deterministic: equal input gives equal bytes.
    """
    if max_side_px < 1:
        raise InvalidArgumentError("max_side_px must be positive")
    if max(result.width, result.height) <= max_side_px:
        return result.png
    try:
        with Image.open(io.BytesIO(result.png)) as src:
            image = src.convert("RGB")
    except (OSError, ValueError, Image.DecompressionBombError):
        raise InvalidArgumentError("page image could not be decoded") from None
    width, height = image.size
    factor = max_side_px / max(width, height)
    size = (max(1, round(width * factor)), max(1, round(height * factor)))
    return _encode(image.resize(size, Image.Resampling.LANCZOS))


def downscale_png(png: bytes, *, max_side_px: int) -> bytes:
    """Return the PNG ``png`` with its longest side at most ``max_side_px`` (PNG to PNG).

    The OCR image of §8.3 (longest side 2576 px) made from an archive PNG; the same rules as
    :func:`page_image_bytes`: Lanczos, deterministic, never upscales, a PNG that already fits
    comes back unchanged. The header is checked before anything is decoded, and an image
    over :data:`MAX_CANVAS_PIXELS` (which the renderer never writes) is refused undecoded.
    """
    width, height = _png_size(png)
    if width < 1 or height < 1 or width * height > MAX_CANVAS_PIXELS:
        raise InvalidArgumentError("page image is too large to scale")
    return page_image_bytes(
        RenderResult(png, width, height, (0.0, 0.0, 0.0, 0.0)), max_side_px=max_side_px
    )


# --------------------------------------------------------------------------- process pool


def _init_render_child(log_level: str) -> None:
    """Pool initializer: the parent's JSON logging, with the renderers held to errors.

    A spawned child starts with Python's default logging, which would print library warnings
    as raw, unbounded text. The fork's renderer warns with details of the page it failed on
    (an SVG of typed text), so in a child nothing below an error is logged from it or rmscene.
    """
    from scriptorium.logging import configure_logging

    configure_logging(log_level)
    for name in ("remarkable_mcp", "rmscene"):
        logging.getLogger(name).setLevel(logging.ERROR)


def render_pool(max_workers: int, *, log_level: str = "info") -> ProcessPoolExecutor:
    """Return a process pool configured for rendering; the caller owns and shuts it down.

    Workers start with ``spawn`` (the server and worker are threaded, and forking a threaded
    process is unsafe), log through :func:`scriptorium.logging.configure_logging` at
    ``log_level``, and are recycled every :data:`MAX_TASKS_PER_CHILD` tasks.
    """
    if max_workers < 1:
        raise ValueError("max_workers must be at least 1")
    return ProcessPoolExecutor(
        max_workers=max_workers,
        mp_context=multiprocessing.get_context("spawn"),
        max_tasks_per_child=MAX_TASKS_PER_CHILD,
        initializer=_init_render_child,
        initargs=(log_level,),
    )


def render_in_pool[**P, R](
    pool: Executor, fn: Callable[P, R], /, *args: P.args, **kwargs: P.kwargs
) -> Future[R]:
    """Submit one of this module's rendering functions to ``pool``.

    Only :func:`render_rm_page`, :func:`render_pdf_page`, :func:`extract_pdf_page`,
    :func:`extract_pdf_page_from_blob` and :func:`page_image_bytes` are accepted, so nothing
    else is shipped to a worker by accident.
    The future raises the same :class:`InvalidArgumentError` (message and hint intact) the
    function raises in-process. Wrap it with ``asyncio.wrap_future`` to await it.
    """
    if fn not in _POOL_FUNCTIONS:
        raise ValueError("render_in_pool runs only scriptorium.library.render functions")
    return pool.submit(fn, *args, **kwargs)


_POOL_FUNCTIONS: Final[frozenset[Callable[..., object]]] = frozenset(
    {
        render_rm_page,
        render_pdf_page,
        extract_pdf_page,
        extract_pdf_page_from_blob,
        page_image_bytes,
        downscale_png,
    }
)


# --------------------------------------------------------------------------- internals


def _extract_locked(pdf_bytes: bytes, index: int) -> bytes:
    """The single-page cut; the caller holds :data:`_PDFIUM_LOCK`."""
    with _open_pdf(pdf_bytes) as src:
        _check_index(index, len(src))
        out = pdfium.PdfDocument.new()
        try:
            out.import_pages(src, [index])
            buf = io.BytesIO()
            out.save(buf)
            return buf.getvalue()
        except pdfium.PdfiumError:
            raise InvalidArgumentError("PDF page could not be extracted") from None
        finally:
            out.close()


def _check_blob_path(blob_path: str) -> str:
    """``blob_path`` if it has the shape of an archive blob's resolved path, else refused."""
    if not isinstance(blob_path, str) or "\x00" in blob_path or not os.path.isabs(blob_path):
        raise PathEscapeError("PDF blob path is not an absolute path")
    try:
        canonical = os.path.realpath(blob_path) == blob_path
    except (OSError, ValueError):
        canonical = False
    head, name = os.path.split(blob_path)
    if not canonical or _BLOB_NAME.fullmatch(name) is None or os.path.basename(head) != _BLOBS_DIR:
        raise PathEscapeError("PDF blob path is not an archive blob")
    return blob_path


def _read_blob_file(path: str, limit: int) -> bytes:
    """A regular file's bytes, refused above ``limit`` before reading, symlinks refused."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        raise NotFoundError("blob is not in the archive") from None
    except OSError:
        raise PathEscapeError("PDF blob could not be opened as a file") from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise PathEscapeError("PDF blob is not a regular file")
        if info.st_size > limit:
            raise InvalidArgumentError(
                f"PDF is too large to render ({info.st_size} bytes)",
                hint=f"the limit is {limit} bytes",
            )
        fh = os.fdopen(fd, "rb")
    except BaseException:
        os.close(fd)
        raise
    with fh:
        return fh.read(limit + 1)


def _check_scale(scale: float) -> None:
    if not (isinstance(scale, int | float) and math.isfinite(scale)):
        raise InvalidArgumentError("scale must be a finite number")
    if not MIN_SCALE <= scale <= MAX_SCALE:
        raise InvalidArgumentError(
            "scale is out of range", hint=f"use a scale between {MIN_SCALE} and {MAX_SCALE}"
        )


def _check_index(index: int, count: int) -> None:
    if isinstance(index, bool) or not isinstance(index, int):
        raise InvalidArgumentError("PDF page index must be an integer")
    if not 0 <= index < count:
        raise InvalidArgumentError(
            f"PDF page index {index} is out of range", hint=f"the PDF has {count} pages"
        )


def _check_bytes(data: bytes, limit: int, what: str) -> None:
    if not isinstance(data, bytes | bytearray | memoryview):
        raise InvalidArgumentError(f"{what} must be bytes")
    if len(data) == 0:
        raise InvalidArgumentError(f"{what} is empty")
    if len(data) > limit:
        raise InvalidArgumentError(
            f"{what} is too large to render ({len(data)} bytes)",
            hint=f"the limit is {limit} bytes",
        )


def _parse_ink(path: Path) -> _Ink:
    """Parse a v6 `.rm` into SVG elements, their coordinates and the reported paper size."""
    try:
        blocks = _fork._v6_blocks(path)
        if blocks is None:
            raise InvalidArgumentError(
                "page is not a readable .rm v6 file", hint="only firmware 3.x pages are supported"
            )
        text, text_coords, anchors = _fork._v6_text_elements_with_bounds(blocks)
        paths, coords = _fork._v6_paths_from_blocks(blocks, anchors)
        paper_w, paper_h = _fork._v6_paper_size(blocks)
        elements = [str(e) for e in [*text, *paths]]
        points = [(float(x), float(y)) for x, y in [*coords, *text_coords]]
        paper = (float(paper_w), float(paper_h))
    except InvalidArgumentError:
        raise
    except Exception as exc:  # rmscene and the fork raise anything on hostile input
        raise InvalidArgumentError(f"page could not be parsed ({type(exc).__name__})") from None
    if not all(math.isfinite(v) for p in points for v in p):
        raise InvalidArgumentError("page has non-finite coordinates")
    if not (all(math.isfinite(v) and v > 0 for v in paper)):
        raise InvalidArgumentError("page has an invalid paper size")
    return _Ink(elements=elements, coords=points, paper=paper)


def _union_with_ink(base: Extent, coords: Sequence[tuple[float, float]]) -> Extent:
    """The fork's extent rule: ``base`` grown to the ink box plus the margin."""
    if not coords:
        return base
    xs = [c[0] for c in coords]
    ys = [c[1] for c in coords]
    return (
        min(base[0], min(xs) - INK_MARGIN_UNITS),
        min(base[1], min(ys) - INK_MARGIN_UNITS),
        max(base[2], max(xs) + INK_MARGIN_UNITS),
        max(base[3], max(ys) + INK_MARGIN_UNITS),
    )


def _canvas_px(extent: Extent, px_per_unit: float) -> tuple[int, int]:
    """Pixel size of ``extent`` at ``px_per_unit``, refusing anything over the canvas bound."""
    width = max(1, round((extent[2] - extent[0]) * px_per_unit))
    height = max(1, round((extent[3] - extent[1]) * px_per_unit))
    if max(width, height) > MAX_CANVAS_SIDE_PX or width * height > MAX_CANVAS_PIXELS:
        raise InvalidArgumentError(
            f"page is too large to render ({width} x {height} px)",
            hint="render at a smaller scale",
        )
    return width, height


def _render_notebook(path: Path, ink: _Ink, scale: float) -> RenderResult:
    paper_w, paper_h = ink.paper
    extent = _union_with_ink((-paper_w / 2, 0.0, paper_w / 2, paper_h), ink.coords)
    _canvas_px(extent, PX_PER_UNIT * scale)  # bound before the fork rasterises anything
    try:
        out = _fork.render_rm_file_extent_png(
            path, _BACKGROUND, PX_PER_UNIT * scale, INK_MARGIN_UNITS
        )
    except Exception as exc:
        raise InvalidArgumentError(f"page could not be rendered ({type(exc).__name__})") from None
    if out is None:
        raise InvalidArgumentError("page could not be rendered")
    png, (x0, y0, w, h) = out
    width, height = _png_size(png)
    return RenderResult(png, width, height, (float(x0), float(y0), float(x0 + w), float(y0 + h)))


def _render_annotated(pdf_bytes: bytes, ink: _Ink, scale: float) -> RenderResult:
    with _PDFIUM_LOCK, _open_pdf(pdf_bytes) as pdf:
        if len(pdf) != 1:
            raise InvalidArgumentError(
                "pdf_page must be a single-page PDF", hint="cut the page out with extract_pdf_page"
            )
        page = _rasterise_pdf_page(pdf, 0, ink, scale)
    return _compose(page, ink, scale)


@dataclass(frozen=True)
class _PdfRaster:
    image: Image.Image
    page_rect: Extent  # the PDF page in units, top centre at the origin
    extent: Extent  # the canvas: page_rect grown to the ink


def _rasterise_pdf_page(pdf: pdfium.PdfDocument, index: int, ink: _Ink, scale: float) -> _PdfRaster:
    """Rasterise page ``index`` at 226/72 units per point. The caller holds the PDFium lock.

    The canvas is bounded before PDFium allocates anything; the raster is resized to the exact
    pixel size of the page rectangle so it lines up with the ink to the pixel.
    """
    px_per_unit = PX_PER_UNIT * scale
    try:
        page = pdf[index]
    except pdfium.PdfiumError:
        raise InvalidArgumentError("PDF page could not be loaded") from None
    try:
        w_pt, h_pt = (float(v) for v in page.get_size())
        if not (math.isfinite(w_pt) and math.isfinite(h_pt) and w_pt > 0 and h_pt > 0):
            raise InvalidArgumentError("PDF page has an invalid size")
        w_units, h_units = w_pt * UNITS_PER_PT, h_pt * UNITS_PER_PT
        page_rect = (-w_units / 2, 0.0, w_units / 2, h_units)
        extent = _union_with_ink(page_rect, ink.coords)
        _canvas_px(extent, px_per_unit)
        page_px = _canvas_px(page_rect, px_per_unit)
        bitmap = page.render(scale=px_per_unit * UNITS_PER_PT)
        image = bitmap.to_pil().convert("RGB")
    except pdfium.PdfiumError:
        raise InvalidArgumentError("PDF page could not be rendered") from None
    finally:
        page.close()
    if image.size != page_px:
        image = image.resize(page_px, Image.Resampling.LANCZOS)
    return _PdfRaster(image=image, page_rect=page_rect, extent=extent)


def _compose(page: _PdfRaster, ink: _Ink, scale: float) -> RenderResult:
    """Place the page raster on a white canvas covering the extent and draw the ink over it."""
    px_per_unit = PX_PER_UNIT * scale
    width, height = _canvas_px(page.extent, px_per_unit)
    canvas = Image.new("RGB", (width, height), _WHITE)
    offset = (
        round((page.page_rect[0] - page.extent[0]) * px_per_unit),
        round((page.page_rect[1] - page.extent[1]) * px_per_unit),
    )
    canvas.paste(page.image, offset)
    canvas = _overlay_ink(canvas, ink.elements, page.extent)
    return RenderResult(_encode(canvas), width, height, page.extent)


def _overlay_ink(canvas: Image.Image, elements: list[str], extent: Extent) -> Image.Image:
    """Draw ink over a page raster.

    Highlighter and shader strokes multiply with the page (PDF text stays readable through
    them, as on the device); everything else is drawn over it. The SVG's own multiply only
    blends within the ink layer, so the two kinds are rasterised separately.
    """
    multiply = [e for e in elements if _MULTIPLY in e]
    cover = [e for e in elements if _MULTIPLY not in e]
    if multiply:
        layer = _rasterise_ink(multiply, extent, canvas.size)
        white = Image.new("RGBA", canvas.size, (*_WHITE, 255))
        canvas = ImageChops.multiply(canvas, Image.alpha_composite(white, layer).convert("RGB"))
    if cover:
        layer = _rasterise_ink(cover, extent, canvas.size)
        canvas = Image.alpha_composite(canvas.convert("RGBA"), layer).convert("RGB")
    return canvas


def _rasterise_ink(elements: list[str], extent: Extent, size: tuple[int, int]) -> Image.Image:
    """Rasterise SVG ink in unit coordinates onto a transparent ``size`` layer covering ``extent``.

    resvg fits to the width and derives the height from the viewBox aspect, which can differ
    from ``size`` by a pixel of rounding; the layer is anchored top-left and cropped or padded.
    """
    x0, y0, x1, y1 = extent
    w, h = x1 - x0, y1 - y0
    svg = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<svg xmlns="http://www.w3.org/2000/svg" '
        f'viewBox="{x0:.1f} {y0:.1f} {w:.1f} {h:.1f}" width="{w:.1f}" height="{h:.1f}">'
        f"{''.join(elements)}</svg>"
    )
    try:
        png = resvg_py.svg_to_bytes(svg_string=svg, width=size[0], height=size[1])
        with Image.open(io.BytesIO(bytes(png))) as raw:
            ink = raw.convert("RGBA")
    except Exception as exc:
        raise InvalidArgumentError(f"ink could not be rendered ({type(exc).__name__})") from None
    if ink.size == size:
        return ink
    layer = Image.new("RGBA", size, (0, 0, 0, 0))
    layer.paste(ink, (0, 0))
    return layer


class _open_pdf:  # noqa: N801  (used like a function in ``with``)
    """Open untrusted PDF bytes with pypdfium2; parse failures become InvalidArgumentError."""

    def __init__(self, data: bytes) -> None:
        self._data = bytes(data)
        self._pdf: pdfium.PdfDocument | None = None

    def __enter__(self) -> pdfium.PdfDocument:
        try:
            self._pdf = pdfium.PdfDocument(self._data)
        except pdfium.PdfiumError:
            raise InvalidArgumentError(
                "PDF could not be opened", hint="it may be damaged or password-protected"
            ) from None
        return self._pdf

    def __exit__(self, *exc: object) -> None:
        if self._pdf is not None:
            self._pdf.close()


def _encode(image: Image.Image) -> bytes:
    """PNG-encode with Pillow's defaults and no metadata, as the fork does: deterministic."""
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


def _png_size(png: bytes) -> tuple[int, int]:
    if len(png) < 24 or not png.startswith(_PNG_SIGNATURE) or png[12:16] != b"IHDR":
        raise InvalidArgumentError("renderer returned an invalid PNG")
    width, height = struct.unpack(">II", png[16:24])
    return int(width), int(height)

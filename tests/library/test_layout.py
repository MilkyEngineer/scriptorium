from __future__ import annotations

import os
from pathlib import Path

import pytest

from scriptorium.errors import PathEscapeError, RefusedError
from scriptorium.library.layout import (
    DOC_EXTENSIONS,
    belongs_to,
    doc_entries,
    doc_file_name,
    doc_uuid,
    is_safe_name,
    is_sha256,
    is_uuid,
    page_name,
    resolve_inside,
    safe_join,
)

DOC = "00000000-0000-4000-8000-000000000001"
PAGE = "00000000-0000-4000-8000-000000000101"
HEXDOC = "abcdef00-0000-4000-8000-0000000000ab"  # has letters, for the case tests


@pytest.mark.parametrize(
    "name",
    [DOC, *(f"{DOC}.{ext}" for ext in DOC_EXTENSIONS), f"{DOC}/{PAGE}.rm", "-" * 36],
)
def test_allowlist_accepts(name: str) -> None:
    assert is_safe_name(name)


@pytest.mark.parametrize(
    "name",
    [
        "",
        ".",
        "..",
        f"../{DOC}",
        f"{DOC}/../{DOC}.content",
        f"/{DOC}.content",
        f"{DOC}.content/",
        HEXDOC.upper(),
        HEXDOC[:-1] + "B",
        DOC + "0",
        DOC[:-1],
        f"{DOC}x.content",
        f"{DOC}.content\n",
        f"{DOC}\n",
        f"{DOC}.content\x00",
        f"{DOC}\x00.content",
        f"{DOC}.txt",
        f"{DOC}.PDF",
        f"{DOC}.rm",
        f"{DOC}.content.pdf",
        f"{DOC}/{PAGE}",
        f"{DOC}/{PAGE}.metadata",
        f"{DOC}/{PAGE}.rm/x",
        f"{DOC}/{DOC}/{PAGE}.rm",
        f"{DOC}\\{PAGE}.rm",
        f"{DOC}/{HEXDOC.upper()}.rm",
        chr(0xFF10) * 36,  # fullwidth digits are not [0-9]
        chr(0x0660) * 36,  # Arabic-Indic digits likewise
        f"{DOC}.metadata-metadata.json",
    ],
)
def test_allowlist_refuses(name: str) -> None:
    assert not is_safe_name(name)


@pytest.mark.parametrize("value", [None, b"x", 1, [DOC]])
def test_allowlist_refuses_non_strings(value: object) -> None:
    assert not is_safe_name(value)
    assert not is_uuid(value)
    assert not is_sha256(value)


def test_uuid_and_sha() -> None:
    assert is_uuid(DOC)
    assert not is_uuid(DOC + "\n")
    assert is_sha256("a" * 64)
    assert not is_sha256("A" * 64)
    assert not is_sha256("a" * 63)
    assert not is_sha256("a" * 64 + "\n")


def test_doc_uuid_and_belongs_to() -> None:
    assert doc_uuid(f"{DOC}.content") == DOC
    assert doc_uuid(f"{DOC}/{PAGE}.rm") == DOC
    assert doc_uuid(DOC) == DOC
    with pytest.raises(RefusedError):
        doc_uuid("../etc/passwd")
    assert belongs_to(f"{DOC}/{PAGE}.rm", DOC)
    assert not belongs_to(f"{DOC}/{PAGE}.rm", PAGE)
    assert not belongs_to(f"{PAGE}.content", DOC)
    assert not belongs_to(f"{DOC}.content", "not-a-uuid")


def test_name_builders() -> None:
    assert doc_file_name(DOC, "pdf") == f"{DOC}.pdf"
    assert page_name(DOC, PAGE) == f"{DOC}/{PAGE}.rm"
    with pytest.raises(RefusedError):
        doc_file_name(DOC, "txt")
    with pytest.raises(RefusedError):
        page_name(DOC, "../x")
    with pytest.raises(RefusedError):
        doc_file_name("../x", "pdf")


def test_doc_entries() -> None:
    assert doc_entries(DOC) == (
        DOC,
        f"{DOC}.metadata",
        f"{DOC}.content",
        f"{DOC}.pagedata",
        f"{DOC}.pdf",
        f"{DOC}.epub",
    )
    with pytest.raises(RefusedError):
        doc_entries("..")


# ---- containment ------------------------------------------------------------------------------


@pytest.fixture
def root(tmp_path: Path) -> Path:
    r = tmp_path / "root"
    (r / "sub").mkdir(parents=True)
    (tmp_path / "outside").mkdir()
    (tmp_path / "outside" / "secret").write_text("x")
    return r


def test_resolve_inside_accepts_root_and_children(root: Path) -> None:
    assert resolve_inside(root, root) == Path(os.path.realpath(root))
    assert resolve_inside(root, root / "sub" / "new") == Path(os.path.realpath(root)) / "sub/new"


@pytest.mark.parametrize("rel", ["..", "../outside/secret", "sub/../../outside", "sub/../.."])
def test_resolve_inside_refuses_dotdot(root: Path, rel: str) -> None:
    with pytest.raises(PathEscapeError):
        resolve_inside(root, root / rel)


def test_resolve_inside_refuses_sibling_prefix(root: Path) -> None:
    sibling = root.parent / (root.name + "2")
    sibling.mkdir()
    with pytest.raises(PathEscapeError):
        resolve_inside(root, sibling / "x")


def test_resolve_inside_refuses_relative_and_nul(root: Path) -> None:
    with pytest.raises(PathEscapeError):
        resolve_inside(root, "sub/x")
    with pytest.raises(PathEscapeError):
        resolve_inside(root, str(root / "sub") + "\x00/x")
    with pytest.raises(PathEscapeError):
        resolve_inside(str(root) + "\x00", root)


def test_resolve_inside_follows_symlinks(root: Path) -> None:
    (root / "out").symlink_to(root.parent / "outside")
    (root / "out-file").symlink_to(root.parent / "outside" / "secret")
    (root / "in").symlink_to(root / "sub")
    with pytest.raises(PathEscapeError):
        resolve_inside(root, root / "out")
    with pytest.raises(PathEscapeError):
        resolve_inside(root, root / "out" / "secret")
    with pytest.raises(PathEscapeError):
        resolve_inside(root, root / "out-file")
    assert resolve_inside(root, root / "in" / "x") == Path(os.path.realpath(root)) / "sub/x"


def test_resolve_inside_judges_a_symlink_loop(root: Path) -> None:
    (root / "a").symlink_to(root / "b")
    (root / "b").symlink_to(root / "a")
    # realpath leaves a loop unresolved instead of raising; the result is still judged.
    result = resolve_inside(root, root / "a" / "x")
    assert result.is_relative_to(os.path.realpath(root))


def test_safe_join(root: Path) -> None:
    real = Path(os.path.realpath(root))
    assert safe_join(root, f"{DOC}.content") == real / f"{DOC}.content"
    assert safe_join(root, f"{DOC}/{PAGE}.rm") == real / DOC / f"{PAGE}.rm"
    assert safe_join(root, DOC, f"{DOC}.metadata") == real / DOC / f"{DOC}.metadata"


@pytest.mark.parametrize(
    "parts",
    [
        (),
        ("..",),
        ("/etc/passwd",),
        (DOC, ".."),
        (DOC, "/etc"),
        (f"{DOC}.content\x00",),
        (HEXDOC.upper(),),
        ("sub",),
    ],
)
def test_safe_join_refuses_unsafe_parts(root: Path, parts: tuple[str, ...]) -> None:
    with pytest.raises(RefusedError):
        safe_join(root, *parts)


def test_safe_join_refuses_symlink_out(root: Path) -> None:
    (root / DOC).symlink_to(root.parent / "outside")
    with pytest.raises(PathEscapeError):
        safe_join(root, f"{DOC}/{PAGE}.rm")
    (root / f"{DOC}.content").symlink_to(root.parent / "outside" / "secret")
    with pytest.raises(PathEscapeError):
        safe_join(root, f"{DOC}.content")

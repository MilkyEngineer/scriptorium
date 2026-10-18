from __future__ import annotations

import os
from pathlib import Path

import pytest

from scriptorium.errors import PathEscapeError, RefusedError
from scriptorium.library.layout import (
    DOC_EXTENSIONS,
    DOC_SUBDIRS,
    PAGE_FILES,
    SAFE_NAME_PATTERN,
    belongs_to,
    doc_dirs,
    doc_entries,
    doc_file_name,
    doc_uuid,
    is_doc_entry,
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

#: The per-page shapes besides the ink, one example each.
LAYERS = f"{DOC}/{PAGE}-metadata.json"
HIGHLIGHTS = f"{DOC}.highlights/{PAGE}.json"
THUMB_PNG = f"{DOC}.thumbnails/{PAGE}.png"
THUMB_JPG = f"{DOC}.thumbnails/{PAGE}.jpg"
TEXTCONV = f"{DOC}.textconversion/{PAGE}.json"
NEW_SHAPES = [LAYERS, HIGHLIGHTS, THUMB_PNG, THUMB_JPG, TEXTCONV]

#: The exact pattern recorded in Architecture §11.0 (matched with re.fullmatch).
ARCHITECTURE_PATTERN = (
    r"(?P<doc>[0-9a-f-]{36})(?:\.(?:metadata|content|pagedata|pdf|epub)"
    r"|/[0-9a-f-]{36}(?:\.rm|-metadata\.json)|\.highlights/[0-9a-f-]{36}\.json"
    r"|\.thumbnails/[0-9a-f-]{36}\.(?:png|jpg)|\.textconversion/[0-9a-f-]{36}\.json)?"
)


@pytest.mark.parametrize(
    "name",
    [
        DOC,
        *(f"{DOC}.{ext}" for ext in DOC_EXTENSIONS),
        f"{DOC}/{PAGE}.rm",
        "-" * 36,
        *NEW_SHAPES,
        f"{HEXDOC}.thumbnails/{HEXDOC}.jpg",
    ],
)
def test_allowlist_accepts(name: str) -> None:
    assert is_safe_name(name)


def test_the_pattern_is_the_one_in_the_architecture() -> None:
    assert SAFE_NAME_PATTERN == ARCHITECTURE_PATTERN


def test_the_pattern_and_the_tables_agree() -> None:
    accepted = {f"{DOC}.{ext}" for ext in DOC_EXTENSIONS}
    accepted |= {f"{DOC}{d}/{PAGE}{suffix}" for d, suffix in PAGE_FILES.values()}
    assert all(is_safe_name(name) for name in accepted)
    assert {d for d, _ in PAGE_FILES.values()} == {"", *(f".{s}" for s in DOC_SUBDIRS)}
    # Every suffix is accepted only in its own directory.
    for directory in {d for d, _ in PAGE_FILES.values()}:
        for suffix in {s for _, s in PAGE_FILES.values()}:
            name = f"{DOC}{directory}/{PAGE}{suffix}"
            assert is_safe_name(name) == (name in accepted), name


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
        # the new shapes: bare directories, wrong suffix or directory, case, extra levels
        *(f"{DOC}.{sub}" for sub in DOC_SUBDIRS),
        *(f"{DOC}.{sub}/" for sub in DOC_SUBDIRS),
        f"{DOC}/{PAGE}.json",
        f"{DOC}/{PAGE}-metadata.JSON",
        f"{DOC}/{PAGE}-metadata.jsonx",
        f"{DOC}/{PAGE}metadata.json",
        f"{DOC}/{PAGE}-metadata.json\n",
        f"{DOC}.highlights/{PAGE}.png",
        f"{DOC}.highlights/{PAGE}.rm",
        f"{DOC}.highlights/{PAGE}-metadata.json",
        f"{DOC}.thumbnails/{PAGE}.jpeg",
        f"{DOC}.thumbnails/{PAGE}.PNG",
        f"{DOC}.thumbnails/{PAGE}.gif",
        f"{DOC}.thumbnails/{PAGE}.json",
        f"{DOC}.textconversion/{PAGE}.rm",
        f"{DOC}.textconversion/{PAGE}.txt",
        f"{DOC}.Highlights/{PAGE}.json",
        f"{DOC}.highlight/{PAGE}.json",
        f"{DOC}.highlightsx/{PAGE}.json",  # sibling-prefix directory names
        f"{DOC}.highlights.evil/{PAGE}.json",
        f"{DOC}.thumbnails2/{PAGE}.png",
        f"{DOC}.cache/{PAGE}.png",
        f"{DOC}.local",
        f"{DOC}.highlights//{PAGE}.json",
        f"{DOC}.highlights/./{PAGE}.json",
        f"{DOC}.highlights/../{DOC}.content",
        f"{DOC}.thumbnails/../../{PAGE}.png",
        f"{DOC}.thumbnails/{PAGE}.png/..",
        f"{DOC}.thumbnails/{PAGE}/{PAGE}.png",
        f"{DOC}.highlights/{DOC}.highlights/{PAGE}.json",
        f"{DOC}.thumbnails\\{PAGE}.png",
        f"{DOC}.thumbnails/{HEXDOC.upper()}.png",
        f"{DOC}.thumbnails/{chr(0xFF10) * 36}.png",
        f"{DOC}.textconversion/{PAGE}.json\x00",
        f"/{DOC}.textconversion/{PAGE}.json",
        f"{DOC}.textconversion/{PAGE[:-1]}.json",
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


@pytest.mark.parametrize("name", NEW_SHAPES)
def test_doc_uuid_and_belongs_to_for_the_new_shapes(name: str) -> None:
    assert doc_uuid(name) == DOC
    assert belongs_to(name, DOC)
    assert not belongs_to(name, PAGE)  # the page id is never taken for the document


def test_name_builders() -> None:
    assert doc_file_name(DOC, "pdf") == f"{DOC}.pdf"
    assert page_name(DOC, PAGE) == f"{DOC}/{PAGE}.rm"
    with pytest.raises(RefusedError):
        doc_file_name(DOC, "txt")
    with pytest.raises(RefusedError):
        page_name(DOC, "../x")
    with pytest.raises(RefusedError):
        doc_file_name("../x", "pdf")


def test_page_name_for_every_kind() -> None:
    assert page_name(DOC, PAGE, "rm") == f"{DOC}/{PAGE}.rm"
    assert page_name(DOC, PAGE, "page_metadata") == LAYERS
    assert page_name(DOC, PAGE, "highlights") == HIGHLIGHTS
    assert page_name(DOC, PAGE, "thumbnail_png") == THUMB_PNG
    assert page_name(DOC, PAGE, "thumbnail_jpg") == THUMB_JPG
    assert page_name(DOC, PAGE, "textconversion") == TEXTCONV
    for kind in PAGE_FILES:
        with pytest.raises(RefusedError):
            page_name(DOC, "../x", kind)
        with pytest.raises(RefusedError):
            page_name(f"{DOC}.highlights", PAGE, kind)
    with pytest.raises(RefusedError):
        page_name(DOC, PAGE, "thumbnail_gif")  # type: ignore[arg-type]


def test_doc_entries() -> None:
    assert doc_dirs(DOC) == (
        DOC,
        f"{DOC}.highlights",
        f"{DOC}.thumbnails",
        f"{DOC}.textconversion",
    )
    assert doc_entries(DOC) == (
        DOC,
        f"{DOC}.metadata",
        f"{DOC}.content",
        f"{DOC}.pagedata",
        f"{DOC}.pdf",
        f"{DOC}.epub",
        f"{DOC}.highlights",
        f"{DOC}.thumbnails",
        f"{DOC}.textconversion",
    )
    # Every allowlisted name lives in exactly one of the document's top-level entries.
    for name in [*(f"{DOC}.{ext}" for ext in DOC_EXTENSIONS), f"{DOC}/{PAGE}.rm", *NEW_SHAPES]:
        assert name.split("/", 1)[0] in doc_entries(DOC), name
    with pytest.raises(RefusedError):
        doc_entries("..")
    with pytest.raises(RefusedError):
        doc_dirs(f"{DOC}.highlights")


def test_is_doc_entry() -> None:
    assert all(is_doc_entry(entry) for entry in doc_entries(DOC))
    for name in [
        f"{DOC}.highlights/{PAGE}.json",
        f"{DOC}.highlightsx",
        f"{DOC}.highlights\n",
        f"{DOC}.cache",
        f"{DOC}.",
        f"{DOC}..",
        f"{DOC}.thumbnails.png",
        f"{HEXDOC.upper()}.thumbnails",
        ".highlights",
        "",
        None,
        1,
    ]:
        assert not is_doc_entry(name), name


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
    for name in NEW_SHAPES:
        assert safe_join(root, name) == real.joinpath(*name.split("/"))
    assert safe_join(root, f"{DOC}.thumbnails") == real / f"{DOC}.thumbnails"


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
        # dot-dot and sibling prefixes through the new directory shapes
        (f"{DOC}.highlights", ".."),
        (f"{DOC}.highlights/..",),
        (f"{DOC}.highlights/../..",),
        (f"{DOC}.thumbnails/../../outside/secret",),
        (f"{DOC}.thumbnails/{PAGE}.png/../../..",),
        (f"{DOC}.thumbnails", f"{PAGE}.png"),  # a per-page file is one part, never split
        (f"{DOC}.thumbnails", "../outside"),
        (f"{DOC}.highlightsx",),
        (f"{DOC}.highlightsx/{PAGE}.json",),
        (f"{DOC}.thumbnails2/{PAGE}.png",),
        (f"{DOC}.textconversion\n",),
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


@pytest.mark.parametrize("name", NEW_SHAPES)
def test_safe_join_refuses_symlinks_out_through_the_new_directories(root: Path, name: str) -> None:
    directory = name.split("/", 1)[0]
    (root / directory).symlink_to(root.parent / "outside")
    with pytest.raises(PathEscapeError):
        safe_join(root, name)
    with pytest.raises(PathEscapeError):
        safe_join(root, directory)


@pytest.mark.parametrize("name", NEW_SHAPES)
def test_safe_join_refuses_a_sibling_prefix_through_the_new_directories(
    root: Path, name: str
) -> None:
    # root is .../root; a link to .../root2 shares the prefix but is outside.
    directory = name.split("/", 1)[0]
    sibling = root.parent / (root.name + "2")
    sibling.mkdir()
    (root / directory).symlink_to(sibling)
    with pytest.raises(PathEscapeError):
        safe_join(root, name)


@pytest.mark.parametrize("name", NEW_SHAPES)
def test_safe_join_refuses_a_file_symlinked_out_of_a_new_directory(root: Path, name: str) -> None:
    directory, leaf = name.split("/", 1)
    (root / directory).mkdir()
    (root / directory / leaf).symlink_to(root.parent / "outside" / "secret")
    with pytest.raises(PathEscapeError):
        safe_join(root, name)

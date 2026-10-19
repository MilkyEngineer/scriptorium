from importlib.metadata import version

import scriptorium


def test_version_matches_the_installed_distribution() -> None:
    # pyproject.toml and scriptorium/__init__.py both carry the version; the release
    # workflow checks the tag against them, and they must not drift apart.
    assert scriptorium.__version__ == version("scriptorium")

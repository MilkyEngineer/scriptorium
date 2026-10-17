import scriptorium


def test_version_is_dev() -> None:
    assert scriptorium.__version__.endswith(".dev0")

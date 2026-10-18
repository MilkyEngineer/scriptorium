import pytest

from scriptorium.errors import ERROR_CODES, AmbiguousError, PathEscapeError, ScriptoriumError


def test_every_subclass_uses_a_known_code() -> None:
    for cls in ScriptoriumError.__subclasses__():
        assert cls.code in ERROR_CODES


def test_unknown_code_rejected() -> None:
    with pytest.raises(ValueError):
        ScriptoriumError("x", code="nope")


def test_as_dict_and_hint() -> None:
    assert ScriptoriumError("m").as_dict() == {"code": "internal", "message": "m"}
    assert AmbiguousError("m", ["a", "b"], hint="h").as_dict() == {
        "code": "ambiguous",
        "message": "m",
        "hint": "h",
    }
    assert PathEscapeError("m").code == "refused"

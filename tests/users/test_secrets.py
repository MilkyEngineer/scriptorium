from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from scriptorium.errors import InvalidArgumentError, ScriptoriumError
from scriptorium.users import secrets as secrets_mod
from scriptorium.users.secrets import SECRETS_FILE, SecretBox
from tests.conftest import make_settings

UID = "0123456789abcdef"
OTHER_UID = "fedcba9876543210"
TOKEN = b"device-token-for-tests-only"


def key() -> str:
    return Fernet.generate_key().decode()


def box(tmp_path: Path, *keys: str, uid: str = UID) -> SecretBox:
    return SecretBox(make_settings(data_dir=tmp_path, master_keys=",".join(keys)), uid)


def secrets_file(tmp_path: Path, uid: str = UID) -> Path:
    return tmp_path / "users" / uid / SECRETS_FILE


def test_round_trip_and_forget(tmp_path: Path) -> None:
    b = box(tmp_path, key())
    assert b.open("device_token") is None
    b.seal("device_token", TOKEN)
    b.seal("claude_api_key", b"sk-test")
    assert b.open("device_token") == TOKEN
    assert b.open("claude_api_key") == b"sk-test"
    b.seal("device_token", b"replaced")
    assert b.open("device_token") == b"replaced"
    b.forget("device_token")
    b.forget("device_token")  # idempotent
    assert b.open("device_token") is None
    assert b.open("claude_api_key") == b"sk-test"


def test_file_is_private_json_without_plaintext(tmp_path: Path) -> None:
    b = box(tmp_path, key())
    b.seal("device_token:01J9Z8Y7X6W5V4T3S2R1Q0P0N0", TOKEN)
    path = secrets_file(tmp_path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    raw = path.read_bytes()
    assert TOKEN not in raw
    doc = json.loads(raw)
    assert doc["v"] == 1 and set(doc["secrets"]) == {"device_token:01J9Z8Y7X6W5V4T3S2R1Q0P0N0"}
    leftovers = [n for n in os.listdir(path.parent) if n not in (SECRETS_FILE, ".secrets.lock")]
    assert leftovers == []


def test_rotation_seals_with_the_first_key_and_opens_with_any(tmp_path: Path) -> None:
    old, new = key(), key()
    box(tmp_path, old).seal("device_token", TOKEN)

    rotated = box(tmp_path, new, old)
    assert rotated.open("device_token") == TOKEN  # old token, new key list
    rotated.seal("claude_api_key", b"sk-test")
    token = json.loads(secrets_file(tmp_path).read_bytes())["secrets"]["claude_api_key"]
    Fernet(new.encode()).decrypt(token.encode())  # sealed with the first key

    new_only = box(tmp_path, new)
    assert new_only.open("claude_api_key") == b"sk-test"
    with pytest.raises(ScriptoriumError) as info:  # the old key was removed too early
        new_only.open("device_token")
    assert info.value.code == "internal"
    assert TOKEN.decode() not in str(info.value)


def test_a_token_is_bound_to_its_user_and_name(tmp_path: Path) -> None:
    k = key()
    box(tmp_path, k).seal("device_token", TOKEN)
    token = json.loads(secrets_file(tmp_path).read_bytes())["secrets"]["device_token"]

    other = box(tmp_path, k, uid=OTHER_UID)
    other.seal("placeholder", b"x")
    path = secrets_file(tmp_path, OTHER_UID)
    doc = json.loads(path.read_bytes())
    doc["secrets"]["device_token"] = token  # copied into another user's file
    path.write_text(json.dumps(doc))
    with pytest.raises(ScriptoriumError):
        other.open("device_token")

    mine = box(tmp_path, k)
    path = secrets_file(tmp_path)
    doc = json.loads(path.read_bytes())
    doc["secrets"]["claude_api_key"] = token  # moved under another name
    path.write_text(json.dumps(doc))
    with pytest.raises(ScriptoriumError):
        mine.open("claude_api_key")


@pytest.mark.parametrize("name", ["", "../x", "a/b", "a b", "x" * 65, "name\n", ".hidden", "é"])
def test_bad_names_are_refused(tmp_path: Path, name: str) -> None:
    b = box(tmp_path, key())
    for call in (lambda: b.seal(name, b"x"), lambda: b.open(name), lambda: b.forget(name)):
        with pytest.raises(InvalidArgumentError):
            call()


def test_bad_values_are_refused(tmp_path: Path) -> None:
    b = box(tmp_path, key())
    with pytest.raises(InvalidArgumentError):
        b.seal("x", "a string")  # type: ignore[arg-type]
    with pytest.raises(InvalidArgumentError):
        b.seal("x", b"x" * (64 * 1024 + 1))


def test_bad_uid_is_refused(tmp_path: Path) -> None:
    with pytest.raises(InvalidArgumentError):
        box(tmp_path, key(), uid="../../etc")


@pytest.mark.parametrize(
    "content",
    [b"not json", b"[]", b'{"v": 2, "secrets": {}}', b'{"v": 1, "secrets": {"a": 1}}'],
)
def test_a_damaged_file_is_an_error_not_an_empty_store(tmp_path: Path, content: bytes) -> None:
    b = box(tmp_path, key())
    b.seal("x", b"y")
    secrets_file(tmp_path).write_bytes(content)
    with pytest.raises(ScriptoriumError):
        b.open("x")
    with pytest.raises(ScriptoriumError):  # and it is not silently overwritten either
        b.seal("x", b"z")


def test_a_symlink_in_place_of_the_file_is_refused(tmp_path: Path) -> None:
    b = box(tmp_path, key())
    b.seal("x", b"y")
    path = secrets_file(tmp_path)
    target = tmp_path / "elsewhere.json"
    path.rename(target)
    path.symlink_to(target)
    with pytest.raises(ScriptoriumError):
        b.open("x")


def test_a_failed_write_leaves_the_old_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    b = box(tmp_path, key())
    b.seal("x", b"old")

    def boom(src: object, dst: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(secrets_mod.os, "replace", boom)
    with pytest.raises(OSError):
        b.seal("x", b"new")
    monkeypatch.undo()
    assert b.open("x") == b"old"
    names = os.listdir(secrets_file(tmp_path).parent)
    assert not [n for n in names if n.endswith(".tmp")]

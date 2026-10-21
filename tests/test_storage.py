"""Tests for the credentials storage package. subprocess.run is mocked
inside _keychain -- no keychain is touched, no secret leaves the process
(fallback tests redirect auth.json into tmp_path). sys.platform is
pinned per test so the darwin and windows paths run anywhere; the real
CredReadW success path cannot run off-Windows, so Windows coverage is
the struct, the not-found paths via a fake advapi32, and the write call
shape. The `pin` fixture also pins keychain availability so keychain
tests pass on hosts without the tools; file-fallback tests force the
backend off or exercise the real shutil.which detection.
"""

import ctypes
import json
import os
import sys

import pytest

from knowledge_builder import storage, storage_cli
from knowledge_builder.storage import _filestore, _keychain


class _Proc:
    """Stand-in for the object subprocess.run returns."""

    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class _Runner:
    """Fake subprocess.run: records (argv, stdin) and replays canned _Proc
    results; Exception instances propagate (FileNotFoundError, ...)."""

    def __init__(self, results=()):
        self.results = list(results)
        self.calls = []

    def __call__(self, argv, **kwargs):
        assert self.results, f"unexpected subprocess call: {argv}"
        self.calls.append((list(argv), kwargs.get("input")))
        item = self.results.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture
def run(monkeypatch):
    """install(*results) -> _Runner; every subprocess call goes through it."""
    def install(*results):
        runner = _Runner(results)
        monkeypatch.setattr(_keychain.subprocess, "run", runner)
        return runner
    return install


@pytest.fixture
def pin(monkeypatch):
    """Pin sys.platform; keychain platforms are also pinned available so
    the tests do not depend on the host's tools."""
    def pin_platform(name):
        monkeypatch.setattr(sys, "platform", name)
        if name in ("linux", "darwin", "win32"):
            monkeypatch.setattr(_keychain, "_keychain_available", lambda: True)
    return pin_platform


@pytest.fixture
def authfile(tmp_path, monkeypatch):
    """auth.json redirected into a fresh tmp directory; returns its Path."""
    path = tmp_path / "kb-ai" / "auth.json"
    monkeypatch.setattr(_filestore, "auth_file_path", lambda: str(path))
    return path


@pytest.fixture
def fallback(monkeypatch, authfile):
    """Force the auth.json backend on, host tools aside."""
    monkeypatch.setattr(_keychain, "_keychain_available", lambda: False)
    return authfile


# ------------------------------------------------------------------ linux

def test_linux_get_found(run, pin):
    pin("linux")
    rec = run(_Proc(stdout="sek ret"))
    assert storage.get_secret("kb:x") == "sek ret"
    assert rec.calls == [(["secret-tool", "lookup", "service", "kb:x"], None)]


def test_linux_get_missing_is_none(run, pin):
    pin("linux")
    run(_Proc(returncode=1))  # quiet miss: rc 1, empty stderr
    assert storage.get_secret("kb:x") is None


def test_linux_get_unreachable_store_raises(run, pin):
    pin("linux")
    run(_Proc(returncode=1, stderr="secret-tool: Unable to create socket"))
    with pytest.raises(storage.SecretStoreError, match="Unable to create socket"):
        storage.get_secret("kb:x")


def test_linux_missing_binary_names_the_package(run, pin):
    pin("linux")
    run(FileNotFoundError(2, "No such file or directory"))
    with pytest.raises(storage.SecretStoreError, match="libsecret-tools"):
        storage.get_secret("kb:x")


def test_linux_set_clears_then_pipes_secret_never_argv(run, pin):
    pin("linux")
    rec = run(_Proc(), _Proc())
    storage.set_secret("kb:x", "sek ret", label="My Label")
    clear_argv, clear_stdin = rec.calls[0]
    store_argv, store_stdin = rec.calls[1]
    assert clear_argv == ["secret-tool", "clear", "service", "kb:x"]
    assert clear_stdin is None
    assert store_argv == ["secret-tool", "store", "--label=My Label", "service", "kb:x"]
    assert store_stdin == "sek ret"
    assert "sek ret" not in store_argv


def test_linux_set_store_failure_raises(run, pin):
    pin("linux")
    run(_Proc(), _Proc(returncode=1, stderr="boom"))
    with pytest.raises(storage.SecretStoreError, match="boom"):
        storage.set_secret("kb:x", "sekret")


def test_linux_delete_found(run, pin):
    pin("linux")
    rec = run(_Proc(stdout="old"), _Proc())
    assert storage.delete_secret("kb:x") is True
    assert rec.calls[1][0] == ["secret-tool", "clear", "service", "kb:x"]


def test_linux_delete_missing(run, pin):
    pin("linux")
    rec = run(_Proc(returncode=1))
    assert storage.delete_secret("kb:x") is False
    assert len(rec.calls) == 1  # no clear without something to clear


# ------------------------------------------------------------------ macOS

def test_mac_get_found(run, pin):
    pin("darwin")
    rec = run(_Proc(stdout="sekret\n"))
    assert storage.get_secret("kb:x") == "sekret"
    assert rec.calls == [(["security", "find-generic-password", "-s", "kb:x", "-w"], None)]


def test_mac_get_strips_exactly_one_newline(run, pin):
    pin("darwin")
    run(_Proc(stdout="sek\nret\n"))
    assert storage.get_secret("kb:x") == "sek\nret"


def test_mac_get_missing(run, pin):
    pin("darwin")
    run(_Proc(returncode=_keychain._MAC_NOT_FOUND))
    assert storage.get_secret("kb:x") is None


def test_mac_get_error_raises(run, pin):
    pin("darwin")
    run(_Proc(returncode=51, stderr="keychain error"))
    with pytest.raises(storage.SecretStoreError, match="find-generic-password"):
        storage.get_secret("kb:x")


def test_mac_set_feeds_interactive_command_and_verifies(run, pin):
    pin("darwin")
    rec = run(_Proc(), _Proc(stdout="sekret\n"))
    storage.set_secret("kb:x", "sekret", label="KBX", account="acc")
    add_argv, add_stdin = rec.calls[0]
    assert add_argv == ["security", "-i"]
    assert add_stdin == "add-generic-password -U -a acc -s kb:x -l KBX -w sekret\n"
    assert "sekret" not in add_argv
    assert rec.calls[1] == (
        ["security", "find-generic-password", "-a", "acc", "-s", "kb:x", "-w"], None)


def test_mac_set_defaults_label_and_account(run, pin):
    pin("darwin")
    rec = run(_Proc(), _Proc(stdout="s\n"))
    storage.set_secret("kb:x", "s")
    assert "-a knowledge-builder -s kb:x -l kb:x" in rec.calls[0][1]


def test_mac_set_verify_mismatch_raises_without_leaking(run, pin):
    pin("darwin")
    run(_Proc(), _Proc(returncode=44))  # add "succeeded", find misses
    with pytest.raises(storage.SecretStoreError) as excinfo:
        storage.set_secret("kb:x", "sekret")
    assert "sekret" not in str(excinfo.value)
    assert "did not store" in str(excinfo.value)


def test_mac_set_rejects_whitespace_payload(run, pin):
    pin("darwin")
    rec = run()  # any subprocess call would fail the test
    with pytest.raises(ValueError, match="whitespace"):
        storage.set_secret("kb:x", "two words")
    with pytest.raises(ValueError, match="whitespace"):
        storage.set_secret("kb:x", "s", label="my label")
    with pytest.raises(ValueError, match="whitespace"):
        storage.set_secret("kb:x", "s", account="the account")
    assert not rec.calls


def test_mac_delete_found(run, pin):
    pin("darwin")
    rec = run(_Proc())
    assert storage.delete_secret("kb:x") is True
    assert rec.calls == [(["security", "delete-generic-password", "-s", "kb:x"], None)]


def test_mac_delete_missing(run, pin):
    pin("darwin")
    run(_Proc(returncode=44))
    assert storage.delete_secret("kb:x") is False


# ----------------------------------------------------------------- windows

def test_windows_struct_matches_credentialw():
    fields = [name for name, _ in _keychain._win_credential_struct()._fields_]
    assert fields == [
        "Flags", "Type", "TargetName", "Comment", "LastWritten",
        "CredentialBlobSize", "CredentialBlob", "Persist",
        "AttributeCount", "Attributes", "TargetAlias", "UserName",
    ]


def test_windows_backend_refused_off_windows(run, pin):
    pin("win32")
    run()
    with pytest.raises(storage.SecretStoreError, match="requires Windows"):
        storage.get_secret("kb:x")


def test_windows_write_with_fake_advapi32(run, pin, monkeypatch):
    pin("win32")

    class _Dll:
        def __init__(self):
            self.written = []

        def CredWriteW(self, cred, flags):
            self.written.append(flags)
            return 1

    dll = _Dll()
    monkeypatch.setattr(_keychain, "_win_advapi32", lambda: dll)
    storage.set_secret("kb:x", "sekret", label="L", account="acc")
    assert dll.written == [0]


def test_windows_get_not_found_with_fake_advapi32(run, pin, monkeypatch):
    pin("win32")

    class _Dll:
        def CredReadW(self, target, ctype, flags, byref):
            return 0

    monkeypatch.setattr(_keychain, "_win_advapi32", lambda: _Dll())
    # ctypes.get_last_error exists only on Windows; create it for this test
    monkeypatch.setattr(ctypes, "get_last_error",
                        lambda: _keychain._WIN_ERROR_NOT_FOUND, raising=False)
    assert storage.get_secret("kb:x") is None


def test_windows_delete_not_found_with_fake_advapi32(run, pin, monkeypatch):
    pin("win32")

    class _Dll:
        def CredDeleteW(self, target, ctype, flags):
            return 0

    monkeypatch.setattr(_keychain, "_win_advapi32", lambda: _Dll())
    monkeypatch.setattr(ctypes, "get_last_error",
                        lambda: _keychain._WIN_ERROR_NOT_FOUND, raising=False)
    assert storage.delete_secret("kb:x") is False


def test_windows_blob_cap_is_value_error(run, pin):
    pin("win32")
    run()  # ValueError fires before any I/O
    with pytest.raises(ValueError, match="1280"):
        storage.set_secret("kb:x", "x" * 2000)


# ------------------------------------------------------- validation + dispatch

@pytest.mark.parametrize("bad", ["", "a\nb", "a\rb", "a\x00b", None, 42])
def test_bad_names_are_value_error(run, bad):
    rec = run()
    for call in (storage.get_secret, storage.delete_secret):
        with pytest.raises(ValueError):
            call(bad)
    with pytest.raises(ValueError):
        storage.set_secret(bad, "sekret")
    assert not rec.calls


@pytest.mark.parametrize("bad", ["", None, 42, "a\x00b", "a\nb", "a\rb"])
def test_bad_secrets_are_value_error(run, bad):
    rec = run()
    with pytest.raises(ValueError):
        storage.set_secret("kb:x", bad)
    assert not rec.calls


def test_bad_label_and_account_are_value_error(run):
    rec = run()
    with pytest.raises(ValueError):
        storage.set_secret("kb:x", "s", label="a\nb")
    with pytest.raises(ValueError):
        storage.set_secret("kb:x", "s", account="a\x00b")
    assert not rec.calls


def test_backend_table_rejects_unknown_platform(pin):
    # safety net only: the public API routes unknown platforms to the
    # file store before _backend() is ever consulted
    pin("freebsd")
    with pytest.raises(storage.SecretStoreError, match="no OS credentials backend"):
        _keychain._backend()


# ---------------------------------------------------------- fallback store

def test_auth_file_path_linux_xdg_default(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.setenv("HOME", "/home/user")
    assert _filestore.auth_file_path() == \
        "/home/user/.local/share/knowledge-builder-ai/auth.json"


def test_auth_file_path_linux_xdg_data_home_wins(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("XDG_DATA_HOME", "/xdg/data")
    assert _filestore.auth_file_path() == "/xdg/data/knowledge-builder-ai/auth.json"


def test_auth_file_path_linux_relative_xdg_ignored(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("XDG_DATA_HOME", "relative/path")
    monkeypatch.setenv("HOME", "/home/user")
    assert _filestore.auth_file_path() == \
        "/home/user/.local/share/knowledge-builder-ai/auth.json"


def test_auth_file_path_darwin(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setenv("HOME", "/Users/u")
    assert _filestore.auth_file_path() == \
        "/Users/u/Library/Application Support/knowledge-builder-ai/auth.json"


def test_auth_file_path_windows_localappdata(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", "C:/Users/u/AppData/Local")
    assert _filestore.auth_file_path() == \
        "C:/Users/u/AppData/Local/knowledge-builder-ai/auth.json"


def test_auth_file_path_windows_home_fallback(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    assert _filestore.auth_file_path() == os.path.join(
        os.path.expanduser("~"), "AppData", "Local",
        "knowledge-builder-ai", "auth.json")


def test_linux_missing_secret_tool_uses_file(run, monkeypatch, authfile):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(_keychain.shutil, "which", lambda name: None)
    rec = run()  # any subprocess call would fail the test
    storage.set_secret("kb:x", "sekret")
    assert storage.get_secret("kb:x") == "sekret"
    assert not rec.calls
    assert json.loads(authfile.read_text()) == {"kb:x": "sekret"}


def test_linux_present_secret_tool_prefers_keychain(run, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(_keychain.shutil, "which",
                        lambda name: "/usr/bin/secret-tool")
    rec = run(_Proc(stdout="sekret"))
    assert storage.get_secret("kb:x") == "sekret"
    assert rec.calls[0][0] == ["secret-tool", "lookup", "service", "kb:x"]


def test_mac_missing_security_uses_file(run, monkeypatch, authfile):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(_keychain.shutil, "which", lambda name: None)
    rec = run()
    storage.set_secret("kb:x", "sekret")
    assert storage.get_secret("kb:x") == "sekret"
    assert not rec.calls


def test_unknown_platform_falls_back_to_file(run, monkeypatch, authfile):
    monkeypatch.setattr(sys, "platform", "freebsd")  # no tool table entry
    rec = run()
    storage.set_secret("kb:x", "sekret")
    assert storage.get_secret("kb:x") == "sekret"
    assert not rec.calls


def test_fallback_missing_file_is_a_clean_miss(fallback):
    assert storage.get_secret("kb:x") is None
    assert storage.delete_secret("kb:x") is False
    assert not fallback.parent.exists()  # reads create nothing


def test_fallback_creates_file_owner_only(fallback):
    storage.set_secret("kb:x", "sekret")
    assert json.loads(fallback.read_text()) == {"kb:x": "sekret"}
    if os.name == "posix":
        assert os.stat(fallback).st_mode & 0o777 == 0o600


def test_fallback_creates_directory_owner_only(fallback):
    storage.set_secret("kb:x", "sekret")
    assert fallback.parent.is_dir()
    if os.name == "posix":
        assert os.stat(fallback.parent).st_mode & 0o077 == 0


def test_fallback_rewrites_loose_file_as_owner_only(fallback):
    fallback.parent.mkdir()
    fallback.write_text("{}")
    os.chmod(fallback, 0o644)
    storage.set_secret("kb:x", "sekret")
    assert json.loads(fallback.read_text()) == {"kb:x": "sekret"}
    if os.name == "posix":
        # the rename swaps the inode, so the 0o644 did not survive
        assert os.stat(fallback).st_mode & 0o777 == 0o600


def test_fallback_upsert(fallback):
    storage.set_secret("kb:x", "one")
    storage.set_secret("kb:x", "two")
    storage.set_secret("kb:y", "three")
    assert storage.get_secret("kb:x") == "two"
    assert json.loads(fallback.read_text()) == {"kb:x": "two", "kb:y": "three"}


def test_fallback_delete(fallback):
    storage.set_secret("kb:x", "sekret")
    assert storage.delete_secret("kb:x") is True
    assert storage.get_secret("kb:x") is None
    assert json.loads(fallback.read_text()) == {}
    assert storage.delete_secret("kb:x") is False


def test_fallback_corrupt_json_is_error_not_miss(fallback):
    fallback.parent.mkdir()
    fallback.write_text("not json at all")
    with pytest.raises(storage.SecretStoreError, match="auth.json"):
        storage.get_secret("kb:x")


def test_fallback_non_object_json_is_error(fallback):
    fallback.parent.mkdir()
    fallback.write_text('["kb:x"]')
    with pytest.raises(storage.SecretStoreError, match="JSON object"):
        storage.get_secret("kb:x")


def test_fallback_non_string_value_is_error(fallback):
    fallback.parent.mkdir()
    fallback.write_text('{"kb:x": 5}')
    with pytest.raises(storage.SecretStoreError, match="JSON object"):
        storage.get_secret("kb:x")


def test_fallback_unusable_path_is_error_not_miss(monkeypatch, tmp_path, fallback):
    blocker = tmp_path / "blocker"  # a regular file where the directory belongs
    blocker.write_text("x")
    monkeypatch.setattr(_filestore, "auth_file_path",
                        lambda: str(blocker / "auth.json"))
    with pytest.raises(storage.SecretStoreError):
        storage.get_secret("kb:x")
    with pytest.raises(storage.SecretStoreError):
        storage.set_secret("kb:x", "sekret")


def test_fallback_write_failure_is_error(monkeypatch, fallback):
    fallback.parent.mkdir()

    def boom(*args, **kwargs):
        raise OSError("disk full")
    monkeypatch.setattr(_filestore.tempfile, "mkstemp", boom)
    with pytest.raises(storage.SecretStoreError, match="cannot write"):
        storage.set_secret("kb:x", "sekret")


def test_fallback_unreadable_store_is_error_not_miss(fallback):
    fallback.mkdir(parents=True)  # a directory where the file belongs: open() fails
    with pytest.raises(storage.SecretStoreError, match="cannot read"):
        storage.get_secret("kb:x")


def test_fallback_validation_precedes_file_io(fallback):
    for call in (storage.get_secret, storage.delete_secret):
        with pytest.raises(ValueError):
            call("")
    with pytest.raises(ValueError):
        storage.set_secret("kb:x", "a\nb")
    assert not fallback.parent.exists()


def test_fallback_drops_label_and_account(fallback):
    storage.set_secret("kb:x", "sekret", label="My Label", account="acc")
    assert json.loads(fallback.read_text()) == {"kb:x": "sekret"}


def test_cli_round_trip_on_file_fallback(monkeypatch, authfile, capsys):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(_keychain.shutil, "which", lambda name: None)
    storage.set_secret("kb:x", "sekret")
    assert storage_cli.main(["get", "kb:x"]) == 0
    assert capsys.readouterr().out == "sekret\n"
    assert storage_cli.main(["has", "kb:y"]) == 1


# ----------------------------------------------------------------- package

def test_package_exports():
    assert set(storage.__all__) == {
        "SecretStoreError", "get_secret", "set_secret", "delete_secret"}
    assert set(_keychain.__all__) == set(storage.__all__)
    for name in storage.__all__:
        assert getattr(storage, name) is getattr(_keychain, name)
        assert name in dir(storage)


def test_package_unknown_attribute():
    with pytest.raises(AttributeError, match="bogus"):
        storage.bogus  # noqa: B018


# --------------------------------------------------------------------- CLI

@pytest.mark.parametrize("argv", [[], ["set"], ["bogus", "x"], ["get", "a", "b"]])
def test_cli_usage_prints_docstring_and_exits_2(capsys, argv):
    with pytest.raises(SystemExit) as excinfo:
        storage_cli.main(argv)
    assert excinfo.value.code == 2
    assert storage_cli.__doc__.strip() in capsys.readouterr().err


def test_cli_get_prints_secret_to_stdout(monkeypatch, capsys):
    monkeypatch.setattr(storage_cli, "get_secret", lambda name: "sekret")
    assert storage_cli.main(["get", "kb:x"]) == 0
    captured = capsys.readouterr()
    assert captured.out == "sekret\n"
    assert captured.err == ""


def test_cli_get_missing_exits_1(monkeypatch, capsys):
    monkeypatch.setattr(storage_cli, "get_secret", lambda name: None)
    assert storage_cli.main(["get", "kb:x"]) == 1
    assert "no secret stored" in capsys.readouterr().err


def test_cli_set_prompts_and_stores(monkeypatch, capsys):
    answers = iter(["sekret", "sekret"])
    monkeypatch.setattr(storage_cli.getpass, "getpass", lambda prompt: next(answers))
    stored = []
    monkeypatch.setattr(storage_cli, "set_secret",
                        lambda name, secret: stored.append((name, secret)))
    assert storage_cli.main(["set", "kb:x"]) == 0
    assert stored == [("kb:x", "sekret")]
    captured = capsys.readouterr()
    assert "stored secret" in captured.err
    assert "sekret" not in captured.out + captured.err


def test_cli_set_mismatch_exits_1(monkeypatch, capsys):
    answers = iter(["a", "b"])
    monkeypatch.setattr(storage_cli.getpass, "getpass", lambda prompt: next(answers))
    stored = []
    monkeypatch.setattr(storage_cli, "set_secret",
                        lambda name, secret: stored.append((name, secret)))
    assert storage_cli.main(["set", "kb:x"]) == 1
    assert stored == []
    assert "do not match" in capsys.readouterr().err


def test_cli_set_empty_exits_1(monkeypatch, capsys):
    monkeypatch.setattr(storage_cli.getpass, "getpass", lambda prompt: "")
    stored = []
    monkeypatch.setattr(storage_cli, "set_secret",
                        lambda name, secret: stored.append((name, secret)))
    assert storage_cli.main(["set", "kb:x"]) == 1
    assert stored == []
    assert "empty secret" in capsys.readouterr().err


def test_cli_set_eof_exits_1(monkeypatch, capsys):
    def eof(prompt):
        raise EOFError
    monkeypatch.setattr(storage_cli.getpass, "getpass", eof)
    assert storage_cli.main(["set", "kb:x"]) == 1
    assert "no secret entered" in capsys.readouterr().err


def test_cli_has_exit_codes(monkeypatch, capsys):
    monkeypatch.setattr(storage_cli, "get_secret", lambda name: "s")
    assert storage_cli.main(["has", "kb:x"]) == 0
    monkeypatch.setattr(storage_cli, "get_secret", lambda name: None)
    assert storage_cli.main(["has", "kb:x"]) == 1
    assert "no secret stored" in capsys.readouterr().err


def test_cli_delete(monkeypatch, capsys):
    monkeypatch.setattr(storage_cli, "delete_secret", lambda name: True)
    assert storage_cli.main(["delete", "kb:x"]) == 0
    assert "deleted" in capsys.readouterr().err
    monkeypatch.setattr(storage_cli, "delete_secret", lambda name: False)
    assert storage_cli.main(["delete", "kb:x"]) == 1
    assert "no secret stored" in capsys.readouterr().err


def test_cli_store_error_exits_1(monkeypatch, capsys):
    def boom(name):
        raise storage.SecretStoreError("no service")
    monkeypatch.setattr(storage_cli, "get_secret", boom)
    assert storage_cli.main(["get", "kb:x"]) == 1
    assert capsys.readouterr().err.startswith("error: no service")


def test_cli_keyboard_interrupt_exits_130(monkeypatch, capsys):
    def boom(name):
        raise KeyboardInterrupt
    monkeypatch.setattr(storage_cli, "get_secret", boom)
    assert storage_cli.main(["get", "kb:x"]) == 130

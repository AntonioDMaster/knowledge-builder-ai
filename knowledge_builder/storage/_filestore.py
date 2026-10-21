"""The auth.json fallback store for machines with no keychain access tool.

Active only when the native backend is absent (no secret-tool on Linux,
no security CLI on macOS, no backend at all on other platforms). A
keychain tool that exists but fails at runtime (no Secret Service on
D-Bus, locked keychain) stays a loud SecretStoreError in _keychain --
the fallback never masks a broken keychain.

auth.json is a flat {"<name>": "<secret>"} mapping in the per-user data
directory, resolved per platform by auth_file_path():

    Linux, BSDs, ...  $XDG_DATA_HOME/knowledge-builder-ai/auth.json
                      (default ~/.local/share/knowledge-builder-ai/auth.json)
    macOS             ~/Library/Application Support/knowledge-builder-ai/auth.json
    Windows           %LOCALAPPDATA%\\knowledge-builder-ai\\auth.json

Every write goes through a mkstemp temp file (owner-only by creation)
and os.replace, so auth.json is never more permissive than the current
user -- also when it already existed with looser permissions, because
the rename swaps the inode -- and a crash never leaves a half-written
file. On POSIX the file ends up mode 0o600 and the directory 0o700; on
Windows the user-profile ACLs on %LOCALAPPDATA% already restrict access
to the user. This is the ~/.ssh/id_rsa level of protection: plaintext
on disk, not an OS keychain -- see README "What this does not protect
against".
"""

from __future__ import annotations

import json
import os
import sys
import tempfile

from ._keychain import SecretStoreError

__all__ = ["auth_file_path", "delete", "get", "set"]

_APP_DIR = "knowledge-builder-ai"
_FILE_NAME = "auth.json"


def auth_file_path():
    """Full path of auth.json in this platform's per-user data directory,
    resolved at call time so tests can pin sys.platform."""
    if sys.platform == "win32":
        base = (os.environ.get("LOCALAPPDATA")
                or os.path.join("~", "AppData", "Local"))
    elif sys.platform == "darwin":
        base = os.path.join("~", "Library", "Application Support")
    else:
        xdg = os.environ.get("XDG_DATA_HOME", "")
        base = xdg if os.path.isabs(xdg) else os.path.join("~", ".local", "share")
    return os.path.join(os.path.expanduser(base), _APP_DIR, _FILE_NAME)


def _load():
    """auth.json parsed as {name: secret}. A missing file is an empty
    mapping (a clean miss); a present-but-broken one is a SecretStoreError
    -- corruption must never silently downgrade to "no secrets"."""
    path = auth_file_path()
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    except ValueError as e:  # json.JSONDecodeError, UnicodeDecodeError
        raise SecretStoreError(f"cannot use {path}: {e}") from e
    except OSError as e:
        raise SecretStoreError(f"cannot read {path}: {e}") from e
    if not isinstance(data, dict) or not all(isinstance(v, str) for v in data.values()):
        raise SecretStoreError(
            f"cannot use {path}: expected a JSON object mapping secret names to strings")
    return data


def _save(mapping):
    """Replace auth.json with `mapping` atomically and owner-only:
    mkstemp creates the temp file readable/writable by the current user
    alone, the explicit chmod makes that unconditional (umask-independent),
    and os.replace never leaves a torn file behind."""
    path = auth_file_path()
    directory = os.path.dirname(path)
    payload = json.dumps(mapping, indent=2, sort_keys=True) + "\n"
    try:
        os.makedirs(directory, mode=0o700, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".auth.json.")
        try:
            os.chmod(tmp, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())  # the rename must not outrun the data
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except OSError as e:
        raise SecretStoreError(f"cannot write {path}: {e}") from e


def get(name):
    """The secret stored under `name`, or None. A read never creates the
    file or its directory."""
    return _load().get(name)


def set(name, secret):
    """Upsert `name` -> `secret`; the first write creates auth.json. The
    label/account metadata of the keychain backends has no file
    equivalent and is dropped here."""
    data = _load()
    data[name] = secret
    _save(data)


def delete(name):
    """Remove `name`; True when something was removed, False when the name
    was not stored. The file itself is kept, possibly as {}."""
    data = _load()
    if name not in data:
        return False
    del data[name]
    _save(data)
    return True

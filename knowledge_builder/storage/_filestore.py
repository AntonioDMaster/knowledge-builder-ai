"""The auth.json fallback store: a flat {name: secret} file in the
per-user data directory (auth_file_path()), used only when the platform
has no keychain access tool -- a present-but-broken keychain stays a
loud error in _keychain and never reaches this module. Plaintext on
disk by design; see README.md ("What this does not protect against")."""

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
    """Resolved at call time so tests can pin sys.platform."""
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
    """A missing file is a clean miss; a broken one is a loud error --
    corruption must never silently downgrade to "no secrets"."""
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
    """Atomic owner-only replace: the explicit chmod makes 0o600
    umask-independent, and the rename swaps the inode so a hand-loosened
    file is re-restricted by the next write."""
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
    """A read never creates the file or its directory."""
    return _load().get(name)


def set(name, secret):
    """Upsert; the first write creates auth.json."""
    data = _load()
    data[name] = secret
    _save(data)


def delete(name):
    """True when something was removed; the file itself is kept, possibly
    as {}."""
    data = _load()
    if name not in data:
        return False
    del data[name]
    _save(data)
    return True

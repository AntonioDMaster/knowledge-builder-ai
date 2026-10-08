"""Zero-dependency credentials storage over the OS's native secret store.

Linux: the Secret Service (GNOME Keyring, KeePassXC, KWallet, ...) via
the secret-tool CLI -- the only non-Python dependency in the stack.
macOS: the Keychain via the built-in security CLI. Windows: the
Credential Manager via ctypes (CredReadW/CredWriteW/CredDeleteW), so
nothing outside CPython there either.

A secret never passes through argv, the environment, or disk in the
clear: Linux and macOS receive it through a stdin pipe, Windows writes
it in-process. There is no fallback store and no environment read -- an
unreachable keychain is a SecretStoreError, never a silent downgrade.
"""

from __future__ import annotations

import functools
import subprocess
import sys

__all__ = [
    "SecretStoreError",
    "delete_secret",
    "get_secret",
    "set_secret",
]

# display metadata defaults; account is used by macOS and Windows only,
# the Linux attribute namespace is the bare name
_DEFAULT_ACCOUNT = "knowledge-builder"

_MAC_NOT_FOUND = 44  # security(1): "The specified item could not be found in the keychain"
_WIN_ERROR_NOT_FOUND = 1168
_CRED_TYPE_GENERIC = 1
# 2 = CRED_PERSIST_LOCAL_MACHINE (on disk); 1 would be memory-only for the session
_CRED_PERSIST_LOCAL_MACHINE = 2
_CRED_MAX_BLOB_BYTES = 5 * 512  # CRED_MAX_CREDENTIAL_BLOB_SIZE; utf-16 halves it in characters


class SecretStoreError(RuntimeError):
    """Backend failure: missing CLI tool, unreachable secret service,
    keychain error, or a write that did not verify. Bad arguments are a
    ValueError instead, raised before any store I/O."""


def _validate_text(value, what):
    """Shared argument gate, raised before any store I/O. Newlines are
    rejected in every value (uniform contract) even though the stdin
    pipe could carry them on Linux: a secret that only round-trips on
    some platforms is a trap."""
    if not isinstance(value, str) or not value:
        raise ValueError(f"{what} must be a non-empty string")
    if "\x00" in value or "\r" in value or "\n" in value:
        raise ValueError(f"{what} must not contain null or newline characters")
    return value


def _run(argv, *, stdin=None):
    """subprocess.run with UTF-8 pipes; a missing tool or undecodable
    output is a SecretStoreError, never a traceback."""
    try:
        return subprocess.run(argv, input=stdin, capture_output=True,
                               text=True, encoding="utf-8", check=False)
    except UnicodeDecodeError as e:
        raise SecretStoreError(f"{argv[0]} returned undecodable output: {e}") from e
    except OSError as e:
        if argv[0] == "secret-tool":
            hint = ("the Linux backend needs the secret-tool CLI: Debian/Ubuntu "
                    "'apt install libsecret-tools', Fedora 'dnf install "
                    "libsecret-tools', Arch 'pacman -S libsecret'")
        else:
            hint = "the macOS backend needs the security CLI built into macOS"
        raise SecretStoreError(f"cannot run {argv[0]!r}: {e} ({hint})") from e


# ------------------------------------------------------------------ linux

def _linux_get(name):
    r = _run(["secret-tool", "lookup", "service", name])
    if r.returncode == 0:
        return r.stdout  # the secret, verbatim
    # lookup exits 1 quietly when nothing matches; stderr means the store
    # itself failed (no Secret Service on D-Bus, locked, ...)
    if r.stderr.strip():
        raise SecretStoreError(f"secret-tool lookup failed: {r.stderr.strip()}")
    return None


def _linux_set(name, secret, label, _account):
    # secret-tool has no update, so a set is clear-then-store (two calls,
    # not atomic); clear also removes any duplicates from earlier doubles
    r = _run(["secret-tool", "clear", "service", name])
    if r.stderr.strip():
        raise SecretStoreError(f"secret-tool clear failed: {r.stderr.strip()}")
    r = _run(["secret-tool", "store", f"--label={label}", "service", name],
             stdin=secret)
    if r.returncode != 0:
        detail = r.stderr.strip() or f"exit code {r.returncode}"
        raise SecretStoreError(f"secret-tool store failed: {detail}")


def _linux_delete(name):
    if _linux_get(name) is None:
        return False
    r = _run(["secret-tool", "clear", "service", name])
    if r.returncode != 0:
        detail = r.stderr.strip() or f"exit code {r.returncode}"
        raise SecretStoreError(f"secret-tool clear failed: {detail}")
    return True


# ------------------------------------------------------------------ macOS

def _mac_find(name, account=None):
    argv = ["security", "find-generic-password"]
    if account is not None:
        argv += ["-a", account]
    r = _run(argv + ["-s", name, "-w"])
    if r.returncode == 0:
        return r.stdout.removesuffix("\n")  # security appends exactly one
    if r.returncode == _MAC_NOT_FOUND:
        return None
    detail = r.stderr.strip() or f"exit code {r.returncode}"
    raise SecretStoreError(f"security find-generic-password failed: {detail}")


def _mac_set(name, secret, label, account):
    # security takes a password non-interactively only via argv (briefly
    # visible in the process list) or a terminal prompt -- so the add
    # command is fed to its interactive mode (-i) over a stdin pipe
    # instead. The line parser tokenizes on whitespace, so any whitespace
    # in the payload is a pre-flight ValueError; whatever else it could
    # mangle is caught by the read-back verify. The security binary is
    # trusted on items it created, so that verify read does not prompt.
    for what, value in (("secret", secret), ("secret name", name),
                        ("label", label), ("account", account)):
        if any(c.isspace() for c in value):
            raise ValueError(
                f"the macOS security CLI takes its input on the stdin command "
                f"line and cannot carry whitespace in the {what}")
    line = (f"add-generic-password -U -a {account} -s {name} "
            f"-l {label} -w {secret}\n")
    r = _run(["security", "-i"], stdin=line)
    # -i's exit code is not a reliable verdict, the read-back is; scoped
    # to our account so verification can never read a different app's
    # item with the same service name
    if _mac_find(name, account) != secret:
        raise SecretStoreError(
            f"macOS Keychain did not store {name!r} correctly (security exit "
            f"code {r.returncode}); inspect the item with: security "
            f"find-generic-password -a {account} -s {name} -w")


def _mac_delete(name):
    r = _run(["security", "delete-generic-password", "-s", name])
    if r.returncode == 0:
        return True
    if r.returncode == _MAC_NOT_FOUND:
        return False
    detail = r.stderr.strip() or f"exit code {r.returncode}"
    raise SecretStoreError(f"security delete-generic-password failed: {detail}")


# ----------------------------------------------------------------- windows

@functools.lru_cache(maxsize=1)
def _win_advapi32():
    """advapi32 with use_last_error so ctypes.get_last_error() is exact.
    Deliberately loadable only on real Windows -- anything else is a
    clean SecretStoreError, also when tests pin the platform to win32."""
    import ctypes
    try:
        return ctypes.WinDLL("advapi32", use_last_error=True)
    except (OSError, AttributeError) as e:
        raise SecretStoreError(
            "the Windows backend (Credential Manager via ctypes) requires "
            "Windows") from e


@functools.lru_cache(maxsize=1)
def _win_credential_struct():
    """CREDENTIALW built from portable ctypes types (no wintypes import,
    so the class is constructible on any platform for layout tests).
    FILETIME must stay two uint32s: a c_uint64 would misalign the
    struct on 32-bit Windows."""
    import ctypes

    class FILETIME(ctypes.Structure):
        _fields_ = [("dwLowDateTime", ctypes.c_uint32),
                    ("dwHighDateTime", ctypes.c_uint32)]

    class CREDENTIAL(ctypes.Structure):
        _fields_ = [
            ("Flags", ctypes.c_uint32),
            ("Type", ctypes.c_uint32),
            ("TargetName", ctypes.c_wchar_p),
            ("Comment", ctypes.c_wchar_p),
            ("LastWritten", FILETIME),
            ("CredentialBlobSize", ctypes.c_uint32),
            ("CredentialBlob", ctypes.POINTER(ctypes.c_byte)),
            ("Persist", ctypes.c_uint32),
            ("AttributeCount", ctypes.c_uint32),
            ("Attributes", ctypes.c_void_p),
            ("TargetAlias", ctypes.c_wchar_p),
            ("UserName", ctypes.c_wchar_p),
        ]

    return CREDENTIAL


def _win_get(name):
    import ctypes
    dll = _win_advapi32()
    pcred = ctypes.POINTER(_win_credential_struct())()
    if dll.CredReadW(name, _CRED_TYPE_GENERIC, 0, ctypes.byref(pcred)) == 0:
        err = ctypes.get_last_error()
        if err == _WIN_ERROR_NOT_FOUND:
            return None
        raise SecretStoreError(f"CredReadW failed with Windows error {err}")
    try:
        blob = pcred.contents
        size = blob.CredentialBlobSize
        if size == 0 or not blob.CredentialBlob:
            return ""
        raw = ctypes.string_at(blob.CredentialBlob, size)
    finally:
        dll.CredFree(pcred)
    try:
        # tolerate NUL padding other writers leave behind
        return raw.decode("utf-16-le").rstrip("\x00")
    except UnicodeDecodeError as e:
        raise SecretStoreError(
            f"credential {name!r} does not hold UTF-16 text: {e}") from e


def _win_set(name, secret, label, account):
    import ctypes
    data = secret.encode("utf-16-le")
    if len(data) > _CRED_MAX_BLOB_BYTES:
        raise ValueError(
            f"secret is {len(secret)} characters; the Windows Credential "
            f"Manager caps stored secrets at {_CRED_MAX_BLOB_BYTES // 2} characters")
    dll = _win_advapi32()
    buffer = ctypes.create_string_buffer(data, len(data))  # exact length: no NUL padding
    cred = _win_credential_struct()()
    cred.Type = _CRED_TYPE_GENERIC
    cred.TargetName = name
    cred.Comment = label
    cred.CredentialBlobSize = len(data)
    cred.CredentialBlob = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte))
    cred.Persist = _CRED_PERSIST_LOCAL_MACHINE
    cred.UserName = account
    if dll.CredWriteW(ctypes.byref(cred), 0) == 0:
        raise SecretStoreError(
            f"CredWriteW failed with Windows error {ctypes.get_last_error()}"
            " (an existing entry written by another tool with different "
            "persistence must be deleted first)")


def _win_delete(name):
    import ctypes
    dll = _win_advapi32()
    if dll.CredDeleteW(name, _CRED_TYPE_GENERIC, 0) == 0:
        err = ctypes.get_last_error()
        if err == _WIN_ERROR_NOT_FOUND:
            return False
        raise SecretStoreError(f"CredDeleteW failed with Windows error {err}")
    return True


# ------------------------------------------------------------------ public

def _backend():
    """(get, set, delete) for this platform, resolved at call time (tests
    pin the platform by patching sys.platform)."""
    platform = sys.platform
    if platform == "linux":
        return _linux_get, _linux_set, _linux_delete
    if platform == "darwin":
        return _mac_find, _mac_set, _mac_delete
    if platform == "win32":
        return _win_get, _win_set, _win_delete
    raise SecretStoreError(
        f"no credentials backend for platform {platform!r}: only the OS "
        "stores are used, there is no fallback")


def get_secret(name):
    """The secret stored under `name`, or None when nothing is. Reads the
    OS-native store only; raises SecretStoreError when the store itself
    is unreachable (missing tool, no Secret Service on D-Bus, keychain
    error)."""
    _validate_text(name, "secret name")
    get, _, _ = _backend()
    return get(name)


def set_secret(name, secret, *, label=None, account=None):
    """Store `secret` under `name`, replacing anything already stored
    there. The secret travels by stdin pipe (Linux, macOS) or in-process
    (Windows) -- never through argv, environment, or disk.

    `label` is display metadata (default: the name); `account` is
    macOS/Windows metadata (default: "knowledge-builder"). Values a
    backend cannot carry raise ValueError before any store I/O: null or
    newline characters everywhere, whitespace on macOS (the security
    CLI is fed through its stdin command line), and secrets longer than
    1280 characters on Windows (the credential blob cap).
    """
    _validate_text(name, "secret name")
    _validate_text(secret, "secret")
    label = _validate_text(label or name, "label")
    account = _validate_text(account or _DEFAULT_ACCOUNT, "account")
    _, setter, _ = _backend()
    setter(name, secret, label, account)


def delete_secret(name):
    """Delete the item stored under `name`. True when something was
    deleted, False when nothing was stored under the name."""
    _validate_text(name, "secret name")
    _, _, delete = _backend()
    return delete(name)

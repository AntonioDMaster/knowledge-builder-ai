"""CLI entry point: python -m knowledge_builder.storage_cli <command> <name>

Commands (the secret is prompted for, never taken from argv):
  set <name>     store a secret in the OS keychain
  get <name>     print the stored secret
  has <name>     exit 0 if a secret is stored under the name, 1 if not
  delete <name>  delete the stored secret

Exit codes: 0 success, 1 nothing stored under the name or the keychain
failed, 2 bad usage. Status goes to stderr; only `get` writes to stdout
(the secret itself).
"""

from __future__ import annotations

import getpass
import sys

from knowledge_builder.storage import (
    SecretStoreError,
    delete_secret,
    get_secret,
    set_secret,
)

_COMMANDS = ("set", "get", "has", "delete")


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 2 or args[0] not in _COMMANDS:
        print(__doc__.strip(), file=sys.stderr)  # pyright: ignore[reportOptionalMemberAccess]
        raise SystemExit(2)

    command, name = args
    try:
        return _dispatch(command, name)
    except (SecretStoreError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print(file=sys.stderr)  # leave the terminal on a fresh line
        return 130


def _dispatch(command: str, name: str) -> int:
    if command == "set":
        try:
            secret = getpass.getpass(f"Enter secret for {name!r}: ")
            confirm = getpass.getpass("Retype to confirm: ")
        except EOFError:
            print("error: no secret entered (stdin is closed)", file=sys.stderr)
            return 1
        if not secret:
            print("error: empty secret", file=sys.stderr)
            return 1
        if secret != confirm:
            print("error: secrets do not match", file=sys.stderr)
            return 1
        set_secret(name, secret)
        print(f"stored secret for {name!r}", file=sys.stderr)
    elif command == "get":
        secret = get_secret(name)
        if secret is None:
            print(f"no secret stored for {name!r}", file=sys.stderr)
            return 1
        print(secret)
    elif command == "has":
        if get_secret(name) is None:
            print(f"no secret stored for {name!r}", file=sys.stderr)
            return 1
    else:  # delete
        if not delete_secret(name):
            print(f"no secret stored for {name!r}", file=sys.stderr)
            return 1
        print(f"deleted secret for {name!r}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

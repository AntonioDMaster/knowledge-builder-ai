# Credentials storage (`knowledge_builder/storage/`)

API keys and other secrets in each OS's **native secret store**, behind
three functions, with zero Python dependencies: the Secret Service via
the `secret-tool` CLI on Linux (GNOME Keyring, KeePassXC, KWallet, ...),
the Keychain via the built-in `security` CLI on macOS, and the
Credential Manager via `ctypes` (`CredReadW`/`CredWriteW`) on Windows.
On a machine with no keychain access tool -- `secret-tool` not
installed, the `security` CLI missing, a platform without a backend --
the same three functions fall back to a restricted `auth.json` file in
the user's data directory (next section). The fallback follows
availability, not failure: a keychain that is present but unreachable
(no Secret Service on D-Bus, a locked keychain) is still a loud
`SecretStoreError`, never a silent downgrade.

Why the OS store and not an encrypted file: on a desktop, the OS
keychain is the only real protection boundary. It enforces per-item
access prompts, locks on screensaver/sleep, and stores blobs encrypted
at rest. A key that lives on the same disk as the data protects against
nothing that matters locally. The fallback file exists for machines
that have no keychain at all (headless servers, minimal containers),
where the realistic alternative is a plaintext config file -- which is
worse: greppable, committable, and with default permissions.

## How the secret travels

The secret never passes through argv, the environment, or disk in the
clear:

| Platform | Store | Write path | Extra install |
| --- | --- | --- | --- |
| Linux | Secret Service (KeePassXC, GNOME Keyring, ...) | `secret-tool store` reads it from a stdin pipe | `libsecret-tools` (Debian/Ubuntu), `libsecret-tools` (Fedora), `libsecret` (Arch) |
| macOS | Keychain | the `add-generic-password` command is fed to `security -i` over a stdin pipe | nothing (built in) |
| Windows | Credential Manager | `CredWriteW` in-process via `ctypes` | nothing (CPython only) |

With the fallback active, the write path is the temp-file-plus-rename
described below; the secret still never passes through argv or the
environment.

## Fallback store: auth.json

When the platform's keychain access tool is missing,
`get_secret` / `set_secret` / `delete_secret` transparently operate on
`auth.json` instead -- a flat JSON object mapping secret names to
secrets:

```json
{
  "knowledge-builder:openrouter": "sk-or-..."
}
```

| Platform | Path |
| --- | --- |
| Linux (and other XDG platforms) | `$XDG_DATA_HOME/knowledge-builder-ai/auth.json`, default `~/.local/share/knowledge-builder-ai/auth.json` |
| macOS | `~/Library/Application Support/knowledge-builder-ai/auth.json` |
| Windows | `%LOCALAPPDATA%\knowledge-builder-ai\auth.json` |

Properties:

- **Restricted on creation.** The file is created readable and writable
  by the current user only: mode `0o600` on Linux/macOS; on Windows the
  user-profile ACLs on `%LOCALAPPDATA%` already restrict it to the user
  (and Windows has no portable stdlib ACL call). The directory is
  created `0o700`. Every write re-creates the file this way, so a
  hand-loosened `chmod` does not survive the next `set_secret`.
- **Crash-safe.** Writes go to a temp file in the same directory, are
  fsynced, and are moved onto `auth.json` with `os.replace` -- a crash
  never leaves a half-written file (at worst a leftover `.auth.json.*`
  temp file).
- **Missing file = no secrets**, not an error, and reads never create
  it. A **corrupt or unreadable** `auth.json` (invalid JSON, not an
  object, non-string values, no read permission) is a loud
  `SecretStoreError`, never a silent "no secrets".
- **Plaintext.** The secrets sit on disk in the clear, at the
  `~/.ssh/id_rsa` level of protection. Right for a container or headless
  box you already trust with the data the keys protect; wrong wherever
  a keychain exists -- install `secret-tool` instead.
- **No metadata.** `label`/`account` are keychain-only and are dropped.
- **Last write wins** for concurrent writers (read-modify-write, no
  locking).
- **No migration.** The backend is chosen per call from availability.
  Install `secret-tool` later and the secrets previously written to
  `auth.json` become invisible to `get_secret` -- re-seed them into the
  keychain (`python -m knowledge_builder.storage_cli set <name>`) and
  remove them from the file by hand.

Next door, in the same directory, sits `language_models.json` -- the
program's model configuration (`knowledge_builder/config.py`). It
references secrets by name (`api_key_title`) instead of embedding
them, so the keychain, or `auth.json` on the fallback, stays the only
place a key lives.

## CLI

```
python -m knowledge_builder.storage_cli <command> <name>
```

| Command | Effect |
| --- | --- |
| `set <name>` | prompt for the secret twice (no echo, never from argv) and store it |
| `get <name>` | print the stored secret (the only stdout output) |
| `has <name>` | exit 0 if stored, 1 if not; nothing printed on success |
| `delete <name>` | delete the stored secret |

Exit codes: 0 success, 1 nothing stored under the name or the store
failed, 2 bad usage. Status messages go to stderr, so `get` output is
pipeable. `set` is interactive only (`getpass` reads the terminal even
when stdin is piped).

## Library

```python
from knowledge_builder.storage import get_secret
```

| Name | Purpose |
| --- | --- |
| `get_secret(name) -> str \| None` | read; `None` when nothing is stored under the name |
| `set_secret(name, secret, *, label=None, account=None)` | store (upsert); `label` is display metadata (default: the name), `account` is macOS/Windows metadata (default: `"knowledge-builder"`) |
| `delete_secret(name) -> bool` | True when an item was deleted, False when nothing was there |
| `SecretStoreError` | `RuntimeError` subclass: unreachable store, keychain error, write that did not verify, or (on the fallback) an `auth.json` that cannot be read or written |

Bad arguments are a `ValueError` raised **before any store I/O**: empty
values, null characters, newlines (uniform contract -- a secret that
only round-trips on some platforms is a trap), whitespace on macOS, and
secrets longer than 1280 characters on Windows (the credential blob
cap). With the LLM stack the key never needs to touch a config file:

```python
from knowledge_builder.llm import LLMConfig, call_llm
from knowledge_builder.storage import get_secret

api_key = get_secret("knowledge-builder:openrouter") or ""
if not api_key:
    raise SystemExit("seed the key first: python -m "
                     "knowledge_builder.storage_cli set knowledge-builder:openrouter")
call_llm("ping", LLMConfig(base_url="https://openrouter.ai/api/v1",
                           model="some-model", api_key=api_key))
```

## Seeding by hand (interop)

Entries created with the platform's own tools are readable here as
long as the service/name matches:

```
secret-tool store --label='KB OpenRouter' service knowledge-builder:openrouter   # Linux, prompts
security add-generic-password -a "$USER" -s knowledge-builder:openrouter -w      # macOS, prompts
```

On Windows the Credential Manager GUI (Control Panel -> Credential
Manager -> Windows Credentials) can create generic credentials with a
matching target name.

On the fallback there is no interop to worry about: seed `auth.json`
directly (keep it owner-only), or use the CLI as usual.

## Platform notes

**Linux.** Items carry a single attribute pair, `service=<name>`, and
`lookup`/`clear` match on it. `secret-tool store` has no update, so a
set is clear-then-store (two calls, not atomic); `clear` also removes
duplicates from earlier double-stores. A quiet exit 1 from `lookup`
means *not found*; stderr output means the store itself failed and
raises. With KeePassXC as the Secret Service provider, entries appear
in the group exposed under *Tools -> Settings -> Secret Service
Integration*; by default KeePassXC shows a confirmation dialog on every
access -- uncheck "Confirm when passwords are retrieved by clients"
there for transparent reads, at the cost of that confirmation.

**macOS.** The `security` CLI takes a password non-interactively only
via argv (briefly visible in the process list) or a terminal prompt, so
`set_secret` feeds `add-generic-password -U` (update-if-exists) to
`security -i` over a stdin pipe. That line protocol cannot carry
whitespace, so whitespace in the secret/name/label/account is a
pre-flight `ValueError` -- multi-word secrets are unsupported on macOS,
single-token secrets (API keys) are the intended use. Every write is
verified by a scoped read-back (`find-generic-password -a <account> -s
<name> -w`); a mismatch raises `SecretStoreError` and never leaves a
silently garbled item. `find -w` appends exactly one newline, which is
stripped. Lookups match the first item with that service name: keep the
Keychain free of duplicate service names.

**Windows.** Generic credentials (`Type=1`, `Persist=2`), blob encoded
as UTF-16LE with a hard cap of 2560 bytes (1280 characters). Entries
are visible in the Credential Manager UI. Overwriting an entry created
by another tool with different persistence can be rejected by Windows;
delete it first.

## What this does not protect against

The OS keychain guards against other users, disk theft, and
accidental commits -- not against malware running as the same user
while the keychain is unlocked: anything running with your privileges
can read what you can read. Treat the store as storage safety, not as
an isolation boundary between your own processes. The `auth.json`
fallback is weaker still: plaintext on disk, it defends against other
local users (owner-only permissions) and accidental commits (it lives
outside the repository), nothing else -- disk theft and malware read
it freely. On a machine with a keychain, install the tool instead.

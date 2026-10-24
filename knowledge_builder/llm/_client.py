"""LLMConfig, chat/call_llm, and the response cache: one stdlib urllib
POST to an OpenAI-compatible endpoint. Only cache-related env vars are
read (LLM_CACHE, LLM_CACHE_DIR, XDG_CACHE_HOME, LOCALAPPDATA); see
README.md."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.request

__all__ = [
    "LLMConfig",
    "LLMError",
    "call_llm",
    "chat",
    "clear_cache",
]

_HEX64_FILE_RE = re.compile(r"^[0-9a-f]{64}\.json$")

_APP_DIR = "knowledge-builder-ai"


class LLMError(RuntimeError):
    """Runtime call failure; bad configuration raises ValueError in
    LLMConfig construction instead."""


def _positive_int(raw, field):
    try:
        value = int(raw)
    except (TypeError, ValueError) as e:
        raise ValueError(f"LLMConfig.{field}={raw!r} is not an integer") from e
    if value <= 0:
        raise ValueError(f"LLMConfig.{field} must be positive, got {value}")
    return value


@dataclasses.dataclass(frozen=True)
class LLMConfig:
    """Owned by the calling app: no default base_url/model, no env reads."""

    base_url: str
    model: str
    api_key: str = ""
    max_output_tokens: int = 16384
    temperature: float | None = None
    timeout: float = 600

    def __post_init__(self):
        base = str(self.base_url or "").strip().rstrip("/")
        if not base:
            raise ValueError(
                "LLMConfig.base_url is required: this stack has no default provider")
        if not base.startswith(("http://", "https://")):
            raise ValueError(
                f"LLMConfig.base_url must start with http:// or https://: {base!r}")
        object.__setattr__(self, "base_url", base)

        model = str(self.model or "").strip()
        if not model:
            raise ValueError(
                "LLMConfig.model is required: this stack has no default model")
        object.__setattr__(self, "model", model)

        object.__setattr__(self, "api_key", self.api_key or "")
        object.__setattr__(self, "max_output_tokens",
                           _positive_int(self.max_output_tokens, "max_output_tokens"))

        try:
            secs = float(self.timeout)
        except (TypeError, ValueError) as e:
            raise ValueError(f"LLMConfig.timeout={self.timeout!r} is not a number") from e
        if secs <= 0:
            raise ValueError(f"LLMConfig.timeout must be positive, got {secs}")
        object.__setattr__(self, "timeout", secs)

        if self.temperature is not None:
            try:
                temp = float(self.temperature)
            except (TypeError, ValueError) as e:
                raise ValueError(
                    f"LLMConfig.temperature={self.temperature!r} is not a number") from e
            object.__setattr__(self, "temperature", temp)

    @classmethod
    def from_dict(cls, data):
        """Unknown keys (app extras) are ignored."""
        names = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in dict(data).items() if k in names})

    def as_dict(self):
        """Persist twin of from_dict; the API key lands in clear text."""
        return dataclasses.asdict(self)

    def __repr__(self):
        key = "<redacted>" if self.api_key else "<none>"
        return (f"LLMConfig(base_url={self.base_url!r}, model={self.model!r}, "
                f"api_key={key}, max_output_tokens={self.max_output_tokens}, "
                f"temperature={self.temperature}, timeout={self.timeout})")


def _cache_enabled(cache):
    if cache is not None:
        return bool(cache)
    return os.environ.get("LLM_CACHE", "1") != "0"


def _cache_dir():
    return os.environ.get("LLM_CACHE_DIR") or _default_cache_dir()


def _default_cache_dir():
    """Resolved at call time so tests can pin sys.platform. Never inside the
    program directory: an installed package tree is not user-writable."""
    if sys.platform == "win32":
        base = (os.environ.get("LOCALAPPDATA")
                or os.path.join("~", "AppData", "Local"))
        return os.path.join(os.path.expanduser(base), _APP_DIR, "cache", "llm")
    if sys.platform == "darwin":
        base = os.path.join("~", "Library", "Caches")
    else:
        # XDG spec: a relative XDG_CACHE_HOME is ignored, not joined
        xdg = os.environ.get("XDG_CACHE_HOME", "")
        base = xdg if os.path.isabs(xdg) else os.path.join("~", ".cache")
    return os.path.join(os.path.expanduser(base), _APP_DIR, "llm")


def _cache_key(config, messages):
    """sha256 over everything that determines the response; the API key is
    deliberately excluded so it never touches the disk."""
    identity = json.dumps(
        [config.base_url, config.model, messages,
         config.max_output_tokens, config.temperature],
        separators=(",", ":"),
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _cache_get(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)["response"]
    except (OSError, ValueError, KeyError):
        return None  # missing/corrupt entries are cache misses, never failures


def _cache_put(path, base, model, response):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"base": base, "model": model, "response": response}, f)
    except OSError:
        pass  # full disk / read-only install: the call itself already succeeded


def _payload(config, messages, token_param):
    payload = {
        "model": config.model,
        "messages": messages,
        token_param: config.max_output_tokens,
    }
    if config.temperature is not None:
        payload["temperature"] = config.temperature
    return payload


def _post(config, payload):
    req = urllib.request.Request(
        config.base_url + "/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + config.api_key,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=config.timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:500]
        raise LLMError(f"HTTP {e.code} from {config.base_url}: {detail}") from e
    except urllib.error.URLError as e:
        raise LLMError(f"Cannot reach {config.base_url}: {e.reason}") from e
    except TimeoutError as e:
        raise LLMError(f"Timeout after {config.timeout}s against {config.base_url}: {e}") from e

    try:
        body = json.loads(raw.decode("utf-8", "replace"))
    except ValueError as e:
        snippet = raw[:200].decode("utf-8", "replace")
        raise LLMError(f"Non-JSON response from {config.base_url}: {snippet!r}") from e

    try:
        # .content is None when the model spent the whole budget on reasoning
        return body["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError) as e:
        raise LLMError(f"Unexpected response shape: {body!r:.500}") from e


def chat(messages, config, *, system=None, cache=None):
    """One completion; `messages` is a prompt string or a list of message dicts."""
    if isinstance(messages, str):
        messages = [{"role": "user", "content": messages}]
    msgs = ([{"role": "system", "content": system}] if system else []) + list(messages)

    path = None
    if _cache_enabled(cache):
        path = os.path.join(_cache_dir(), _cache_key(config, msgs) + ".json")
        cached = _cache_get(path)
        if cached is not None:
            return cached

    # GPT-5/o-series require max_completion_tokens; many other servers only
    # accept max_tokens. Retry only on a 400 that names the parameter.
    try:
        text = _post(config, _payload(config, msgs, "max_completion_tokens"))
    except LLMError as e:
        if "HTTP 400" not in str(e) or "max_completion_tokens" not in str(e):
            raise
        text = _post(config, _payload(config, msgs, "max_tokens"))

    if path is not None:
        _cache_put(path, config.base_url, config.model, text)
    return text


def call_llm(prompt, config, **kwargs):
    return chat(prompt, config, **kwargs)


def clear_cache():
    """Delete only this module's <64-hex>.json files; returns the count."""
    directory = _cache_dir()
    try:
        names = os.listdir(directory)
    except OSError:
        return 0
    removed = 0
    for name in names:
        if not _HEX64_FILE_RE.match(name):
            continue
        try:
            os.unlink(os.path.join(directory, name))
            removed += 1
        except OSError:
            pass
    return removed

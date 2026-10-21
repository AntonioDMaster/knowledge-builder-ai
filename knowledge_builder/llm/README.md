# LLM connection stack (`knowledge_builder/llm/`)

Vendor-unbound chat over any OpenAI-compatible `/v1/chat/completions`
endpoint, using only the standard library (`urllib.request`). Works with
OpenAI, Anthropic, Gemini, OpenRouter, Ollama, vLLM, llama.cpp server,
LM Studio, and anything else that speaks the chat-completions protocol.
No vendor SDK is imported anywhere; a new provider is just a different
`base_url`.

## The contract with the main app

This stack ships **no provider, no model, and no API key of its own**,
and it reads none of them from the environment. Provider selection and
saving/loading of connection parameters belong to the calling
application. Every call receives a configured `LLMConfig`:

```python
from knowledge_builder.llm import LLMConfig, call_llm

config = LLMConfig(
    base_url="https://openrouter.ai/api/v1",  # scheme + /v1 root
    model="some-model",
    api_key="sk-...",                         # "" for most local servers
    # optional, with these defaults:
    # max_output_tokens=16384, temperature=None, timeout=600
)
call_llm("ping", config)
```

Omitting the config is a `TypeError`; a blank/invalid field is a
`ValueError` raised at construction, before any network I/O. Runtime
failures (HTTP errors, timeouts, malformed responses) raise `LLMError`
(a `RuntimeError` subclass).

### Saving / loading

`LLMConfig.as_dict()` / `LLMConfig.from_dict(mapping)` round-trip the
config through a plain mapping, so the app can persist it in whatever
format it already uses. `from_dict` ignores unknown keys, so the app's
config file may carry extra fields (provider name, UI preferences)
alongside. `as_dict()` contains the API key in clear text: store the
result securely -- or keep the key out of files entirely and resolve it
at runtime from the OS keychain (`knowledge_builder/storage/`).
`repr(config)` masks the key, so configs are safe to
log.

The app-side implementation of both is `knowledge_builder/config.py`:
a `language_models.json` in the per-user data directory (Zed-shaped,
seeded from a packaged template) whose providers reference their key
by `api_key_title` instead of embedding it.
`config.llm_config_for(cfg, provider_name, model_name)` resolves the
key through the storage package and returns the `LLMConfig`.

### Per-call variations

The stack adds no override keyword arguments; vary a call with
`dataclasses.replace`:

```python
from dataclasses import replace
call_llm(prompt, replace(config, temperature=0.2))
```

## API

| Name | Purpose |
| --- | --- |
| `LLMConfig` | Frozen dataclass: `base_url`, `model`, `api_key`, `max_output_tokens`, `temperature`, `timeout` |
| `chat(messages, config, *, system=None, cache=None)` | One completion; `messages` is a prompt string or a list of message dicts |
| `call_llm(prompt, config, **kwargs)` | Single-turn convenience wrapper |
| `json_call(prompt, config, normalize=None, retries=4, **kwargs)` | Call, parse JSON, retry on bad replies (`normalize` may reject) |
| `yaml_call(prompt, config, normalize=None, retries=4, **kwargs)` | Same for fenced YAML replies (needs optional PyYAML) |
| `read_prompt` / `fill` / `extract_mermaid` / `parse_json` / `parse_yaml` | Prompt-file plumbing shared by workflow code |
| `clear_cache()` | Delete cached responses; returns the count |

## Response cache

Responses land on disk keyed by sha256(base URL + model + messages +
generation params), so iterating on downstream code costs nothing and
the same model name on two servers never shares an entry. The API key
is deliberately excluded from the key and never written to disk.

- Toggle per call: `chat(..., cache=False)`; or globally: `LLM_CACHE=0`.
- Location: `LLM_CACHE_DIR`, default `.cache/` next to `_client.py`
  (gitignored). Only `clear_cache()` and cache writes touch this
  directory, and only files this stack wrote (`<64-hex>.json`).

## The `max_completion_tokens` caveat

OpenAI GPT-5/o-series models require `max_completion_tokens` while many
third-party servers only accept `max_tokens`. The new name is sent
first; on a 400 whose error body names it, the call is retried once
with the legacy name. Unrelated 400s are not retried.

## Smoke test

```
python -m knowledge_builder.llm BASE_URL MODEL [API_KEY]
```

The stack ships no defaults, so the smoke test takes the connection
parameters from argv exactly the way the main app passes a configured
`LLMConfig`.

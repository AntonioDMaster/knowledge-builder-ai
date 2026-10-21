"""Tests for the LLM connection stack. HTTP is mocked at
urllib.request.urlopen -- no network, no keys. The autouse fixture pins
tests to a scratch cache dir and clears provider-ish env vars; the env
tests prove those vars are never consulted anyway.
"""

import io
import json
import os
import sys
import urllib.error
import urllib.request
from dataclasses import replace

import pytest

from knowledge_builder import llm
from knowledge_builder.llm import LLMConfig, _client, _plumbing

_ENV_NAMES = (
    "OPENAI_BASE_URL",
    "OPENAI_API_KEY",
    "OPENAI_MODEL",
    "LLM_MAX_OUTPUT_TOKENS",
    "LLM_TIMEOUT",
    "LLM_CACHE",
    "LLM_CACHE_DIR",
)

# the config every test passes by hand: no defaults kick in anywhere
CFG = LLMConfig(base_url="http://localhost:8000/v1", model="test-model",
                api_key="sk-test")


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path, monkeypatch):
    for name in _ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LLM_CACHE_DIR", str(tmp_path / "cache"))
    yield


class _FakeResponse:
    """Minimal stand-in for the object urlopen returns."""

    def __init__(self, payload: bytes):
        self._payload = payload

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None


def _ok(content="hello"):
    body = {"choices": [{"message": {"role": "assistant", "content": content}}]}
    return _FakeResponse(json.dumps(body).encode("utf-8"))


def _raw_ok(body):
    return _FakeResponse(json.dumps(body).encode("utf-8"))


def _http_error(code, body):
    return urllib.error.HTTPError(
        "http://localhost/v1/chat/completions", code, "boom", {},  # pyright: ignore[reportArgumentType]
        io.BytesIO(body.encode("utf-8")),
    )


class _Recorder:
    """Fake urlopen: records requests, replays canned responses (exception
    instances get raised)."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def __call__(self, req, timeout=None):
        assert self.responses, "unexpected network call"
        self.requests.append((req, timeout))
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _payload_of(rec, i=0):
    return json.loads(rec.requests[i][0].data)


def _headers_of(rec, i=0):
    return {k.lower(): v for k, v in rec.requests[i][0].header_items()}


# --------------------------------------------------------------- package


def test_llm_error_is_runtime_error():
    assert issubclass(llm.LLMError, RuntimeError)


def test_package_exports():
    for name in ("LLMConfig", "LLMError", "call_llm", "chat", "clear_cache",
                 "json_call", "yaml_call"):
        assert name in llm.__all__
    assert "chat" in dir(llm)
    with pytest.raises(AttributeError, match="no attribute"):
        llm.definitely_not_a_name  # noqa: B018


def test_no_preinstalled_provider_or_model():
    for name in ("DEFAULT_BASE_URL", "DEFAULT_MODEL"):
        assert name not in llm.__all__
        assert not hasattr(llm, name)


# ---------------------------------------------------------------- config


def test_config_is_required():
    with pytest.raises(TypeError, match="required positional argument: 'config'"):
        llm.call_llm("ping")
    with pytest.raises(TypeError, match="required positional argument: 'config'"):
        llm.chat("hi")


def test_provider_env_vars_are_never_consulted(monkeypatch):
    monkeypatch.setenv("OPENAI_BASE_URL", "http://evil/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-env")
    monkeypatch.setenv("OPENAI_MODEL", "env-model")
    monkeypatch.setenv("LLM_MAX_OUTPUT_TOKENS", "100")
    monkeypatch.setenv("LLM_TIMEOUT", "30")
    rec = _Recorder([_ok()])
    monkeypatch.setattr(urllib.request, "urlopen", rec)

    llm.call_llm("ping", CFG)

    req, timeout = rec.requests[0]
    assert req.full_url == "http://localhost:8000/v1/chat/completions"
    assert _headers_of(rec)["authorization"] == "Bearer sk-test"
    assert _payload_of(rec)["model"] == "test-model"
    assert _payload_of(rec)["max_completion_tokens"] == 16384  # config default
    assert timeout == 600  # config default, not the env value


def test_config_drives_request_shape(monkeypatch):
    rec = _Recorder([_ok("hi there")])
    monkeypatch.setattr(urllib.request, "urlopen", rec)
    cfg = LLMConfig(base_url="http://localhost:8000/v1/", model="test-model",
                    api_key="sk-test", max_output_tokens=100, timeout=30)

    assert llm.call_llm("ping", cfg) == "hi there"

    req, timeout = rec.requests[0]
    assert req.full_url == "http://localhost:8000/v1/chat/completions"  # rstrip("/")
    assert req.get_method() == "POST"
    assert _headers_of(rec)["content-type"] == "application/json"
    assert _headers_of(rec)["authorization"] == "Bearer sk-test"
    payload = _payload_of(rec)
    assert payload["model"] == "test-model"
    assert payload["messages"] == [{"role": "user", "content": "ping"}]
    assert payload["max_completion_tokens"] == 100
    assert "max_tokens" not in payload
    assert "temperature" not in payload
    assert timeout == 30


def test_per_call_variation_via_replace(monkeypatch):
    rec = _Recorder([_ok()])
    monkeypatch.setattr(urllib.request, "urlopen", rec)

    llm.call_llm("ping", replace(CFG, temperature=0.3, max_output_tokens=7,
                                 timeout=5))

    payload = _payload_of(rec)
    assert payload["temperature"] == 0.3
    assert payload["max_completion_tokens"] == 7
    assert rec.requests[0][1] == 5


def test_config_validation():
    with pytest.raises(ValueError, match="base_url"):
        LLMConfig(base_url="", model="m")
    with pytest.raises(ValueError, match="base_url"):
        LLMConfig(base_url=None, model="m")
    with pytest.raises(ValueError, match="http"):
        LLMConfig(base_url="api.openai.com/v1", model="m")  # missing scheme
    with pytest.raises(ValueError, match="model"):
        LLMConfig(base_url="http://a/v1", model="   ")
    with pytest.raises(ValueError, match="timeout"):
        LLMConfig(base_url="http://a/v1", model="m", timeout="soon")
    with pytest.raises(ValueError, match="timeout"):
        LLMConfig(base_url="http://a/v1", model="m", timeout=0)
    with pytest.raises(ValueError, match="max_output_tokens"):
        LLMConfig(base_url="http://a/v1", model="m", max_output_tokens=-5)
    with pytest.raises(ValueError, match="max_output_tokens"):
        LLMConfig(base_url="http://a/v1", model="m", max_output_tokens="lots")


def test_config_normalizes_input():
    cfg = LLMConfig(base_url="http://a/v1/", model="  m  ", api_key=None,
                    max_output_tokens="64", timeout="30")
    assert (cfg.base_url, cfg.model, cfg.api_key) == ("http://a/v1", "m", "")
    assert cfg.max_output_tokens == 64
    assert cfg.timeout == 30.0


def test_config_dict_roundtrip():
    cfg = LLMConfig("http://a/v1", "m", "sk-x", max_output_tokens=99,
                    temperature=0.25, timeout=12.5)
    as_dict = cfg.as_dict()
    assert as_dict == {
        "base_url": "http://a/v1", "model": "m", "api_key": "sk-x",
        "max_output_tokens": 99, "temperature": 0.25, "timeout": 12.5,
    }
    assert LLMConfig.from_dict(as_dict) == cfg


def test_config_from_dict_ignores_unknown_keys():
    cfg = LLMConfig.from_dict({"base_url": "http://a/v1", "model": "m",
                               "provider": "ollama", "remember": True,
                               "api_key": None})
    assert cfg == LLMConfig("http://a/v1", "m", "")


def test_config_repr_hides_api_key():
    shown = repr(LLMConfig("http://a/v1", "m", "sk-supersecret"))
    assert "supersecret" not in shown
    assert "<redacted>" in shown
    assert "<none>" in repr(LLMConfig("http://a/v1", "m"))
    assert "http://a/v1" in shown and "'m'" in shown


# ------------------------------------------------------- request shaping


def test_chat_system_and_history(monkeypatch):
    rec = _Recorder([_ok()])
    monkeypatch.setattr(urllib.request, "urlopen", rec)
    history = [
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "q2"},
    ]

    llm.chat(history, CFG, system="You are terse.")

    messages = _payload_of(rec)["messages"]
    assert messages[0] == {"role": "system", "content": "You are terse."}
    assert messages[1:] == history


def test_chat_accepts_bare_string(monkeypatch):
    rec = _Recorder([_ok()])
    monkeypatch.setattr(urllib.request, "urlopen", rec)

    llm.chat("hi", CFG)

    assert _payload_of(rec)["messages"] == [{"role": "user", "content": "hi"}]


# ------------------------------------------------- token-param fallback


def test_max_tokens_fallback_on_rejection(monkeypatch):
    err = _http_error(400, json.dumps({"error": {
        "message": "Unrecognized request argument supplied: max_completion_tokens"}}))
    rec = _Recorder([err, _ok("legacy ok")])
    monkeypatch.setattr(urllib.request, "urlopen", rec)

    assert llm.call_llm("ping", CFG) == "legacy ok"

    assert "max_completion_tokens" in _payload_of(rec, 0)
    second = _payload_of(rec, 1)
    assert second["max_tokens"] == CFG.max_output_tokens
    assert "max_completion_tokens" not in second


def test_unrelated_400_does_not_fallback(monkeypatch):
    rec = _Recorder([_http_error(400, '{"error": {"message": "model not found"}}')])
    monkeypatch.setattr(urllib.request, "urlopen", rec)

    with pytest.raises(llm.LLMError, match="HTTP 400"):
        llm.call_llm("ping", CFG)
    assert len(rec.requests) == 1


# -------------------------------------------------------- error mapping


def test_http_error_body_quoted(monkeypatch):
    rec = _Recorder([_http_error(401, '{"error": {"message": "bad key"}}')])
    monkeypatch.setattr(urllib.request, "urlopen", rec)

    with pytest.raises(llm.LLMError,
                       match="HTTP 401 from http://localhost:9/v1:.*bad key"):
        llm.call_llm("ping", LLMConfig(base_url="http://localhost:9/v1", model="m"))


def test_url_error(monkeypatch):
    rec = _Recorder([urllib.error.URLError("connection refused")])
    monkeypatch.setattr(urllib.request, "urlopen", rec)

    with pytest.raises(llm.LLMError, match="Cannot reach"):
        llm.call_llm("ping", CFG)


def test_timeout_error(monkeypatch):
    rec = _Recorder([TimeoutError("read timed out")])
    monkeypatch.setattr(urllib.request, "urlopen", rec)

    with pytest.raises(llm.LLMError, match="Timeout after"):
        llm.call_llm("ping", CFG)


def test_non_json_200(monkeypatch):
    rec = _Recorder([_FakeResponse(b"<html>gateway error</html>")])
    monkeypatch.setattr(urllib.request, "urlopen", rec)

    with pytest.raises(llm.LLMError, match="Non-JSON response"):
        llm.call_llm("ping", CFG)


def test_content_none_returns_empty(monkeypatch):
    rec = _Recorder([_raw_ok({"choices": [{"message": {"content": None}}]})])
    monkeypatch.setattr(urllib.request, "urlopen", rec)

    assert llm.call_llm("ping", CFG) == ""


def test_unexpected_shape(monkeypatch):
    rec = _Recorder([_raw_ok({"object": "list"})])
    monkeypatch.setattr(urllib.request, "urlopen", rec)

    with pytest.raises(llm.LLMError, match="Unexpected response shape"):
        llm.call_llm("ping", CFG)


# ---------------------------------------------------------------- cache


def test_cache_roundtrip(monkeypatch):
    rec = _Recorder([_ok("first")])
    monkeypatch.setattr(urllib.request, "urlopen", rec)
    assert llm.call_llm("same prompt", CFG) == "first"
    assert len(rec.requests) == 1

    # second call served from disk: urlopen never touched
    rec2 = _Recorder([])
    monkeypatch.setattr(urllib.request, "urlopen", rec2)
    assert llm.call_llm("same prompt", CFG) == "first"
    assert rec2.requests == []


def test_cache_disable_by_env_and_param(monkeypatch):
    monkeypatch.setenv("LLM_CACHE", "0")
    rec = _Recorder([_ok("one"), _ok("two")])
    monkeypatch.setattr(urllib.request, "urlopen", rec)
    assert llm.call_llm("p", CFG) == "one"
    assert llm.call_llm("p", CFG) == "two"  # went back to the wire
    assert len(rec.requests) == 2

    # per-call cache=False
    monkeypatch.delenv("LLM_CACHE")
    rec2 = _Recorder([_ok("one"), _ok("two")])
    monkeypatch.setattr(urllib.request, "urlopen", rec2)
    assert llm.call_llm("p", CFG, cache=False) == "one"
    assert llm.call_llm("p", CFG, cache=False) == "two"

    # cache=True re-enables under a no-cache env
    monkeypatch.setenv("LLM_CACHE", "0")
    rec3 = _Recorder([_ok("three")])
    monkeypatch.setattr(urllib.request, "urlopen", rec3)
    assert llm.call_llm("q", CFG, cache=True) == "three"
    rec3.responses.clear()  # any further network call fails the test
    assert llm.call_llm("q", CFG, cache=True) == "three"


def test_cache_separates_servers(monkeypatch):
    rec = _Recorder([_ok("server-a"), _ok("server-b")])
    monkeypatch.setattr(urllib.request, "urlopen", rec)
    cfg_a = LLMConfig(base_url="http://a/v1", model="m")
    cfg_b = LLMConfig(base_url="http://b/v1", model="m")

    assert llm.call_llm("same", cfg_a) == "server-a"
    assert llm.call_llm("same", cfg_b) == "server-b"

    rec.responses.clear()
    assert llm.call_llm("same", cfg_a) == "server-a"


def test_cache_key_covers_generation_params():
    msgs = [{"role": "user", "content": "p"}]
    cfg = LLMConfig(base_url="http://a/v1", model="m",
                    max_output_tokens=10, temperature=0.5)
    base = _client._cache_key(cfg, msgs)
    assert _client._cache_key(replace(cfg, temperature=None), msgs) != base
    assert _client._cache_key(replace(cfg, max_output_tokens=11), msgs) != base
    assert _client._cache_key(replace(cfg, model="other"), msgs) != base
    assert _client._cache_key(replace(cfg, api_key="sk-other"), msgs) == base
    assert _client._cache_key(cfg, [{"role": "user", "content": "other"}]) != base
    assert _client._cache_key(cfg, msgs) == base


def test_clear_cache_scoped(monkeypatch):
    rec = _Recorder([_ok("v1")])
    monkeypatch.setattr(urllib.request, "urlopen", rec)
    llm.call_llm("p", CFG)

    cache_dir = os.environ["LLM_CACHE_DIR"]
    assert len(os.listdir(cache_dir)) == 1

    # decoys that must survive: not 64-hex .json names
    for decoy in ("notes.txt", "cafe.json", "z" * 64 + ".json"):
        with open(os.path.join(cache_dir, decoy), "w", encoding="utf-8") as f:
            f.write("keep me")

    assert llm.clear_cache() == 1
    assert sorted(os.listdir(cache_dir)) == ["cafe.json", "notes.txt", "z" * 64 + ".json"]
    assert llm.clear_cache() == 0  # idempotent


# ------------------------------------------------------------ plumbing


def test_fill_replaces_only_named_slots():
    template = '{"repo": "{repo}", "n": 3, "shape": {"k": "{not_a_slot}"}}'
    out = _plumbing.fill(template, repo="demo")
    assert out == '{"repo": "demo", "n": 3, "shape": {"k": "{not_a_slot}"}}'


def test_read_prompt(tmp_path):
    (tmp_path / "p.txt").write_text("analyze {repo}", encoding="utf-8")
    assert _plumbing.read_prompt(str(tmp_path), "p.txt") == "analyze {repo}"


def test_extract_mermaid():
    md = "before\n```mermaid\ngraph TD\nA-->B\n```\nmid\n```mermaid\npie\n\"x\": 1\n```\n"
    assert _plumbing.extract_mermaid(md).startswith("graph TD")
    assert _plumbing.extract_mermaid(md, "pie").startswith("pie")
    assert _plumbing.extract_mermaid("no fences") == ""
    assert _plumbing.extract_mermaid(None) == ""


def test_parse_json_fenced_bare_and_nested():
    assert _plumbing.parse_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert _plumbing.parse_json('sure: {"a": [1, 2]}') == {"a": [1, 2]}
    # a ```mermaid fence inside a string value must not truncate the parse
    tricky = '```json\n{"diagram": "```mermaid\\ngraph\\n```", "n": 2}\n```'
    assert _plumbing.parse_json(tricky) == {
        "diagram": "```mermaid\ngraph\n```", "n": 2}


def test_parse_json_raises_on_no_json():
    with pytest.raises(AssertionError, match="No JSON found"):
        _plumbing.parse_json("all prose, no structure")


def test_parse_yaml_clear_error_without_pyyaml(monkeypatch):
    monkeypatch.setitem(sys.modules, "yaml", None)  # forces ImportError
    with pytest.raises(RuntimeError, match="PyYAML"):
        _plumbing.parse_yaml("```yaml\na: 1\n```")


def test_parse_yaml_parses_when_pyyaml_present():
    pytest.importorskip("yaml")
    assert _plumbing.parse_yaml("```yaml\na: 1\n```") == {"a": 1}


def test_json_call_retries_with_fresh_prompt(monkeypatch):
    calls = []

    def fake_call_llm(prompt, config, **kw):
        calls.append(prompt)
        if len(calls) == 1:
            return "no json here"
        return '```json\n{"a": 1, "b": 2}\n```'

    monkeypatch.setattr(_plumbing, "call_llm", fake_call_llm)

    assert _plumbing.json_call("give me data", CFG, retries=3) == {"a": 1, "b": 2}
    assert len(calls) == 2
    assert calls[0] == "give me data"
    assert "(retry 1)" in calls[1]


def test_json_call_normalize_retries(monkeypatch):
    def normalize(obj):
        if "b" not in obj:
            raise KeyError("b")
        return obj

    replies = iter(['{"a": 1}', '{"a": 1, "b": 2}'])
    monkeypatch.setattr(_plumbing, "call_llm",
                        lambda p, config, **kw: next(replies))

    assert _plumbing.json_call("p", CFG, normalize) == {"a": 1, "b": 2}


def test_json_call_gives_up(monkeypatch):
    monkeypatch.setattr(_plumbing, "call_llm",
                        lambda p, config, **kw: "still no json")
    with pytest.raises(AssertionError, match="gave up after 2"):
        _plumbing.json_call("p", CFG, retries=2)


def test_json_call_passes_config_and_kwargs(monkeypatch):
    seen = []

    def fake_call_llm(prompt, config, **kw):
        seen.append((config, kw))
        return '{"ok": true}'

    monkeypatch.setattr(_plumbing, "call_llm", fake_call_llm)

    assert _plumbing.json_call("p", CFG, cache=False) == {"ok": True}
    assert seen == [(CFG, {"cache": False})]

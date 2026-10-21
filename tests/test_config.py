"""Tests for the program configuration module. No keychain, no network:
llm_config_for's storage lookup is monkeypatched, file operations are
redirected into tmp_path through config_file_path, and sys.platform is
pinned per test for the path resolution. The template itself is under
test too: it must stay the shipped setup guide, byte-identical to what
save_config writes once _help is dropped.

The compliance section at the end drives the real llm stack functions
with config-built LLMConfigs, urlopen mocked like test_llm and
cache=False so the disk cache never answers for a mocked response.
"""

import json
import os
import sys
import urllib.request
from dataclasses import replace

import pytest

from knowledge_builder import config, llm, storage
from knowledge_builder.llm import LLMConfig
from knowledge_builder.storage import _filestore


@pytest.fixture
def cfgfile(tmp_path, monkeypatch):
    path = tmp_path / "kb-ai" / "language_models.json"
    monkeypatch.setattr(config, "config_file_path", lambda: str(path))
    return path


@pytest.fixture
def forbid_storage(monkeypatch):
    """Fail the test if llm_config_for touches the secret store at all."""
    def boom(name):
        raise AssertionError(f"unexpected storage lookup: {name!r}")
    monkeypatch.setattr(storage, "get_secret", boom)


# --------------------------------------------------------------- template

def test_template_is_the_setup_guide():
    data = json.loads(config._template_text())
    assert set(data) == {"_help", "language_models"}

    lm = data["language_models"]
    assert lm["openai_compatible"] == {}
    assert lm["ollama"] == {
        "api_url": "http://127.0.0.1:11434",
        "auto_discover": False,
        "available_models": []}

    directions = "\n".join(data["_help"]["directions"])
    assert "storage_cli set" in directions  # the non-obvious key-seeding step
    assert "ollama" in directions
    assert "--provider" in directions

    example = data["_help"]["openai_compatible_example"]
    (provider,) = example.values()
    assert provider["api_url"].endswith("/v1")
    assert provider["api_key_title"]
    assert provider["available_models"][0]["name"]

    ollama = data["_help"]["ollama_example"]
    assert ollama["api_url"] == "http://127.0.0.1:11434"
    assert ollama["available_models"][0]["name"]


def test_template_is_canonical_save_format():
    text = config._template_text()
    assert text == json.dumps(json.loads(text), indent=2) + "\n"


def test_base_config_matches_template():
    cfg = config.base_config()
    assert cfg == config.ProgramConfig.from_dict(json.loads(config._template_text()))
    assert cfg == config.ProgramConfig(language_models=config.LanguageModels(
        ollama=config.OllamaProvider(api_url="http://127.0.0.1:11434")))
    assert cfg.language_models.openai_compatible == {}


# ------------------------------------------------------------ path layout

def test_config_file_path_linux_xdg_default(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.setenv("HOME", "/home/user")
    assert config.config_file_path() == \
        "/home/user/.local/share/knowledge-builder-ai/language_models.json"


def test_config_file_path_linux_xdg_data_home_wins(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("XDG_DATA_HOME", "/xdg/data")
    assert config.config_file_path() == \
        "/xdg/data/knowledge-builder-ai/language_models.json"


def test_config_file_path_linux_relative_xdg_ignored(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("XDG_DATA_HOME", "relative/path")
    monkeypatch.setenv("HOME", "/home/user")
    assert config.config_file_path() == \
        "/home/user/.local/share/knowledge-builder-ai/language_models.json"


def test_config_file_path_darwin(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setenv("HOME", "/Users/u")
    assert config.config_file_path() == \
        "/Users/u/Library/Application Support/knowledge-builder-ai/language_models.json"


def test_config_file_path_windows_localappdata(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", "C:/Users/u/AppData/Local")
    assert config.config_file_path() == \
        "C:/Users/u/AppData/Local/knowledge-builder-ai/language_models.json"


def test_config_file_path_windows_home_fallback(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    assert config.config_file_path() == os.path.join(
        os.path.expanduser("~"), "AppData", "Local",
        "knowledge-builder-ai", "language_models.json")


@pytest.mark.parametrize("platform", ["linux", "darwin", "win32"])
def test_config_file_lives_next_to_auth_json(monkeypatch, platform):
    """Storage compatibility: config and credentials share the data dir."""
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.setenv("HOME", "/home/user")
    assert os.path.dirname(config.config_file_path()) == \
        os.path.dirname(_filestore.auth_file_path())


# -------------------------------------------------------------- load/save

def test_load_seeds_missing_file_from_template(cfgfile):
    cfg = config.load_config()
    assert cfgfile.exists()
    assert cfgfile.read_text(encoding="utf-8") == config._template_text()
    assert cfg == config.base_config()


def test_saved_base_config_is_the_template_without_help(cfgfile, tmp_path):
    config.load_config()
    out = tmp_path / "out.json"
    config.save_config(config.base_config(), out)
    data = json.loads(config._template_text())
    assert out.read_text(encoding="utf-8") == \
        json.dumps({"language_models": data["language_models"]},
                   indent=2) + "\n"


def test_load_leaves_existing_file_alone(cfgfile):
    payload = json.dumps({"language_models": {}})
    cfgfile.parent.mkdir(parents=True)
    cfgfile.write_text(payload, encoding="utf-8")
    cfg = config.load_config()
    assert cfg == config.ProgramConfig()
    assert cfgfile.read_text(encoding="utf-8") == payload


def test_load_accepts_explicit_path(tmp_path):
    path = tmp_path / "custom.json"
    cfg = config.load_config(path)
    assert path.exists()
    assert cfg == config.base_config()


def test_load_invalid_json_is_config_error(cfgfile):
    cfgfile.parent.mkdir(parents=True)
    cfgfile.write_text("{oops", encoding="utf-8")
    with pytest.raises(config.ConfigError, match="cannot use"):
        config.load_config()
    assert cfgfile.read_text(encoding="utf-8") == "{oops"  # never reset


def test_load_non_object_is_config_error(cfgfile):
    cfgfile.parent.mkdir(parents=True)
    cfgfile.write_text("[]", encoding="utf-8")
    with pytest.raises(config.ConfigError, match="expected a JSON object"):
        config.load_config()


def test_load_wrong_nested_shape_is_value_error(cfgfile):
    cfgfile.parent.mkdir(parents=True)
    cfgfile.write_text(
        json.dumps({"language_models": {"openai_compatible": "x"}}),
        encoding="utf-8")
    with pytest.raises(ValueError, match="openai_compatible"):
        config.load_config()


def test_load_ignores_unknown_keys(cfgfile):
    cfgfile.parent.mkdir(parents=True)
    cfgfile.write_text(
        json.dumps({"language_models": {}, "future_section": {"x": 1}}),
        encoding="utf-8")
    assert config.load_config() == config.ProgramConfig()


def test_load_unwritable_path_is_config_error(tmp_path, monkeypatch):
    blocker = tmp_path / "blocker"  # a regular file where the directory belongs
    blocker.write_text("x")
    monkeypatch.setattr(config, "config_file_path",
                        lambda: str(blocker / "language_models.json"))
    with pytest.raises(config.ConfigError):
        config.load_config()


def test_write_failure_is_config_error(tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("x")
    with pytest.raises(config.ConfigError, match="cannot write"):
        config.save_config(config.ProgramConfig(), blocker / "lm.json")


def test_save_load_round_trip(tmp_path):
    cfg = config.base_config()
    path = tmp_path / "lm.json"
    assert config.save_config(cfg, path) == str(path)
    assert config.load_config(path) == cfg


def test_save_is_deterministic(tmp_path):
    path = tmp_path / "lm.json"
    config.save_config(config.base_config(), path)
    first = path.read_text(encoding="utf-8")
    config.save_config(config.base_config(), path)
    assert path.read_text(encoding="utf-8") == first


def test_save_omits_unset_optionals(tmp_path):
    provider = config.OpenAICompatibleProvider(
        api_url="http://x/v1",
        available_models=(config.OpenAIModel(name="m"),))
    cfg = config.ProgramConfig(language_models=config.LanguageModels(
        openai_compatible={"P": provider}))
    path = tmp_path / "m.json"
    config.save_config(cfg, path)
    saved = json.loads(path.read_text(encoding="utf-8"))
    entry = saved["language_models"]["openai_compatible"]["P"]
    assert set(entry) == {"api_url", "available_models"}
    assert entry["available_models"][0] == {
        "name": "m",
        "capabilities": {  # defaults materialize on save
            "tools": False, "images": False, "parallel_tool_calls": False,
            "prompt_cache_key": False, "chat_completions": True,
            "interleaved_reasoning": False, "max_tokens_parameter": True}}
    assert config.load_config(path) == cfg


def test_save_default_path_is_config_file_path(cfgfile):
    config.save_config(config.ProgramConfig())
    assert cfgfile.read_text(encoding="utf-8") == \
        json.dumps({"language_models": {"openai_compatible": {}}},
                    indent=2) + "\n"


# -------------------------------------------------------------- validation

def test_model_name_must_be_nonempty_text():
    with pytest.raises(ValueError, match="OpenAIModel.name"):
        config.OpenAIModel(name="")
    with pytest.raises(ValueError, match="OpenAIModel.name"):
        config.OpenAIModel(name=123)


def test_api_url_must_be_absolute_http():
    with pytest.raises(ValueError, match="http:// or https://"):
        config.OpenAICompatibleProvider(api_url="endpoint.name/api/v1")
    with pytest.raises(ValueError, match="non-empty"):
        config.OpenAICompatibleProvider(api_url="")


@pytest.mark.parametrize("bad", [0, -5, "lots", 1.5, True])
def test_max_tokens_positive_integer(bad):
    with pytest.raises(ValueError, match="max_tokens"):
        config.OpenAIModel(name="m", max_tokens=bad)


def test_capabilities_must_be_booleans():
    with pytest.raises(ValueError, match="Capabilities.tools"):
        config.Capabilities(tools="yes")


def test_ollama_flags_must_be_booleans():
    with pytest.raises(ValueError, match="OllamaModel.supports_tools"):
        config.OllamaModel(name="m", supports_tools=1)


def test_auto_discover_must_be_boolean():
    with pytest.raises(ValueError, match="auto_discover"):
        config.OllamaProvider(auto_discover="no")


def test_context_window_positive():
    with pytest.raises(ValueError, match="context_window"):
        config.OllamaProvider(context_window=0)


def test_api_key_title_nonempty_or_absent():
    with pytest.raises(ValueError, match="api_key_title"):
        config.OpenAICompatibleProvider(api_url="http://x/v1", api_key_title="")


def test_provider_models_normalized_to_tuple():
    provider = config.OpenAICompatibleProvider(
        api_url="http://x/v1",
        available_models=[config.OpenAIModel(name="m")])
    assert provider.available_models == (config.OpenAIModel(name="m"),)
    with pytest.raises(TypeError, match="available_models"):
        config.OpenAICompatibleProvider(api_url="http://x/v1",
                                        available_models=("bogus",))


def test_ollama_default_url_is_local():
    provider = config.OllamaProvider()
    assert provider.api_url == "http://127.0.0.1:11434"
    assert provider.available_models == ()


# --------------------------------------------------- llm_config_for + glue

def _example_config():
    """One openai_compatible provider with a secret and a max_tokens."""
    return config.ProgramConfig(language_models=config.LanguageModels(
        openai_compatible={"example": config.OpenAICompatibleProvider(
            api_url="https://endpoint.name/api/v1",
            api_key_title="example",
            available_models=(config.OpenAIModel(
                name="openai/model-name-version", max_tokens=1000000),))}))


def test_llm_config_openai_compatible(monkeypatch):
    cfg = _example_config()
    monkeypatch.setattr(storage, "get_secret", lambda name: "sk-example")
    lc = config.llm_config_for(cfg, "example", "openai/model-name-version")
    assert isinstance(lc, LLMConfig)
    assert lc.base_url == "https://endpoint.name/api/v1"
    assert lc.model == "openai/model-name-version"
    assert lc.api_key == "sk-example"
    assert lc.max_output_tokens == 1000000
    assert lc.temperature is None


def test_llm_config_missing_secret_names_seed_command(monkeypatch):
    cfg = _example_config()
    monkeypatch.setattr(storage, "get_secret", lambda name: None)
    with pytest.raises(config.ConfigError,
                       match="storage_cli set example"):
        config.llm_config_for(cfg, "example", "openai/model-name-version")


def test_llm_config_ollama_appends_v1_and_needs_no_key(forbid_storage):
    provider = config.OllamaProvider(
        available_models=(config.OllamaModel(name="llama3:8b",
                                             max_tokens=32768),))
    cfg = config.ProgramConfig(language_models=config.LanguageModels(
        ollama=provider))
    lc = config.llm_config_for(cfg, "ollama", "llama3:8b")
    assert lc.base_url == "http://127.0.0.1:11434/v1"
    assert lc.model == "llama3:8b"
    assert lc.api_key == ""
    assert lc.max_output_tokens == 32768


def test_llm_config_ollama_v1_not_duplicated():
    provider = config.OllamaProvider(
        api_url="http://host:11434/v1/",
        available_models=(config.OllamaModel(name="m"),))
    cfg = config.ProgramConfig(language_models=config.LanguageModels(ollama=provider))
    assert config.llm_config_for(cfg, "ollama", "m").base_url == \
        "http://host:11434/v1"


def test_llm_config_keyless_openai_compatible_never_touches_storage(forbid_storage):
    provider = config.OpenAICompatibleProvider(
        api_url="http://x/v1", available_models=(config.OpenAIModel(name="m"),))
    cfg = config.ProgramConfig(language_models=config.LanguageModels(
        openai_compatible={"P": provider}))
    assert config.llm_config_for(cfg, "P", "m").api_key == ""


def test_llm_config_ollama_with_secret_resolves_it(monkeypatch):
    provider = config.OllamaProvider(
        api_key_title="ollama-key",
        available_models=(config.OllamaModel(name="m"),))
    cfg = config.ProgramConfig(language_models=config.LanguageModels(ollama=provider))
    monkeypatch.setattr(storage, "get_secret",
                        lambda name: "key!" if name == "ollama-key" else None)
    assert config.llm_config_for(cfg, "ollama", "m").api_key == "key!"


def test_llm_config_unset_max_tokens_uses_llm_default(forbid_storage):
    provider = config.OpenAICompatibleProvider(
        api_url="http://x/v1", available_models=(config.OpenAIModel(name="m"),))
    cfg = config.ProgramConfig(language_models=config.LanguageModels(
        openai_compatible={"P": provider}))
    assert config.llm_config_for(cfg, "P", "m").max_output_tokens == 16384


def test_llm_config_unknown_provider_lists_available():
    cfg = config.ProgramConfig(language_models=config.LanguageModels(
        openai_compatible={"example": config.OpenAICompatibleProvider(
            api_url="http://x/v1")},
        ollama=config.OllamaProvider()))
    with pytest.raises(config.ConfigError, match="example, ollama"):
        config.llm_config_for(cfg, "Nope", "any")


def test_llm_config_unknown_model_lists_available():
    cfg = _example_config()
    ollama = config.OllamaProvider(
        available_models=(config.OllamaModel(name="llama3:8b"),))
    cfg = replace(cfg, language_models=replace(cfg.language_models,
                                               ollama=ollama))
    with pytest.raises(config.ConfigError, match="has no model 'nope'"):
        config.llm_config_for(cfg, "example", "nope")
    with pytest.raises(config.ConfigError, match="llama3:8b"):
        config.llm_config_for(cfg, "ollama", "nope")


# ------------------------------------------ compliance with the llm stack

class _FakeResponse:
    """Minimal stand-in for the object urlopen returns."""

    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None


class _Recorder:
    """Fake urlopen: records requests, replays canned responses; an empty
    list means any network call fails the test."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def __call__(self, req, timeout=None):
        assert self.responses, "unexpected network call"
        self.requests.append((req, timeout))
        return self.responses.pop(0)


def _ok(content="hello"):
    body = {"choices": [{"message": {"role": "assistant", "content": content}}]}
    return _FakeResponse(json.dumps(body).encode("utf-8"))


def _payload_of(rec, i=0):
    return json.loads(rec.requests[i][0].data)


def _headers_of(rec, i=0):
    return {k.lower(): v for k, v in rec.requests[i][0].header_items()}


def _example_llm_config(monkeypatch, secret="sk-example"):
    """The example provider's LLMConfig with its key resolved from storage."""
    monkeypatch.setattr(storage, "get_secret", lambda name: secret)
    return config.llm_config_for(_example_config(), "example",
                                 "openai/model-name-version")


def test_call_llm_with_config_built_llm_config(monkeypatch):
    lc = _example_llm_config(monkeypatch)
    rec = _Recorder([_ok("hi there")])
    monkeypatch.setattr(urllib.request, "urlopen", rec)

    assert llm.call_llm("ping", lc, cache=False) == "hi there"

    req = rec.requests[0][0]
    assert req.full_url == "https://endpoint.name/api/v1/chat/completions"
    assert req.get_method() == "POST"
    assert _headers_of(rec)["authorization"] == "Bearer sk-example"
    assert _headers_of(rec)["content-type"] == "application/json"
    payload = _payload_of(rec)
    assert payload["model"] == "openai/model-name-version"
    assert payload["max_completion_tokens"] == 1000000  # config max_tokens
    assert payload["messages"] == [{"role": "user", "content": "ping"}]


def test_chat_with_ollama_config_hits_v1_chat_completions(monkeypatch,
                                                          forbid_storage):
    provider = config.OllamaProvider(
        available_models=(config.OllamaModel(name="llama3:8b",
                                             max_tokens=32768),))
    cfg = config.ProgramConfig(language_models=config.LanguageModels(
        ollama=provider))
    lc = config.llm_config_for(cfg, "ollama", "llama3:8b")
    rec = _Recorder([_ok()])
    monkeypatch.setattr(urllib.request, "urlopen", rec)

    llm.chat("hi", lc, cache=False)

    assert rec.requests[0][0].full_url == \
        "http://127.0.0.1:11434/v1/chat/completions"
    assert _headers_of(rec)["authorization"] == "Bearer "  # keyless
    assert _payload_of(rec)["model"] == "llama3:8b"
    assert _payload_of(rec)["max_completion_tokens"] == 32768


def test_json_call_with_config_built_llm_config(monkeypatch):
    lc = _example_llm_config(monkeypatch)
    rec = _Recorder([_ok('{"answer": 42}')])
    monkeypatch.setattr(urllib.request, "urlopen", rec)

    assert llm.json_call("give json", lc, cache=False) == {"answer": 42}


def test_yaml_call_with_config_built_llm_config(monkeypatch):
    pytest.importorskip("yaml")
    lc = _example_llm_config(monkeypatch)
    rec = _Recorder([_ok("```yaml\nanswer: 42\n```")])
    monkeypatch.setattr(urllib.request, "urlopen", rec)

    assert llm.yaml_call("give yaml", lc, cache=False) == {"answer": 42}


def test_per_call_replace_on_config_built_llm_config(monkeypatch):
    lc = _example_llm_config(monkeypatch)
    rec = _Recorder([_ok()])
    monkeypatch.setattr(urllib.request, "urlopen", rec)

    llm.call_llm("ping", replace(lc, temperature=0.2), cache=False)

    assert _payload_of(rec)["temperature"] == 0.2


def test_config_built_llm_config_survives_stack_round_trip(monkeypatch):
    lc = _example_llm_config(monkeypatch)
    assert LLMConfig.from_dict(lc.as_dict()) == lc


def test_missing_secret_fails_before_any_network(monkeypatch):
    monkeypatch.setattr(storage, "get_secret", lambda name: None)
    rec = _Recorder([])
    monkeypatch.setattr(urllib.request, "urlopen", rec)

    with pytest.raises(config.ConfigError, match="storage_cli set"):
        config.llm_config_for(_example_config(), "example",
                              "openai/model-name-version")
    assert rec.requests == []

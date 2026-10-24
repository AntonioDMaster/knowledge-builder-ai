"""Tests for the LLM CLI. No network, no keychain: HTTP is mocked at
urllib.request.urlopen like test_llm, the secret store is monkeypatched
like test_config, and language_models.json is redirected into tmp_path
through config.config_file_path (the CLI resolves the default path at
call time, so the monkeypatch reaches it).
"""

import io
import json
import urllib.error
import urllib.request
from dataclasses import replace

import pytest

from knowledge_builder import config, llm_cli, storage


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path, monkeypatch):
    for name in ("LLM_CACHE", "LLM_CACHE_DIR"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LLM_CACHE_DIR", str(tmp_path / "cache"))
    yield


@pytest.fixture
def cfgfile(tmp_path, monkeypatch):
    path = tmp_path / "kb-ai" / "language_models.json"
    monkeypatch.setattr(config, "config_file_path", lambda: str(path))
    return path


def _provider(url="http://localhost:8000/v1", models=("m1",), secret=None):
    entry = {"api_url": url,
             "available_models": [{"name": m} for m in models]}
    if secret:
        entry["api_key_title"] = secret
    return entry


def _write_config(path, providers, ollama=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"language_models": {
        "openai_compatible": providers, "ollama": ollama}}),
        encoding="utf-8")


class _FakeResponse:
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


def _fake_input(lines):
    """input() over a fixed list of lines; exhaustion is EOF."""
    it = iter(lines)

    def _input(prompt=""):
        try:
            return next(it)
        except StopIteration:
            raise EOFError from None

    return _input


# ------------------------------------------------------------ configuration

def test_bad_flag_is_usage_error(capsys):
    with pytest.raises(SystemExit) as e:
        llm_cli.main(["--bogus"])
    assert e.value.code == 2
    assert "usage:" in capsys.readouterr().err


def test_no_providers_directs_to_the_config_file(cfgfile, capsys):
    _write_config(cfgfile, providers={})
    assert llm_cli.main(["ping"]) == 1
    err = capsys.readouterr().err
    assert "no LLM provider is configured" in err
    assert str(cfgfile) in err


def test_untouched_template_directs_to_the_config_file(cfgfile, capsys):
    # a missing file is seeded from the template by load_config itself
    assert llm_cli.main(["ping"]) == 1
    err = capsys.readouterr().err
    assert "shipped setup guide" in err
    assert str(cfgfile) in err
    assert cfgfile.exists()
    assert "_help" in cfgfile.read_text(encoding="utf-8")


def test_edited_template_values_no_longer_count_as_example(cfgfile, capsys,
                                                           monkeypatch):
    rec = _Recorder([_ok("hi")])
    monkeypatch.setattr(urllib.request, "urlopen", rec)
    cfg = config.base_config()
    ollama = replace(config.OllamaProvider(),
                     available_models=(config.OllamaModel(name="llama3:8b"),))
    cfg = replace(cfg, language_models=replace(cfg.language_models,
                                               ollama=ollama))
    config.save_config(cfg, cfgfile)

    assert llm_cli.main(["ping"]) == 0
    assert rec.requests[0][0].full_url == \
        "http://127.0.0.1:11434/v1/chat/completions"


def test_corrupt_config_is_loud(cfgfile, capsys):
    cfgfile.parent.mkdir(parents=True, exist_ok=True)
    cfgfile.write_text("{not json", encoding="utf-8")
    assert llm_cli.main(["ping"]) == 1
    assert "cannot use" in capsys.readouterr().err


# ---------------------------------------------------------------- selection

def test_default_pick_is_first_provider_and_first_model(cfgfile, capsys,
                                                        monkeypatch):
    _write_config(cfgfile, providers={
        "alpha": _provider(url="http://alpha:1/v1", models=("a1", "a2")),
        "beta": _provider(url="http://beta:2/v1", models=("b1")),
    })
    rec = _Recorder([_ok()])
    monkeypatch.setattr(urllib.request, "urlopen", rec)

    assert llm_cli.main(["ping"]) == 0

    err = capsys.readouterr().err
    assert "provider=alpha model=a1" in err
    assert rec.requests[0][0].full_url == "http://alpha:1/v1/chat/completions"
    assert _payload_of(rec)["model"] == "a1"


def test_ollama_only_config_defaults_to_the_ollama_block(cfgfile, capsys,
                                                        monkeypatch):
    _write_config(cfgfile, providers={}, ollama={
        "api_url": "http://127.0.0.1:11434",
        "available_models": [{"name": "llama3:8b"}]})
    rec = _Recorder([_ok()])
    monkeypatch.setattr(urllib.request, "urlopen", rec)

    assert llm_cli.main(["ping"]) == 0

    err = capsys.readouterr().err
    assert "provider=ollama model=llama3:8b" in err
    assert rec.requests[0][0].full_url == \
        "http://127.0.0.1:11434/v1/chat/completions"  # /v1 appended


def test_explicit_ollama_provider_among_compat_providers(cfgfile, capsys,
                                                         monkeypatch):
    _write_config(cfgfile, providers={
        "alpha": _provider(url="http://alpha:1/v1"),
    }, ollama={"api_url": "http://127.0.0.1:11434",
               "available_models": [{"name": "llama3:8b"}]})
    rec = _Recorder([_ok()])
    monkeypatch.setattr(urllib.request, "urlopen", rec)

    assert llm_cli.main(["ping", "--provider", "ollama",
                         "--model", "llama3:8b"]) == 0

    assert _payload_of(rec)["model"] == "llama3:8b"
    assert rec.requests[0][0].full_url == \
        "http://127.0.0.1:11434/v1/chat/completions"


def test_provider_and_model_flags_override(cfgfile, capsys, monkeypatch):
    _write_config(cfgfile, providers={
        "alpha": _provider(url="http://alpha:1/v1", models=("a1", "a2")),
        "beta": _provider(url="http://beta:2/v1", models=("b1",)),
    })
    rec = _Recorder([_ok()])
    monkeypatch.setattr(urllib.request, "urlopen", rec)

    assert llm_cli.main(["ping", "--provider", "beta", "--model", "b1"]) == 0

    assert "provider=beta model=b1" in capsys.readouterr().err
    assert rec.requests[0][0].full_url == "http://beta:2/v1/chat/completions"
    assert _payload_of(rec)["model"] == "b1"


def test_unknown_provider_lists_available(cfgfile, capsys):
    _write_config(cfgfile, providers={
        "alpha": _provider(), "beta": _provider(),
    })
    assert llm_cli.main(["ping", "--provider", "nope"]) == 1
    err = capsys.readouterr().err
    assert "no provider 'nope'" in err
    assert "available: alpha, beta" in err
    assert str(cfgfile) in err


def test_unknown_model_lists_available(cfgfile, capsys):
    _write_config(cfgfile, providers={
        "alpha": _provider(models=("a1", "a2")),
    })
    assert llm_cli.main(["ping", "--model", "nope"]) == 1
    err = capsys.readouterr().err
    assert "no model 'nope'" in err
    assert "available: a1, a2" in err
    assert str(cfgfile) in err


def test_provider_without_models_directs_to_the_config_file(cfgfile, capsys):
    _write_config(cfgfile, providers={"alpha": _provider(models=())})
    assert llm_cli.main(["ping"]) == 1
    err = capsys.readouterr().err
    assert "no available_models" in err
    assert str(cfgfile) in err


def test_config_flag_loads_another_file(cfgfile, tmp_path, capsys,
                                        monkeypatch):
    _write_config(cfgfile, providers={"alpha": _provider()})
    other = tmp_path / "other" / "language_models.json"
    _write_config(other, providers={"gamma": _provider(url="http://gamma:3/v1",
                                                       models=("g1",))})
    rec = _Recorder([_ok()])
    monkeypatch.setattr(urllib.request, "urlopen", rec)

    assert llm_cli.main(["ping", "--config", str(other)]) == 0

    assert "provider=gamma model=g1" in capsys.readouterr().err
    assert rec.requests[0][0].full_url == "http://gamma:3/v1/chat/completions"


# ----------------------------------------------------------------- one-shot

def test_oneshot_reply_on_stdout_status_on_stderr(cfgfile, capsys,
                                                  monkeypatch):
    _write_config(cfgfile, providers={"alpha": _provider()})
    rec = _Recorder([_ok("the answer")])
    monkeypatch.setattr(urllib.request, "urlopen", rec)

    assert llm_cli.main(["ping"]) == 0

    captured = capsys.readouterr()
    assert captured.out == "the answer\n"
    assert "provider=alpha model=m1" in captured.err
    payload = _payload_of(rec)
    assert payload["messages"] == [{"role": "user", "content": "ping"}]
    assert _headers_of(rec)["authorization"] == "Bearer "  # keyless
    assert payload["max_completion_tokens"] == 16384  # the llm default


def test_oneshot_stdin_prompt(cfgfile, monkeypatch):
    _write_config(cfgfile, providers={"alpha": _provider()})
    rec = _Recorder([_ok()])
    monkeypatch.setattr(urllib.request, "urlopen", rec)
    monkeypatch.setattr("sys.stdin", io.StringIO("piped prompt"))

    assert llm_cli.main(["-"]) == 0

    assert _payload_of(rec)["messages"] == \
        [{"role": "user", "content": "piped prompt"}]


def test_system_prompt_flag(cfgfile, monkeypatch):
    _write_config(cfgfile, providers={"alpha": _provider()})
    rec = _Recorder([_ok()])
    monkeypatch.setattr(urllib.request, "urlopen", rec)

    llm_cli.main(["ping", "--system", "be terse"])

    messages = _payload_of(rec)["messages"]
    assert messages[0] == {"role": "system", "content": "be terse"}


def test_temperature_flag_uses_replace_not_kwargs(cfgfile, monkeypatch):
    _write_config(cfgfile, providers={"alpha": _provider()})
    rec = _Recorder([_ok()])
    monkeypatch.setattr(urllib.request, "urlopen", rec)

    llm_cli.main(["ping", "--temperature", "0.4"])

    assert _payload_of(rec)["temperature"] == 0.4


def test_no_cache_flag_hits_the_wire_again(cfgfile, capsys, monkeypatch):
    _write_config(cfgfile, providers={"alpha": _provider()})
    rec1 = _Recorder([_ok("first")])
    monkeypatch.setattr(urllib.request, "urlopen", rec1)
    assert llm_cli.main(["ping"]) == 0

    rec2 = _Recorder([_ok("second")])
    monkeypatch.setattr(urllib.request, "urlopen", rec2)
    assert llm_cli.main(["ping", "--no-cache"]) == 0

    out = capsys.readouterr().out
    assert out == "first\nsecond\n"


def test_default_caching_serves_the_second_call(cfgfile, capsys, monkeypatch):
    _write_config(cfgfile, providers={"alpha": _provider()})
    rec1 = _Recorder([_ok("cached reply")])
    monkeypatch.setattr(urllib.request, "urlopen", rec1)
    llm_cli.main(["ping"])

    rec2 = _Recorder([])  # any network call fails the test
    monkeypatch.setattr(urllib.request, "urlopen", rec2)
    llm_cli.main(["ping"])

    assert capsys.readouterr().out == "cached reply\ncached reply\n"


# -------------------------------------------------------------------- repl

def test_repl_multiturn_history(cfgfile, capsys, monkeypatch):
    _write_config(cfgfile, providers={"alpha": _provider()})
    rec = _Recorder([_ok("a1"), _ok("a2")])
    monkeypatch.setattr(urllib.request, "urlopen", rec)
    monkeypatch.setattr("builtins.input",
                        _fake_input(["q1", "q2", "/exit"]))

    assert llm_cli.main([]) == 0

    messages = _payload_of(rec, 1)["messages"]
    assert messages == [
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "q2"},
    ]
    assert "a1\na2\n" in capsys.readouterr().out


def test_repl_new_resets_history(cfgfile, capsys, monkeypatch):
    _write_config(cfgfile, providers={"alpha": _provider()})
    rec = _Recorder([_ok("a1"), _ok("a2")])
    monkeypatch.setattr(urllib.request, "urlopen", rec)
    monkeypatch.setattr("builtins.input",
                        _fake_input(["q1", "/new", "q2", "/exit"]))

    assert llm_cli.main([]) == 0

    assert _payload_of(rec, 1)["messages"] == \
        [{"role": "user", "content": "q2"}]
    assert "conversation reset" in capsys.readouterr().err


def test_repl_eof_exits_cleanly(cfgfile, monkeypatch):
    _write_config(cfgfile, providers={"alpha": _provider()})
    rec = _Recorder([_ok("a1")])
    monkeypatch.setattr(urllib.request, "urlopen", rec)
    monkeypatch.setattr("builtins.input", _fake_input(["q1"]))

    assert llm_cli.main([]) == 0
    assert len(rec.requests) == 1


def test_repl_survives_call_failure_and_pops_the_turn(cfgfile, capsys,
                                                      monkeypatch):
    _write_config(cfgfile, providers={"alpha": _provider()})
    rec = _Recorder([urllib.error.URLError("connection refused"), _ok("a2")])
    monkeypatch.setattr(urllib.request, "urlopen", rec)
    monkeypatch.setattr("builtins.input",
                        _fake_input(["q1", "q2", "/exit"]))

    assert llm_cli.main([]) == 0

    err = capsys.readouterr().err
    assert "Cannot reach" in err
    assert _payload_of(rec, 1)["messages"] == \
        [{"role": "user", "content": "q2"}]


def test_repl_keyboard_interrupt_is_130(cfgfile, monkeypatch):
    _write_config(cfgfile, providers={"alpha": _provider()})

    def boom(prompt=""):
        raise KeyboardInterrupt

    monkeypatch.setattr("builtins.input", boom)
    assert llm_cli.main([]) == 130


def test_repl_cache_command_off_and_on(cfgfile, capsys, monkeypatch):
    _write_config(cfgfile, providers={"alpha": _provider()})
    rec = _Recorder([_ok("a1"), _ok("a2")])
    monkeypatch.setattr(urllib.request, "urlopen", rec)
    monkeypatch.setattr("builtins.input", _fake_input([
        "q1", "/cache off", "/new", "q1", "/cache on", "/new", "q1", "/exit"]))

    assert llm_cli.main([]) == 0

    captured = capsys.readouterr()
    assert captured.out == "a1\na2\na1\n"
    assert "[llm-cli] cache: off" in captured.err
    assert "[llm-cli] cache: on" in captured.err
    # only the off-turn hit the wire; the last identical turn was a cache hit
    assert len(rec.requests) == 2


def test_repl_cache_status_and_bad_argument(cfgfile, capsys, monkeypatch):
    _write_config(cfgfile, providers={"alpha": _provider()})
    rec = _Recorder([])  # any network call fails the test
    monkeypatch.setattr(urllib.request, "urlopen", rec)
    monkeypatch.setattr("builtins.input", _fake_input([
        "/cache", "/cache maybe", "/cache", "/cache off", "/cache", "/exit"]))

    assert llm_cli.main([]) == 0

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.count("cache: default") == 2  # the bad arg changed nothing
    assert "[llm-cli] cache: off" in captured.err
    assert "takes 'on' or 'off', not 'maybe'" in captured.err
    assert "/cache on|off" in captured.err  # the banner lists the command
    assert rec.requests == []


def test_repl_cache_on_overrides_the_no_cache_flag(cfgfile, capsys,
                                                   monkeypatch):
    _write_config(cfgfile, providers={"alpha": _provider()})
    rec = _Recorder([_ok("a1")])
    monkeypatch.setattr(urllib.request, "urlopen", rec)
    monkeypatch.setattr("builtins.input", _fake_input([
        "/cache on", "q1", "/new", "q1", "/exit"]))

    assert llm_cli.main(["--no-cache"]) == 0

    captured = capsys.readouterr()
    assert captured.out == "a1\na1\n"
    assert "[llm-cli] cache: on" in captured.err
    assert len(rec.requests) == 1  # the second identical turn came from the cache


# ----------------------------------------------------------------- secrets

def test_missing_secret_names_the_seed_command(cfgfile, capsys, monkeypatch):
    _write_config(cfgfile, providers={"alpha": _provider(secret="ALPHA")})
    monkeypatch.setattr(storage, "get_secret", lambda name: None)
    rec = _Recorder([])  # any network call fails the test
    monkeypatch.setattr(urllib.request, "urlopen", rec)

    assert llm_cli.main(["ping"]) == 1
    err = capsys.readouterr().err
    assert "storage_cli set ALPHA" in err
    assert rec.requests == []


def test_stored_secret_becomes_the_bearer(cfgfile, monkeypatch):
    _write_config(cfgfile, providers={"alpha": _provider(secret="ALPHA")})
    monkeypatch.setattr(storage, "get_secret", lambda name: "sk-x")
    rec = _Recorder([_ok()])
    monkeypatch.setattr(urllib.request, "urlopen", rec)

    llm_cli.main(["ping"])

    assert _headers_of(rec)["authorization"] == "Bearer sk-x"

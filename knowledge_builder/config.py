"""Program configuration: the LLM endpoints this installation may use.

One JSON file in the per-user data directory, next to the storage
package's auth.json so the program keeps all of its data in one place:

    Linux, BSDs, ...  $XDG_DATA_HOME/knowledge-builder-ai/language_models.json
                      (default ~/.local/share/knowledge-builder-ai/language_models.json)
    macOS             ~/Library/Application Support/knowledge-builder-ai/language_models.json
    Windows           %LOCALAPPDATA%\\knowledge-builder-ai\\language_models.json

The shape follows Zed's language_models settings -- openai_compatible
providers keyed by name plus one ollama block. templates/config/
language_models.json is the shipped setup guide: a _help section with
directions and copy-paste examples (ignored on load like any unknown
key) over the empty base configuration. That template is also the seed:
load_config() on a missing file writes it out first, then loads it. A
file that exists is never overwritten, and a broken one is a loud
ConfigError, not a reset.

The file holds no API key. A provider that needs one declares
api_key_title -- the name under which knowledge_builder.storage keeps
it (seed with `python -m knowledge_builder.storage_cli set <name>`) --
and llm_config_for() resolves it at call time.

Invalid values are a ValueError at construction (the LLMConfig
convention); ConfigError (a RuntimeError) means the configuration is
unusable as asked: unreadable or unparseable file, unknown provider or
model, or a declared secret that is not stored.
"""

from __future__ import annotations

import dataclasses
import json
import os
import sys
import tempfile
from importlib import resources

__all__ = [
    "Capabilities",
    "ConfigError",
    "LanguageModels",
    "OllamaModel",
    "OllamaProvider",
    "OpenAICompatibleProvider",
    "OpenAIModel",
    "ProgramConfig",
    "base_config",
    "config_file_path",
    "llm_config_for",
    "load_config",
    "save_config",
]

_PKG = "knowledge_builder"
_APP_DIR = "knowledge-builder-ai"
_FILE_NAME = "language_models.json"


class ConfigError(RuntimeError):
    """The configuration is unusable as asked: an unreadable or unwritable
    file, or an unresolvable provider / model / secret reference."""


# ---------------------------------------------------------------- helpers

def _text(value, what):
    """Non-empty string or ValueError; JSON is typed, so nothing is coerced."""
    if not isinstance(value, str) or not value:
        raise ValueError(f"{what} must be a non-empty string")
    return value


def _optional_text(value, what):
    return None if value is None else _text(value, what)


def _optional_positive_int(value, what):
    if value is None:
        return None
    # one mixed guard: wrong type or non-positive value -- one invalid file entry
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"{what} must be a positive integer, got {value!r}")
    return value


def _boolean(value, what):
    if value is not True and value is not False:
        raise ValueError(f"{what} must be true or false")
    return value


def _api_url(value, what):
    _text(value, what)
    if not value.startswith(("http://", "https://")):
        raise ValueError(f"{what} must start with http:// or https://: {value!r}")
    return value


def _as_dict(obj):
    """Nested plain dict in field-declaration order (the template's key
    order), with unset (None) fields omitted -- from_dict reads their
    absence back as the default, so saved files stay hand-editable."""
    out = {}
    for f in dataclasses.fields(obj):
        v = getattr(obj, f.name)
        if v is None:
            continue
        if dataclasses.is_dataclass(v):
            v = _as_dict(v)
        elif isinstance(v, dict):
            v = {k: _as_dict(x) if dataclasses.is_dataclass(x) else x
                 for k, x in v.items()}
        elif isinstance(v, (list, tuple)):
            v = [_as_dict(x) if dataclasses.is_dataclass(x) else x for x in v]
        out[f.name] = v
    return out


def _only_known(cls, data):
    """{known field: value} from a JSON object; unknown keys are ignored
    so the file can carry newer or app-specific fields."""
    if not hasattr(data, "items"):
        raise ValueError(
            f"{cls.__name__} must be a JSON object, got {type(data).__name__}")
    names = {f.name for f in dataclasses.fields(cls)}
    return {k: v for k, v in data.items() if k in names}


# --------------------------------------------------------------- datamodel

@dataclasses.dataclass(frozen=True)
class Capabilities:
    """What one openai_compatible model can do (Zed's capabilities block).
    Informational for this stack so far; chat_completions and
    max_tokens_parameter default to True because that is all the llm
    package speaks."""

    tools: bool = False
    images: bool = False
    parallel_tool_calls: bool = False
    prompt_cache_key: bool = False
    chat_completions: bool = True
    interleaved_reasoning: bool = False
    max_tokens_parameter: bool = True

    def __post_init__(self):
        for f in dataclasses.fields(self):
            _boolean(getattr(self, f.name), f"Capabilities.{f.name}")

    @classmethod
    def from_dict(cls, data):
        return cls(**_only_known(cls, data))

    def as_dict(self):
        return _as_dict(self)


@dataclasses.dataclass(frozen=True)
class OpenAIModel:
    """One available_models entry; max_tokens maps to LLMConfig's
    max_output_tokens (None = the llm package's own default)."""

    name: str
    display_name: str | None = None
    max_tokens: int | None = None
    reasoning_effort: str | None = None
    capabilities: Capabilities = dataclasses.field(default_factory=Capabilities)

    def __post_init__(self):
        _text(self.name, "OpenAIModel.name")
        _optional_text(self.display_name, "OpenAIModel.display_name")
        _optional_positive_int(self.max_tokens, "OpenAIModel.max_tokens")
        _optional_text(self.reasoning_effort, "OpenAIModel.reasoning_effort")
        if self.capabilities is None:
            object.__setattr__(self, "capabilities", Capabilities())
        elif not isinstance(self.capabilities, Capabilities):
            raise TypeError(
                "OpenAIModel.capabilities must be a Capabilities object")

    @classmethod
    def from_dict(cls, data):
        d = _only_known(cls, data)
        if d.get("display_name") == "":
            d["display_name"] = None  # an empty display name means "unset"
        if "capabilities" in d:
            d["capabilities"] = Capabilities.from_dict(d["capabilities"] or {})
        return cls(**d)

    def as_dict(self):
        return _as_dict(self)


@dataclasses.dataclass(frozen=True)
class OllamaModel:
    """One available_models entry of the ollama block: Zed's ollama shape
    with supports_* flags instead of a capabilities object."""

    name: str
    display_name: str | None = None
    max_tokens: int | None = None
    supports_tools: bool = False
    supports_thinking: bool = False
    supports_images: bool = False

    def __post_init__(self):
        _text(self.name, "OllamaModel.name")
        _optional_text(self.display_name, "OllamaModel.display_name")
        _optional_positive_int(self.max_tokens, "OllamaModel.max_tokens")
        for name in ("supports_tools", "supports_thinking", "supports_images"):
            _boolean(getattr(self, name), f"OllamaModel.{name}")

    @classmethod
    def from_dict(cls, data):
        d = _only_known(cls, data)
        if d.get("display_name") == "":
            d["display_name"] = None  # an empty display name means "unset"
        return cls(**d)

    def as_dict(self):
        return _as_dict(self)


@dataclasses.dataclass(frozen=True)
class OpenAICompatibleProvider:
    """One named OpenAI-compatible endpoint. api_url is the scheme + /v1
    root, passed to LLMConfig.base_url verbatim. api_key_title names the
    stored secret to resolve at call time; without it the endpoint is
    assumed keyless."""

    api_url: str
    api_key_title: str | None = None
    available_models: tuple[OpenAIModel, ...] = ()

    def __post_init__(self):
        _api_url(self.api_url, "OpenAICompatibleProvider.api_url")
        _optional_text(self.api_key_title, "OpenAICompatibleProvider.api_key_title")
        models = tuple(self.available_models)
        for m in models:
            if not isinstance(m, OpenAIModel):
                raise TypeError(
                    "OpenAICompatibleProvider.available_models entries must "
                    "be OpenAIModel objects")
        object.__setattr__(self, "available_models", models)

    @classmethod
    def from_dict(cls, data):
        d = _only_known(cls, data)
        if "available_models" in d:
            d["available_models"] = tuple(
                OpenAIModel.from_dict(m) for m in d["available_models"] or ())
        return cls(**d)

    def as_dict(self):
        return _as_dict(self)


@dataclasses.dataclass(frozen=True)
class OllamaProvider:
    """The single ollama block. api_url keeps Zed's shape (no /v1);
    llm_config_for appends /v1 when missing, because the llm stack talks
    to the /v1 root. auto_discover and context_window are carried as
    data -- nothing implements discovery yet."""

    api_url: str = "http://127.0.0.1:11434"
    api_key_title: str | None = None
    auto_discover: bool = False
    context_window: int | None = None
    available_models: tuple[OllamaModel, ...] = ()

    def __post_init__(self):
        _api_url(self.api_url, "OllamaProvider.api_url")
        _optional_text(self.api_key_title, "OllamaProvider.api_key_title")
        _boolean(self.auto_discover, "OllamaProvider.auto_discover")
        _optional_positive_int(self.context_window, "OllamaProvider.context_window")
        models = tuple(self.available_models)
        for m in models:
            if not isinstance(m, OllamaModel):
                raise TypeError(
                    "OllamaProvider.available_models entries must be "
                    "OllamaModel objects")
        object.__setattr__(self, "available_models", models)

    @classmethod
    def from_dict(cls, data):
        d = _only_known(cls, data)
        if "available_models" in d:
            d["available_models"] = tuple(
                OllamaModel.from_dict(m) for m in d["available_models"] or ())
        return cls(**d)

    def as_dict(self):
        return _as_dict(self)


@dataclasses.dataclass(frozen=True)
class LanguageModels:
    """The language_models section: openai_compatible providers by name,
    plus the one ollama block (None when absent)."""

    openai_compatible: dict[str, OpenAICompatibleProvider] = \
        dataclasses.field(default_factory=dict)
    ollama: OllamaProvider | None = None

    def __post_init__(self):
        compat = dict(self.openai_compatible)
        for name, provider in compat.items():
            _text(name, "LanguageModels.openai_compatible keys")
            if not isinstance(provider, OpenAICompatibleProvider):
                raise TypeError(
                    "LanguageModels.openai_compatible values must be "
                    "OpenAICompatibleProvider objects")
        object.__setattr__(self, "openai_compatible", compat)
        if self.ollama is not None and not isinstance(self.ollama, OllamaProvider):
            raise TypeError("LanguageModels.ollama must be an OllamaProvider")

    @classmethod
    def from_dict(cls, data):
        d = _only_known(cls, data)
        if "openai_compatible" in d:
            raw = d["openai_compatible"] or {}
            if not hasattr(raw, "items"):
                raise ValueError(
                    "LanguageModels.openai_compatible must be a JSON object "
                    f"of providers, got {type(raw).__name__}")
            d["openai_compatible"] = {
                name: OpenAICompatibleProvider.from_dict(p)
                for name, p in raw.items()}
        if "ollama" in d:
            d["ollama"] = (OllamaProvider.from_dict(d["ollama"])
                           if d["ollama"] else None)
        return cls(**d)

    def as_dict(self):
        return _as_dict(self)


@dataclasses.dataclass(frozen=True)
class ProgramConfig:
    """The whole configuration file; language_models is the only section so far."""

    language_models: LanguageModels = dataclasses.field(
        default_factory=LanguageModels)

    def __post_init__(self):
        if not isinstance(self.language_models, LanguageModels):
            raise TypeError(
                "ProgramConfig.language_models must be a LanguageModels")

    @classmethod
    def from_dict(cls, data):
        d = _only_known(cls, data)
        if "language_models" in d:
            d["language_models"] = LanguageModels.from_dict(d["language_models"] or {})
        return cls(**d)

    def as_dict(self):
        return _as_dict(self)


# ------------------------------------------------------------------ paths

def _data_dir():
    """This platform's per-user data directory -- the same resolution as
    storage/_filestore.auth_file_path, so language_models.json and
    auth.json always sit side by side (tests assert it)."""
    if sys.platform == "win32":
        base = (os.environ.get("LOCALAPPDATA")
                or os.path.join("~", "AppData", "Local"))
    elif sys.platform == "darwin":
        base = os.path.join("~", "Library", "Application Support")
    else:
        xdg = os.environ.get("XDG_DATA_HOME", "")
        base = xdg if os.path.isabs(xdg) else os.path.join("~", ".local", "share")
    return os.path.join(os.path.expanduser(base), _APP_DIR)


def config_file_path():
    """language_models.json in the per-user data directory, resolved per
    call so tests can pin sys.platform."""
    return os.path.join(_data_dir(), _FILE_NAME)


# -------------------------------------------------------------- template

def _template_text():
    """The packaged setup guide: _help directions over the empty base
    configuration, formatted exactly the way save_config writes JSON so
    the seeded file stays canonical. from_dict ignores _help, so saving
    the base config writes only the language_models section."""
    try:
        return (resources.files(_PKG)
                .joinpath("templates", "config", _FILE_NAME)
                .read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise ConfigError(f"cannot read the packaged template: {e}") from e


def base_config():
    """The template as a ProgramConfig -- the empty base configuration;
    nothing is written."""
    return ProgramConfig.from_dict(json.loads(_template_text()))


# ------------------------------------------------------------- file I/O

def _write(path, text):
    """Replace the file at `path` with `text` atomically and owner-only:
    mkstemp + explicit chmod + fsync + os.replace, mirroring the storage
    package's auth.json writes -- same directory, same guarantees."""
    directory = os.path.dirname(path) or "."
    try:
        os.makedirs(directory, mode=0o700, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=f".{_FILE_NAME}.")
        try:
            os.chmod(tmp, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
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
        raise ConfigError(f"cannot write {path}: {e}") from e


def load_config(path=None):
    """ProgramConfig from `path` (default: config_file_path()); a missing
    file is seeded from the packaged template, a present one never
    overwritten. ConfigError for an unreadable or unparseable file,
    ValueError for invalid values inside it."""
    path = os.fspath(path) if path is not None else config_file_path()
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except FileNotFoundError:
        text = _template_text()
        _write(path, text)
    except OSError as e:
        raise ConfigError(f"cannot read {path}: {e}") from e
    try:
        data = json.loads(text)
    except ValueError as e:  # json.JSONDecodeError, UnicodeDecodeError
        raise ConfigError(f"cannot use {path}: {e}") from e
    if not isinstance(data, dict):
        raise ConfigError(f"cannot use {path}: expected a JSON object")
    return ProgramConfig.from_dict(data)


def save_config(config, path=None):
    """Write `config` to `path` (default: config_file_path()) as
    hand-editable JSON; returns the path written."""
    path = os.fspath(path) if path is not None else config_file_path()
    _write(path, json.dumps(_as_dict(config), indent=2) + "\n")
    return path


# ------------------------------------------------------- llm + storage glue

def llm_config_for(config, provider_name, model_name):
    """The LLMConfig for one provider + model of `config`, ready for any
    llm package entry point. `provider_name` is an openai_compatible
    key or "ollama".

    The API key is resolved through the storage package from the
    provider's api_key_title; a declared secret that is not stored is a
    ConfigError naming the seed command, and a provider without one is
    assumed keyless (api_key ""). The ollama api_url keeps Zed's shape,
    so /v1 is appended here when missing -- the llm stack posts to the
    /v1 root. Per-call variation stays dataclasses.replace, per the llm
    stack's contract."""
    from . import llm, storage

    lm = config.language_models
    if provider_name == "ollama":
        provider = lm.ollama
        is_ollama = True
    else:
        provider = lm.openai_compatible.get(provider_name)
        is_ollama = False
    if provider is None:
        available = sorted(lm.openai_compatible)
        if lm.ollama is not None:
            available.append("ollama")
        hint = ", ".join(available) or "none is configured"
        raise ConfigError(
            f"no provider {provider_name!r} in the configuration "
            f"(available: {hint})")

    model = next((m for m in provider.available_models if m.name == model_name), None)
    if model is None:
        names = ", ".join(m.name for m in provider.available_models) or "none"
        raise ConfigError(
            f"provider {provider_name!r} has no model {model_name!r} "
            f"(available: {names})")

    api_key = ""
    if provider.api_key_title is not None:
        api_key = storage.get_secret(provider.api_key_title)
        if api_key is None:
            raise ConfigError(
                f"provider {provider_name!r} declares api_key_title "
                f"{provider.api_key_title!r}, but no secret is stored under "
                f"that title; seed it with: python -m knowledge_builder."
                f"storage_cli set {provider.api_key_title}")

    base_url = provider.api_url
    if is_ollama:
        base_url = base_url.rstrip("/")
        if not base_url.endswith("/v1"):
            base_url += "/v1"

    kwargs = {}
    if model.max_tokens is not None:
        kwargs["max_output_tokens"] = model.max_tokens
    return llm.LLMConfig(base_url=base_url, model=model.name,
                         api_key=api_key, **kwargs)

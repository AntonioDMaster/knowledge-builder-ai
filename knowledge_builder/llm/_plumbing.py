"""Prompt plumbing around _client.py. PyYAML is optional (imported
lazily); the project stays stdlib-only."""

from __future__ import annotations

import json
import os
import re

from ._client import call_llm

__all__ = [
    "extract_mermaid",
    "fill",
    "json_call",
    "parse_json",
    "parse_yaml",
    "read_prompt",
    "yaml_call",
]


def read_prompt(prompts_dir, name):
    with open(os.path.join(prompts_dir, name), encoding="utf-8") as f:
        return f.read()


def fill(template, **kwargs):
    """Fill {key} slots by literal replacement -- unlike str.format, the
    template's literal JSON/Mermaid braces survive; unfilled slots stay."""
    for k, v in kwargs.items():
        template = template.replace("{" + k + "}", str(v))
    return template


def extract_mermaid(md, kind=None):
    """Pull the first ```mermaid block (optionally of a given diagram type)."""
    for m in re.finditer(r"```mermaid\s*\n(.*?)```", md or "", re.DOTALL):
        body = m.group(1).strip()
        if kind is None or body.startswith(kind):
            return body
    return ""


def parse_json(text):
    """Decode the first JSON value in a reply (after a ```json fence).
    raw_decode rather than a closing-fence regex, so a nested fence inside
    a string value can't truncate the parse."""
    m = re.search(r"```json[ \t]*\n", text)
    search = text[m.end():] if m else text
    decoder = json.JSONDecoder()
    for i, ch in enumerate(search):
        if ch in "[{":
            try:
                obj, _ = decoder.raw_decode(search[i:])
                return obj
            except ValueError:
                continue
    raise AssertionError(f"No JSON found in LLM response. Got:\n{text[:500]}")


def _yaml():
    try:
        import yaml  # pyright: ignore[reportMissingModuleSource]
    except ImportError as e:
        raise RuntimeError(
            "PyYAML is required for YAML replies: uv add pyyaml "
            "(or pip install pyyaml)"
        ) from e
    return yaml


def parse_yaml(text):
    """Raises on a missing fence or bad YAML so yaml_call can retry."""
    m = re.search(r"```yaml\s*\n(.*?)```", text, re.DOTALL)
    assert m, f"LLM response missing ```yaml fence. Got:\n{text[:500]}"
    return _yaml().safe_load(m.group(1))


def json_call(prompt, config, normalize=None, retries=4, **chat_kwargs):
    """Bad replies retry with a changed prompt tail (which also dodges the
    response cache); normalize(obj) may raise ValueError/KeyError to force
    a retry. Network errors are NOT caught -- they belong to the caller."""
    last = None
    for k in range(retries):
        tail = "" if k == 0 else (
            f"\n\nReturn COMPLETE JSON with every required top-level field. (retry {k})"
        )
        try:
            obj = parse_json(call_llm(prompt + tail, config, **chat_kwargs))
            return obj if normalize is None else normalize(obj)
        except (AssertionError, ValueError, KeyError) as e:
            last = e
    raise AssertionError(f"json_call gave up after {retries} tries. Last error: {last}")


def yaml_call(prompt, config, normalize=None, retries=4, **chat_kwargs):
    """Like json_call, for fenced YAML replies."""
    ymod = _yaml()  # fail fast with the install hint, before any request
    last = None
    for k in range(retries):
        tail = "" if k == 0 else (
            "\n\nReturn VALID YAML: put every string value in double quotes and "
            f"escape any double quote inside it with a backslash. (retry {k})"
        )
        try:
            obj = parse_yaml(call_llm(prompt + tail, config, **chat_kwargs))
            return obj if normalize is None else normalize(obj)
        except (AssertionError, ymod.YAMLError, ValueError, KeyError) as e:
            last = e
    raise AssertionError(f"yaml_call gave up after {retries} tries. Last error: {last}")

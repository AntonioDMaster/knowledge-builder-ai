"""CLI entry point: python -m knowledge_builder.llm_cli [PROMPT]

Chat with an LLM from the program configuration (language_models.json);
--help lists the flags and the provider/model defaults. Exit codes: 0
success, 1 config or call failure, 2 usage, 130 Ctrl-C. stdout carries
only the model's reply; status and errors go to stderr.
"""

from __future__ import annotations

import argparse
import dataclasses
import sys

from knowledge_builder import config, llm


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(sys.argv[1:] if argv is None else argv)
    try:
        return _run(args)
    except (config.ConfigError, llm.LLMError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print(file=sys.stderr)  # leave the terminal on a fresh line
        return 130


def _build_parser():
    parser = argparse.ArgumentParser(
        prog="python -m knowledge_builder.llm_cli",
        description="Chat with an LLM from the program configuration "
                    "(language_models.json).",
    )
    parser.add_argument(
        "prompt", nargs="?", default=None,
        help="one-shot prompt ('-' reads it from stdin); "
             "omit for an interactive chat")
    parser.add_argument(
        "--config", dest="config_path", metavar="PATH",
        help="configuration file (default: the per-user language_models.json)")
    parser.add_argument(
        "--provider", metavar="NAME",
        help="provider from the configuration (default: the first one, "
             "the ollama block last)")
    parser.add_argument(
        "--model", metavar="NAME",
        help="model of that provider (default: its first)")
    parser.add_argument(
        "--system", metavar="TEXT", help="system prompt for the call")
    parser.add_argument(
        "--temperature", type=float, metavar="FLOAT",
        help="sampling temperature")
    parser.add_argument(
        "--no-cache", action="store_true",
        help="skip the response cache (the REPL's /cache re-enables it)")
    return parser


def _run(args):
    path = args.config_path or config.config_file_path()
    program_config = config.load_config(path)
    if program_config == config.base_config():
        raise config.ConfigError(
            "no LLM is configured yet: the configuration file is still "
            "the shipped setup guide with nothing filled in; follow the "
            "directions inside it:\n" + path)

    provider_name, model_name = _select(program_config, args.provider,
                                        args.model, path)
    print(f"[llm-cli] provider={provider_name} model={model_name}",
          file=sys.stderr)

    llm_config = config.llm_config_for(program_config, provider_name,
                                       model_name)
    if args.temperature is not None:  # per-call variation = replace, not kwargs
        llm_config = dataclasses.replace(llm_config,
                                         temperature=args.temperature)

    prompt = args.prompt
    if prompt == "-":
        prompt = sys.stdin.read()

    cache = False if args.no_cache else None  # None: the LLM_CACHE default
    if prompt is not None:
        print(llm.call_llm(prompt, llm_config, system=args.system, cache=cache))
        return 0
    return _repl(llm_config, args.system, cache)


def _select(program_config, provider_arg, model_arg, path):
    """(provider_name, model_name) to use; every failure raises
    ConfigError naming `path`."""
    lm = program_config.language_models
    providers = dict(lm.openai_compatible)
    if lm.ollama is not None:
        providers["ollama"] = lm.ollama  # reserved name: the block wins

    if not provider_arg and not providers:
        raise config.ConfigError(
            "no LLM provider is configured: add an openai_compatible "
            "provider or an ollama block with available_models to:\n" + path)

    name = provider_arg or next(iter(providers))
    provider = providers.get(name)
    if provider is None:
        available = ", ".join(providers) or "none is configured"
        raise config.ConfigError(
            f"no provider {name!r} in the configuration "
            f"(available: {available}); edit:\n" + path)

    if model_arg:
        model_name = model_arg
    elif provider.available_models:
        model_name = provider.available_models[0].name
    else:
        raise config.ConfigError(
            f"provider {name!r} declares no available_models; edit:\n" + path)
    if all(m.name != model_name for m in provider.available_models):
        available = ", ".join(m.name for m in provider.available_models) or "none"
        raise config.ConfigError(
            f"provider {name!r} has no model {model_name!r} "
            f"(available: {available}); edit:\n" + path)
    return name, model_name


def _repl(llm_config, system, cache):
    history: list[dict] = []
    print("[llm-cli] one line per message; /new resets, "
          "/cache on|off sets the response cache, /exit quits",
          file=sys.stderr)
    while True:
        try:
            text = input("you> ").strip()
        except EOFError:
            print(file=sys.stderr)
            return 0
        if not text:
            continue
        if text in ("/exit", "/quit"):
            return 0
        if text == "/new":
            history.clear()
            print("[llm-cli] conversation reset", file=sys.stderr)
            continue
        if text.split()[0] == "/cache":
            cache = _cache_command(text, cache)
            continue
        history.append({"role": "user", "content": text})
        try:
            reply = llm.chat(history, llm_config, system=system, cache=cache)
        except llm.LLMError as e:
            history.pop()  # a failed turn must not shape the next request
            print(f"error: {e}", file=sys.stderr)
            continue
        history.append({"role": "assistant", "content": reply})
        print(reply)


def _cache_command(text, cache):
    """Handles /cache: bare reports the state, 'on'/'off' set it; returns
    the value to pass as chat's cache (None keeps the LLM_CACHE default)."""
    arg = text.partition(" ")[2].strip().lower()
    if arg == "on":
        print("[llm-cli] cache: on", file=sys.stderr)
        return True
    if arg == "off":
        print("[llm-cli] cache: off", file=sys.stderr)
        return False
    if arg:
        print(f"error: /cache takes 'on' or 'off', not {arg!r}",
              file=sys.stderr)
    elif cache is None:
        print("[llm-cli] cache: default (on unless LLM_CACHE=0)",
              file=sys.stderr)
    else:
        print(f"[llm-cli] cache: {'on' if cache else 'off'}", file=sys.stderr)
    return cache


if __name__ == "__main__":
    raise SystemExit(main())

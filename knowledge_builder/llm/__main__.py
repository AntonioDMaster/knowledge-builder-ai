import sys

if __name__ == "__main__":
    from . import LLMConfig, LLMError, call_llm

    args = sys.argv[1:]
    if len(args) not in (2, 3):
        print("usage: python -m knowledge_builder.llm BASE_URL MODEL [API_KEY]",
              file=sys.stderr)
        raise SystemExit(2)

    try:
        config = LLMConfig(base_url=args[0], model=args[1],
                           api_key=args[2] if len(args) == 3 else "")
        print(call_llm("Reply with the single word: ready", config))
    except (ValueError, LLMError) as e:
        print(f"LLM call failed: {e}", file=sys.stderr)
        raise SystemExit(1)

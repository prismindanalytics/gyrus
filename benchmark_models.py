#!/usr/bin/env python3
"""Benchmark extraction quality across models.

Usage:
    uv run --python 3.12 benchmark_models.py --anthropic-key KEY
    uv run --python 3.12 benchmark_models.py --anthropic-key KEY --openai-key KEY --google-key KEY

Picks 5 representative sessions (short, medium, long from different tools),
runs extraction with each available model, and compares:
  - Thought count
  - Quality (are thoughts strategic vs technical noise?)
  - Speed (wall clock time)
  - Cost (estimated from pricing)
"""
import sys, os, time, json, argparse
sys.path.insert(0, os.path.dirname(__file__))
import ingest

def pick_sessions(n=5):
    """Pick diverse representative sessions."""
    state = {"processed_sessions": {}}
    all_sessions = (
        ingest.find_claude_code_sessions(state) +
        ingest.find_cowork_sessions(state) +
        ingest.find_antigravity_sessions(state) +
        ingest.find_codex_sessions(state)
    )
    if not all_sessions:
        print("No sessions found"); return []

    # Pre-extract text and filter to sessions with real content
    for s in all_sessions:
        try:
            s["_size"] = os.path.getsize(s["path"])
        except OSError:
            s["_size"] = 0

    # Sort by size, filter to sessions with content
    all_sessions.sort(key=lambda s: s["_size"])
    viable = [s for s in all_sessions if s["_size"] > 2000]
    if not viable:
        viable = all_sessions

    # Try to get diversity: one per tool type, then fill with size spread
    picked = []
    seen_tools = set()
    seen_ids = set()
    # First pass: one per tool
    for s in viable:
        if s["type"] not in seen_tools and len(picked) < n:
            seen_tools.add(s["type"])
            seen_ids.add(s["session_id"])
            picked.append(s)
    # Second pass: fill remaining slots with size-distributed sessions
    remaining = [s for s in viable if s["session_id"] not in seen_ids]
    if remaining and len(picked) < n:
        step = max(1, len(remaining) // (n - len(picked) + 1))
        for i in range(0, len(remaining), step):
            if len(picked) >= n:
                break
            picked.append(remaining[i])
    return picked[:n]

def extract_text(session):
    extractors = {
        "claude-code": lambda s: ingest.extract_claude_code_conversation(s["path"]),
        "cowork": lambda s: ingest.extract_cowork_conversation(s["path"], s.get("output_dir")),
        "antigravity": lambda s: ingest.extract_antigravity_session(s["path"]),
        "codex": lambda s: ingest.extract_codex_conversation(s["path"]),
    }
    fn = extractors.get(session["type"])
    return fn(session) if fn else ""

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--anthropic-key", default=os.environ.get("ANTHROPIC_API_KEY"))
    parser.add_argument("--openai-key", default=os.environ.get("OPENAI_API_KEY"))
    parser.add_argument("--google-key", default=os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY"))
    args = parser.parse_args()

    # Determine available models
    models = []
    if args.anthropic_key:
        models += ["haiku", "sonnet"]
    if args.openai_key:
        models += ["gpt-5.4-nano", "gpt-5.4-mini", "gpt-5.4", "gpt-4.1-nano", "gpt-4.1-mini", "gpt-4.1"]
    if args.google_key:
        models += ["gemini-lite", "gemini-flash"]

    if not models:
        print("No API keys provided. Use --anthropic-key, --openai-key, --google-key")
        return

    print(f"Models to benchmark: {', '.join(models)}")
    print()

    sessions = pick_sessions(5)
    if not sessions:
        return
    print(f"Selected {len(sessions)} sessions:")
    for s in sessions:
        print(f"  {s['type']}: {s['session_id'][:30]}... ({s['_size']//1024}KB)")
    print()

    # Extract text once
    texts = []
    for s in sessions:
        t = extract_text(s)
        texts.append(t)
        print(f"  Text extracted: {len(t)} chars from {s['type']}:{s['session_id'][:20]}")
    print()

    # Run each model
    results = {}
    for model_name in models:
        resolved = ingest._resolve_model(model_name)
        provider = resolved["provider"]
        key_map = {"anthropic": args.anthropic_key, "openai": args.openai_key, "google": args.google_key}
        if not key_map.get(provider):
            print(f"  Skipping {model_name} (no {provider} key)")
            continue

        # Set keys once (don't mutate extract_model — use model_override instead)
        ingest._config["keys"] = {k: v for k, v in key_map.items() if v}

        print(f"{'='*60}")
        print(f"Model: {model_name} ({resolved['model']})")
        print(f"  Pricing: ${ingest.MODEL_PRICING.get(model_name, (0,0))[0]}/${ingest.MODEL_PRICING.get(model_name, (0,0))[1]} per MTok")
        print(f"{'='*60}")

        model_results = []
        total_time = 0
        total_thoughts = 0

        for i, (s, text) in enumerate(zip(sessions, texts)):
            if len(text) < 100:
                print(f"  Session {i+1}: too short, skipping")
                continue

            workspace = s.get("workspace", "")
            prompt = ingest.EXTRACTION_PROMPT
            if workspace:
                prompt += f"WORKSPACE: {workspace}\n\n"
            prompt += "CONVERSATION:\n" + text

            t0 = time.time()
            try:
                raw = ingest.call_llm(prompt, role="extract", max_tokens=2048, model_override=model_name)
                elapsed = time.time() - t0
                raw = ingest._strip_json_fences(raw)
                thoughts = json.loads(raw)
            except Exception as e:
                elapsed = time.time() - t0
                print(f"  Session {i+1}: ERROR ({elapsed:.1f}s) - {e}")
                thoughts = []

            n = len(thoughts)
            total_thoughts += n
            total_time += elapsed
            kinds = {}
            for t in thoughts:
                k = t.get("kind", "?")
                kinds[k] = kinds.get(k, 0) + 1

            print(f"  Session {i+1} ({s['type']}, {len(text)//1024}KB): "
                  f"{n} thoughts in {elapsed:.1f}s "
                  f"[{', '.join(f'{v} {k}' for k,v in kinds.items())}]")

            # Show first 2 thoughts as quality sample
            for t in thoughts[:2]:
                proj = t.get("project", "?")
                content = t.get("content", "")[:80]
                print(f"    > [{proj}] {content}")

            model_results.append({"thoughts": n, "time": elapsed, "chars": len(text)})

        # Summary
        cost_per_call = ingest._COST_PER_CALL.get(model_name, 0.01)
        est_cost = len([r for r in model_results if r["thoughts"] > 0]) * cost_per_call
        print(f"\n  TOTAL: {total_thoughts} thoughts, {total_time:.1f}s, ~${est_cost:.3f}")
        print()
        results[model_name] = {
            "thoughts": total_thoughts, "time": total_time,
            "cost": est_cost, "sessions": len(model_results)
        }

    # Final comparison
    print(f"\n{'='*60}")
    print(f"{'Model':<15} {'Thoughts':>8} {'Time':>8} {'Cost':>8} {'Thoughts/s':>10}")
    print(f"{'='*60}")
    for model, r in results.items():
        tps = r["thoughts"] / r["time"] if r["time"] > 0 else 0
        print(f"{model:<15} {r['thoughts']:>8} {r['time']:>7.1f}s ${r['cost']:>7.3f} {tps:>9.1f}")

if __name__ == "__main__":
    main()

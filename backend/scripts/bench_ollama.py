"""Benchmark raw Ollama throughput to isolate latency causes.

Measures prompt-eval and generation speed separately, so we can tell whether
slowness comes from prompt processing, generation, model loading or options.

Usage:
    cd backend
    .venv/Scripts/python scripts/bench_ollama.py                    # default sweep
    .venv/Scripts/python scripts/bench_ollama.py --num-ctx 2048 --num-thread 8
    .venv/Scripts/python scripts/bench_ollama.py --model qwen2:0.5b
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

OLLAMA = "http://localhost:11434"

# Approximate shape of a real Circle RAG prompt (system + evidence block)
FILLER = ("Aravinth: reminder submit the SIH abstract by Friday. " * 8)


def bench(model: str, prompt: str, num_predict: int, options: dict,
          keep_alive: str = "30m", label: str = "") -> dict:
    payload = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "keep_alive": keep_alive,
        "options": {**options, "num_predict": num_predict, "temperature": 0.1},
    }
    t0 = time.perf_counter()
    with httpx.Client(timeout=600.0) as c:
        r = c.post(f"{OLLAMA}/api/generate", json=payload)
        r.raise_for_status()
        d = r.json()
    wall = (time.perf_counter() - t0) * 1000
    def ms(k: str) -> float:
        return round(float(d.get(k) or 0) / 1e6, 1)
    pe, ev = int(d.get("prompt_eval_count") or 0), int(d.get("eval_count") or 0)
    out = {
        "label": label,
        "wall_ms": round(wall, 1),
        "prompt_tokens": pe,
        "prompt_eval_ms": ms("prompt_eval_duration"),
        "gen_tokens": ev,
        "gen_ms": ms("eval_duration"),
        "load_ms": ms("load_duration"),
        "prompt_ms_per_token": round(ms("prompt_eval_duration") / pe, 2) if pe else None,
        "gen_tok_per_s": round(ev / (ms("eval_duration") / 1000), 2) if ev else None,
    }
    print(f"  {label:34} prompt {out['prompt_tokens']:>5}t "
          f"{out['prompt_eval_ms']:>8}ms ({out['prompt_ms_per_token']}ms/t) | "
          f"gen {out['gen_tokens']:>4}t {out['gen_ms']:>8}ms "
          f"({out['gen_tok_per_s']} tok/s) | load {out['load_ms']}ms | "
          f"wall {out['wall_ms']}ms")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="gemma3:4b")
    ap.add_argument("--num-ctx", type=str, default="")
    ap.add_argument("--num-thread", type=int, default=0)
    ap.add_argument("--num-predict", type=int, default=64)
    ap.add_argument("--sweep", action="store_true")
    args = ap.parse_args()

    short = "Say hello."
    long_prompt = "You are a helpful assistant.\n\nEvidence:\n" + FILLER + "\n\nQuestion: summarize."

    print(f"model={args.model}  host={OLLAMA}")
    try:
        with httpx.Client(timeout=5.0) as c:
            c.get(f"{OLLAMA}/api/version").raise_for_status()
    except Exception as e:
        print(f"Ollama unreachable: {e}")
        return 1

    base = {}
    if args.num_ctx:
        base["num_ctx"] = int(args.num_ctx)
    if args.num_thread:
        base["num_thread"] = args.num_thread

    if args.sweep and not base:
        print("\n== option sweep (num_predict=%d) ==" % args.num_predict)
        bench(args.model, short, args.num_predict, {}, label="tiny prompt, model default")
        results = {}
        for ctx in (2048, 4096, 8192):
            for threads in (0, 8):
                opts = {"num_ctx": ctx}
                if threads:
                    opts["num_thread"] = threads
                lbl = f"num_ctx={ctx} threads={'auto' if not threads else threads}"
                results[lbl] = bench(args.model, long_prompt, args.num_predict,
                                     opts, label=lbl)
        print("\n== best by prompt eval ==")
        best = min(results.items(), key=lambda kv: kv[1]["prompt_eval_ms"])
        print(f"  {best[0]} -> {best[1]['prompt_eval_ms']}ms")
        return 0

    print("\n== single configuration ==")
    bench(args.model, short, args.num_predict, base, label="tiny prompt")
    bench(args.model, long_prompt, args.num_predict, base, label="RAG-sized prompt")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

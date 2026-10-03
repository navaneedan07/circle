"""Profile the ask pipeline on this machine.

Breaks each question down by stage (person identification, filters, vector
search, keyword search, fusion, prompt build, LLM generation) and prints
Ollama token metrics (prompt eval, generation, tok/s, model load).

Usage:
    cd backend
    .venv/Scripts/python scripts/profile_ask.py                  # default questions
    .venv/Scripts/python scripts/profile_ask.py "your question"  # one question
    .venv/Scripts/python scripts/profile_ask.py --warm            # warmup only
"""
from __future__ import annotations

import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from circle.ai.embeddings import get_embeddings  # noqa: E402
from circle.ai.llm import get_llm  # noqa: E402
from circle.ai.rag import (  # noqa: E402
    RagPipeline, _filters_for, _format_evidence, hybrid_retrieve,
    identify_person, understand,
)
from circle.repository.mongo import MongoStore  # noqa: E402

QUESTIONS = [
    "What have we been working on together recently?",
    "What did I promise this person?",
    "Prepare me for my next meeting",
]


def profile_one(store, embedder, llm, question: str, person_id: str | None) -> dict:
    from circle.domain.models import Person

    timings: dict[str, float] = {}

    t = time.perf_counter()
    plan = understand(question)
    person: Person | None = store.get_person(person_id) if person_id else None
    if person is None:
        person = identify_person(store, question)
    timings["identify_person"] = (time.perf_counter() - t) * 1000

    t = time.perf_counter()
    filters = _filters_for(person, plan)
    timings["filters"] = (time.perf_counter() - t) * 1000

    t = time.perf_counter()
    vec = embedder.embed(question)
    timings["query_embed"] = (time.perf_counter() - t) * 1000

    t = time.perf_counter()
    vector_hits = store.vector_search(vec, filters, k=16)
    timings["vector_search"] = (time.perf_counter() - t) * 1000

    t = time.perf_counter()
    keyword_hits = store.keyword_search(question, filters, k=16)
    timings["keyword_search"] = (time.perf_counter() - t) * 1000

    t = time.perf_counter()
    evidence = hybrid_retrieve(store, embedder, question, filters)
    timings["retrieve_total"] = (time.perf_counter() - t) * 1000

    t = time.perf_counter()
    prompt_tail = _format_evidence(evidence, person) if evidence else ""
    timings["context_assembly"] = (time.perf_counter() - t) * 1000

    t = time.perf_counter()
    pipe = RagPipeline(store, embedder, llm)
    result = pipe.ask(question, person_id=person.id if person else None)
    timings["ask_total"] = (time.perf_counter() - t) * 1000

    return {
        "question": question,
        "person": person.display_name if person else None,
        "timings": timings,
        "evidence": len(evidence),
        "prompt_chars": len(prompt_tail),
        "llm": dict(getattr(llm, "last_metrics", {}) or {}),
        "embed_llm": dict(getattr(embedder, "last_metrics", {}) or {}),
        "insufficient": result.insufficient,
        "answer_chars": len(result.answer),
    }


def main() -> int:
    argv = sys.argv[1:]
    person_filter = None
    if "--person" in argv:
        i = argv.index("--person")
        if i + 1 < len(argv):
            person_filter = argv[i + 1]
            argv = argv[:i] + argv[i + 2:]
    args = [a for a in argv if not a.startswith("--")]
    warm_only = "--warm" in sys.argv

    store = MongoStore()
    embedder = get_embeddings()
    llm = get_llm()

    print("== warmup (loads models into memory) ==")
    print("  embedder:", embedder.warmup())
    if hasattr(llm, "warmup"):
        print("  llm     :", llm.warmup())

    if warm_only:
        return 0

    # Pick the person with the most indexed memories (the realistic heavy case)
    # unless --person NAME is given.
    candidates = [p for p in store.list_people(limit=100)
                  if not p.is_user and p.display_name]
    if person_filter:
        needle = person_filter.lower()
        candidates = [p for p in candidates
                      if needle in p.display_name.lower()
                      or any(needle in a.lower() for a in p.aliases)]
    if not candidates:
        print("no matching people in the database — generate demo data first")
        return 1
    person = max(candidates,
                 key=lambda p: len(store.memories_for_person(p.id, limit=2000)))
    print(f"\nprofiling against person: {person.display_name} ({person.id})")

    results = []
    for q in (args or QUESTIONS):
        r = profile_one(store, embedder, llm, q, person.id)
        results.append(r)
        print(f"\nQ: {q}")
        print(f"  evidence records : {r['evidence']}  (prompt {r['prompt_chars']} chars)")
        for k, v in r["timings"].items():
            print(f"  {k:18}: {v:8.1f} ms")
        lm = r["llm"]
        if lm:
            print(f"  prompt tokens    : {lm.get('prompt_tokens')} "
                  f"({lm.get('prompt_eval_ms')} ms)")
            print(f"  generated tokens : {lm.get('generated_tokens')} "
                  f"({lm.get('generation_ms')} ms)")
            print(f"  tok/s            : {lm.get('tokens_per_second')}")
            print(f"  model load       : {lm.get('load_ms')} ms")
        print(f"  answer           : {r['answer_chars']} chars, "
              f"insufficient={r['insufficient']}")

    print("\n== summary ==")
    for key in ("ask_total", "retrieve_total", "query_embed", "vector_search",
                "keyword_search", "identify_person"):
        vals = [r["timings"][key] for r in results]
        print(f"  {key:18}: median {statistics.median(vals):8.1f} ms")
    gps = [r["llm"].get("generation_ms", 0) for r in results if r["llm"]]
    if gps:
        print(f"  {'generation':18}: median {statistics.median(gps):8.1f} ms")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

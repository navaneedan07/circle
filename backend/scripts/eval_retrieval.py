"""Evaluate hybrid retrieval: do BOTH branches contribute, and does the
semantic branch surface meaning-matched text that keyword search cannot?

Usage: python scripts/eval_retrieval.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from circle.ai.embeddings import get_embeddings
from circle.ai.rag import hybrid_retrieve
from circle.config import get_settings
from circle.repository.mongo import MongoStore

store = MongoStore(get_settings())
embedder = get_embeddings()

# Find a person with plenty of memories to make retrieval meaningful.
counts = {}
for d in store.db.memories.aggregate([
        {"$match": {"person_id": {"$nin": [None, ""]}}},
        {"$group": {"_id": "$person_id", "n": {"$sum": 1}}},
        {"$sort": {"n": -1}}]):
    counts[d["_id"]] = d["n"]
if not counts:
    print("no memories with person_id")
    sys.exit(0)
pid = max(counts, key=counts.get)
print(f"evaluating person {pid} with {counts[pid]} memories\n")

QUERIES = [
    # (question, note)
    "what did we discuss about the machine learning model",
    "tell me about the internship",
    "who did I meet recently",
    "any promises I made",
    "fatigue attention factors",
    "submission abstract draft",
]

total_ev = 0
sem = kw = hyb = neither = 0
for q in QUERIES:
    flt = {"person_id": pid}
    ev = hybrid_retrieve(store, embedder, q, flt, k=5,
                         snippet_chars=200, total_chars=1200)
    vec = store.vector_search(embedder.embed(q), flt, k=10)
    kwres = store.keyword_search(q, flt, k=10)
    vec_ids = {m.id for m, _ in vec}
    kw_ids = {m.id for m, _ in kwres}
    print(f"Q: {q}")
    print(f"   vector_hits={len(vec)} keyword_hits={len(kwres)} "
          f"evidence={len(ev)}")
    for e in ev:
        print(f"   {e.label} [{e.matched_by:<8}] score={e.score:.3f} "
              f"v={e.vector_rank} k={e.keyword_rank} {e.text[:44]!r}")
        total_ev += 1
        if e.matched_by == "semantic":
            sem += 1
        elif e.matched_by == "keyword":
            kw += 1
        elif e.matched_by == "hybrid":
            hyb += 1
        else:
            neither += 1
        if e.memory.id not in vec_ids and e.memory.id not in kw_ids:
            print("      !! evidence from NEITHER branch")
    print()

print("=" * 60)
print(f"evidence items: {total_ev}")
print(f"  semantic-only (vector found it, keyword did not): {sem}")
print(f"  keyword-only:  {kw}")
print(f"  hybrid (both): {hyb}")
print(f"  neither:       {neither}")
ok = (sem > 0 or hyb > 0) and (kw > 0 or hyb > 0)
print(f"\nboth branches contributing: {ok}")
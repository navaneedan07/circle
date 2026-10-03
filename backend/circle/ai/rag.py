"""RAG pipeline (spec §20, §21, §39).

question understanding -> person identification -> metadata filters
-> vector retrieval + lexical retrieval -> hybrid ranking (RRF)
-> context assembly -> local Gemma -> validated, cited answer.

Hallucination control: citations are validated against the supplied evidence;
unknown ids are stripped. With zero evidence we never call the LLM at all.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from circle.ai import prompts
from circle.ai.embeddings import EmbeddingProvider
from circle.ai.llm import LLMProvider
from circle.config import get_settings
from circle.domain.models import Memory, Person, RelationshipProfile, SourceType
from circle.repository.mongo import MongoStore

log = logging.getLogger("circle.rag")

MAX_EVIDENCE = 10
EVIDENCE_SNIPPET = 900
_RRF_K = 60

# Fusion weights. The lexical branch is trusted a little less because it can
# only ever match surface forms, while the vector branch carries meaning.
_VECTOR_WEIGHT = 1.0
_KEYWORD_WEIGHT = 0.7


def rag_budget(prep: bool = False) -> tuple[int, int, int]:
    """(max_records, snippet_chars, total_chars) from configuration.

    Prompt length dominates CPU latency, so the evidence block is budgeted.
    """
    s = get_settings()
    if prep:
        return (s.rag_prep_max_evidence, s.rag_prep_evidence_chars,
                s.rag_prep_total_evidence_chars)
    return (s.rag_max_evidence, s.rag_evidence_chars,
            s.rag_total_evidence_chars)


def _trim(text: str, limit: int) -> str:
    """Trim to `limit` chars, preferring a clean word boundary."""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    space = cut.rfind(" ")
    if space > limit * 0.6:
        cut = cut[:space]
    return cut.rstrip() + "…"

_CITE_RE = re.compile(r"\[(S\d+(?:\s*,\s*S\d+)*)\]")

_PREFERRED_SOURCES = {
    "whatsapp": 0, "telegram": 1, "email": 2, "calendar": 3, "voice": 4,
    "notes": 5, "chat": 6, "instagram": 7, "x": 8, "document": 9,
}


@dataclass
class Evidence:
    label: str                 # [S1]
    memory: Memory
    text: str
    score: float = 0.0         # fused hybrid score
    vector_rank: Optional[int] = None    # rank in the semantic branch
    keyword_rank: Optional[int] = None   # rank in the lexical branch

    @property
    def matched_by(self) -> str:
        if self.vector_rank is not None and self.keyword_rank is not None:
            return "hybrid"
        if self.vector_rank is not None:
            return "semantic"
        if self.keyword_rank is not None:
            return "keyword"
        return "none"

    @property
    def citation(self) -> str:
        if self.memory.citation:
            return self.memory.citation
        kind = self.memory.kind
        when = self.memory.occurred_at
        when_s = when.strftime("%b %d, %Y %H:%M") if when else "unknown date"
        return f"{self.memory.source.value} {kind} — {when_s}"


@dataclass
class AnswerResult:
    answer: str
    sources: list[dict[str, Any]] = field(default_factory=list)
    person_id: Optional[str] = None
    person_name: Optional[str] = None
    insufficient: bool = False
    evidence_count: int = 0
    intent: str = "ask"
    latency_ms: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "answer": self.answer,
            "sources": self.sources,
            "person_id": self.person_id,
            "person_name": self.person_name,
            "insufficient": self.insufficient,
            "evidence_count": self.evidence_count,
            "intent": self.intent,
            "latency_ms": self.latency_ms,
        }


# ---------------------------------------------------------------------------
# Question understanding
# ---------------------------------------------------------------------------
@dataclass
class QuestionPlan:
    intent: str = "ask"          # ask | prepare | summary | top_people |
                                 # activity_count | people_count |
                                 # last_interaction | recent_people |
                                 # top_topics
    person_name: Optional[str] = None
    after: Optional[datetime] = None
    source_hint: Optional[str] = None
    topic_hint: Optional[str] = None


_CUTOFF_PATTERNS = [
    (r"\blast (?:24 hours|day|24h)\b", timedelta(days=1)),
    (r"\btoday\b", timedelta(days=1)),
    (r"\byesterday\b", timedelta(days=2)),
    (r"\blast (?:7 days|week)\b", timedelta(days=7)),
    (r"\bthis week\b", timedelta(days=7)),
    (r"\blast (?:14 days|two weeks)\b", timedelta(days=14)),
    (r"\blast (?:30 days|month)\b", timedelta(days=30)),
    (r"\blast (?:90 days|three months)\b", timedelta(days=90)),
    (r"\bthis year\b", timedelta(days=365)),
]

_SOURCE_HINTS = {s.value: s.value for s in SourceType}

# Questions about WHO or HOW MANY are answered from interaction counts, not
# from document retrieval. Retrieving four messages and asking the model to
# name "who I talk to most" yields whoever happens to appear in them, which is
# not a frequency answer at all.
# A frequency marker is required, so "who did I talk to about the internship"
# stays a retrieval question while "who do I talk to the most" does not.
_FREQ_MARK = r"(?:the\s+most|most|frequently|often)"
_TOP_PEOPLE_PATTERNS = [
    # "who do I talk to the most", "who have I spoken to the most"
    r"\bwho (?:do|does|have|has) i "
    r"(?:talk|speak|spoken|chat|text|message|interact)(?:s|ed|ing)?\w*"
    r"\s+(?:to\s+|with\s+)?(?:people\s+|them\s+)?" + _FREQ_MARK,
    # "who are my closest contacts", "what are my top people"
    r"\bwho (?:is|are) (?:my|the|our)\s+"
    r"(?:top|closest|best|main|few|most active|frequent)\w*\s+"
    r"(?:contacts?|people|persons?|friends?)",
    r"\b(?:my|the|our)\s+(?:top|closest|best|main|few|most active)\w*\s+"
    r"(?:contacts?|people|persons?|friends?)",
    # "the most active people", "people I talk to most"
    r"\b(?:most|top)\s+(?:talked|chatted|messaged|active|communicated)\w*"
    r"\s+(?:to\s+)?(?:people|contacts?|persons?|friends?)",
    # "which people do I message most frequently"
    r"\bwhich people\b.*\b" + _FREQ_MARK,
    # "to whom i talk most", "with whom do i speak most often"
    r"\b(?:to|with)\s+whom\b[^.?!]{0,40}?\b" + _FREQ_MARK,
    # "whom do i talk to most"
    r"\bwhom\s+(?:do|does|did|should|will)\s+i\b[^.?!]{0,40}?\b" + _FREQ_MARK,
    # "who i talk most", "who i text the most" (no auxiliary verb)
    r"\bwho\b(?!\s+(?:is|are|was|were)\b)[^.?!]{0,30}?\b"
    r"(?:talk|speak|spoken|chat|text|message|interact)(?:s|ed|ing)?\w*"
    r"[^.?!]{0,20}?\b" + _FREQ_MARK,
    r"\bhow (?:often|many times) (?:do|does) i talk\b",
    r"\bwho do i know (?:the )?(?:best|most)\b",
]

# "...talk ABOUT the internship" is a retrieval question, not a ranking one,
# so it must never be pulled into the deterministic count path.
_TOP_PEOPLE_EXCLUDE = r"\btalk(?:ed|ing|s)?\s+about\b|\bwhat\b.*\btopic"


# ---------------------------------------------------------------------------
# Deterministic archive facts
#
# Everything below is an aggregate the archive already holds exactly. None of
# it needs a language model, and asking one to produce a count yields a
# confident number copied out of a retrieved message: asked "how many people
# have I talked to", the model answered "109 people responded to a trip"
# because that number appeared inside a chat, not because it counted anything.
# ---------------------------------------------------------------------------

# "how many messages / emails / meetings ..." -> the interaction kind to count.
# Longest keys first so "voice notes" wins over "voice".
_ACTIVITY_UNITS: dict[str, Optional[str]] = {
    "voice notes": "voice", "voice note": "voice", "voice messages": "voice",
    "messages": "message", "message": "message",
    "texts": "message", "text": "message",
    "chats": "message", "chat": "message",
    "emails": "email", "email": "email",
    "mails": "email", "mail": "email",
    "meetings": "meeting", "meeting": "meeting",
    "calls": "meeting", "call": "meeting",
    "interactions": None, "interaction": None,
}
_ACTIVITY_PATTERN = (
    r"\b(?:how many|how much)\s+(?:of\s+my\s+|my\s+)?(?:"
    + "|".join(sorted(_ACTIVITY_UNITS, key=len, reverse=True))
    + r")\b"
)
# Singular label per counted kind, so "1 email" never renders as "1 emails".
_ACTIVITY_SINGULAR = {
    "message": "message", "email": "email", "meeting": "meeting",
    "voice": "voice note", None: "interaction",
}
# "how many times did I talk to X" is an interaction count for one person.
_ACTIVITY_TIMES = r"\bhow many times\b[^.?!]{0,40}?\b(?:talk|speak|spoke|chat|text|message|interact|call|meet)\w*"
# A count scoped to a subject ("how many messages did he say ABOUT the trip")
# cannot be answered by a plain aggregate, so it stays a retrieval question
# rather than silently counting every message that person ever sent.
_ACTIVITY_EXCLUDE = r"\babout\b"
# A unit count asked about the user's own archive. "how many people responded
# to the trip" is a question about a chat, and is deliberately not matched:
# people_count requires an explicit I-relation to the archive.
_PEOPLE_COUNT = (
    r"\bhow (?:many|much)\s+(?:of\s+my\s+)?(?:people|persons?|contacts?|friends?)\b"
    r"[^.?!]{0,30}?\b(?:i|we|me|my|our)\b"
    r"|\bhow (?:many|much)\s+(?:people|persons?|contacts?|friends?)\s+(?:do|did|have|has)\s+(?:i|we)\b"
    r"|\b(?:number|count) of (?:people|persons?|contacts?|friends?)\b[^.?!]{0,20}?\b(?:i|we|my|our)\b"
)
_LAST_INTERACTION = (
    r"\bwhen (?:did|do|was|were)\s+(?:i|we)\s+(?:last\s+)?"
    r"(?:talk|speak|chat|text|message|interact|call|meet|hear from)\w*"
    r"|\blast time\s+(?:i|we)\s+(?:talked|spoke|chatted|texted|messaged|met|called)\b"
    r"|\bwhen did i last hear from\b"
)
# "talk ABOUT X" stays a retrieval question even with a last-interaction verb.
_LAST_INTERACTION_EXCLUDE = r"\btalk(?:ed|ing)?\s+about\b|\bdiscuss(?:ed|ion)?\b|\bconcerning\b"

# "who did I talk to in the last 7 days" has no superlative in it, so the
# ranking router misses it, yet it is still a group-by over the window.
_RECENT_PEOPLE = (
    r"\bwho\b[^.?!]{0,30}?\b(?:talk|speak|spoken|chat|text|message|interact|meet|call)\w*"
)
_RECENT_PEOPLE_EXCLUDE = r"\btalk(?:ed|ing)?\s+about\b|\bdiscuss\b"

_TOP_TOPICS = (
    r"\b(?:my|our|the)\s+(?:top|main|common|frequent|main)\w*\s+topics?\b"
    r"|\btopics?\b[^.?!]{0,30}?\b(?:most|frequently|often)\b"
    r"|\bwhat (?:do|does|did) (?:i|we)\s+(?:talk|chat|speak)\s+about\s+"
    r"(?:the\s+)?most\b"
    r"|\bwhat are (?:my|our)\s+(?:main\s+)?topics\b"
)
_TOP_TOPICS_EXCLUDE = r"\bwhen\b[^.?!]{0,20}?\b(?:discuss|talk)\b|\bwhy\b"

# Only a question carrying an explicit time window counts as windowed.
_CUTOFF_WINDOWED = "|".join(p for p, _ in _CUTOFF_PATTERNS)


# Intents answered from the archive's own aggregates. None of them call the
# model, so each one is exact by construction.
_DETERMINISTIC_INTENTS = {"activity_count", "people_count", "last_interaction",
                          "recent_people", "top_topics"}


def _window_phrase(plan: QuestionPlan) -> str:
    """Human phrasing of the recognised time window."""
    if not plan.after:
        return "across your whole imported archive"
    days = max(1, round((datetime.now(timezone.utc) - plan.after).total_seconds()
                        / 86400))
    noun = "day" if days == 1 else "days"
    return f"in the last {days} {noun}"


def _fmt_int(n: int) -> str:
    return f"{n:,}"


def _plural(n: int, singular: str, many: Optional[str] = None) -> str:
    """'1 email' / '3 emails'. Counts of one read as an error otherwise."""
    return singular if n == 1 else (many or singular + "s")


def _activity_kind(question: str) -> tuple[Optional[str], str]:
    """(relationship_event kind to count, singular human label) for a question."""
    q = question.lower()
    for unit in sorted(_ACTIVITY_UNITS, key=len, reverse=True):
        if re.search(rf"\b{re.escape(unit)}\b", q):
            kind = _ACTIVITY_UNITS[unit]
            return kind, _ACTIVITY_SINGULAR.get(kind, kind or "interaction")
    return None, "interaction"


def classify(question: str) -> str:
    """Route a question to the code that can actually answer it.

    Countable questions go to MongoDB aggregations. Only questions that need
    reading and phrasing stay with the model.
    """
    q = question.lower()

    if any(k in q for k in ("prepare me", "prepare for", "brief for",
                            "get me ready", "meeting brief")):
        return "prepare"
    if (not re.search(_TOP_PEOPLE_EXCLUDE, q)
            and any(re.search(p, q) for p in _TOP_PEOPLE_PATTERNS)):
        return "top_people"
    if any(k in q for k in ("relationship", "how is our relationship",
                            "relationship with")):
        return "summary"
    # Counting intents come after top_people so its existing phrasings, several
    # of which contain "how many", keep their existing answers.
    if not re.search(_TOP_TOPICS_EXCLUDE, q) and re.search(_TOP_TOPICS, q):
        return "top_topics"
    if not re.search(_LAST_INTERACTION_EXCLUDE, q) and \
            re.search(_LAST_INTERACTION, q):
        return "last_interaction"
    if not re.search(_ACTIVITY_EXCLUDE, q) and (
            re.search(_ACTIVITY_PATTERN, q)
            or re.search(_ACTIVITY_TIMES, q)):
        return "activity_count"
    if re.search(_PEOPLE_COUNT, q):
        return "people_count"
    # A windowed "who did I talk to" is a group-by over that window. Only when
    # a cutoff was recognised: without one, "who did I talk to" is open-ended
    # and the ranking intent or retrieval is the better answer.
    if re.search(_CUTOFF_WINDOWED, q) and \
            not re.search(_RECENT_PEOPLE_EXCLUDE, q) and \
            re.search(_RECENT_PEOPLE, q):
        return "recent_people"
    return "ask"


def understand(question: str) -> QuestionPlan:
    plan = QuestionPlan()
    q = question.lower()
    plan.intent = classify(question)
    for pattern, delta in _CUTOFF_PATTERNS:
        if re.search(pattern, q):
            plan.after = datetime.now(timezone.utc) - delta
            break
    for src in _SOURCE_HINTS:
        if re.search(rf"\b{re.escape(src)}\b", q):
            plan.source_hint = src
            break
    if "voice note" in q or "voice memo" in q or "recording" in q:
        plan.source_hint = "voice"
    return plan


def identify_person(store: MongoStore, question: str) -> Optional[Person]:
    """Deterministic name matching against people/aliases. Returns None when
    ambiguous -- the caller then searches across all people."""
    people = store.list_people(limit=1000)
    if not people:
        return None
    q = question.lower()
    matches: list[tuple[int, Person]] = []
    for p in people:
        names = [p.display_name, *p.aliases]
        best = 0
        for name in names:
            if not name:
                continue
            n = name.lower()
            if re.search(rf"\b{re.escape(n)}\b", q):
                best = max(best, len(n))
        if best:
            matches.append((best, p))
    if not matches:
        return None
    matches.sort(key=lambda t: -t[0])
    # Unique best match (clear length gap) wins; otherwise prefer most recent
    if len(matches) > 1 and matches[0][0] == matches[1][0]:
        return matches[0][1]
    return matches[0][1]


# ---------------------------------------------------------------------------
# Hybrid retrieval
# ---------------------------------------------------------------------------
def _filters_for(person: Optional[Person], plan: QuestionPlan) -> dict[str, Any]:
    f: dict[str, Any] = {}
    if person and person.id:
        f["person_id"] = person.id
    if plan.source_hint:
        f["source"] = plan.source_hint
    if plan.after:
        f["occurred_at"] = {"$gte": plan.after.isoformat()}
    return f


def _as_datetime(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def hybrid_retrieve(store: MongoStore, embedder: EmbeddingProvider,
                    question: str, filters: dict[str, Any],
                    k: int = MAX_EVIDENCE, snippet_chars: int = EVIDENCE_SNIPPET,
                    total_chars: int = 0) -> list[Evidence]:
    vector_hits: list[tuple[Memory, float]] = []
    keyword_hits: list[tuple[Memory, float]] = []
    try:
        vec = embedder.embed(question)
        vector_hits = store.vector_search(vec, filters, k=k * 2)
    except Exception as e:
        log.warning("vector retrieval failed: %s", e)
    try:
        keyword_hits = store.keyword_search(question, filters, k=k * 2)
    except Exception as e:
        log.warning("keyword retrieval failed: %s", e)
    log.info("retrieval: vector=%d keyword=%d", len(vector_hits), len(keyword_hits))

    # Weighted, length-normalized reciprocal rank fusion.
    #
    # Plain RRF (1/(k+rank)) lets a list of 1 outrank a list of 10 whenever the
    # single item sits at rank 0, because the terms are not comparable across
    # lists of different sizes. Normalizing each list by its own length and
    # weighting the branches keeps one strong lexical hit from crowding out a
    # whole vector neighbourhood.
    fused: dict[str, float] = {}
    memos: dict[str, Memory] = {}
    vector_ranks: dict[str, int] = {}
    keyword_ranks: dict[str, int] = {}

    def _fuse(hits, weight, rank_map):
        if not hits:
            return
        n = len(hits)
        for rank, (m, _) in enumerate(hits):
            memos.setdefault(m.id, m)
            rank_map[m.id] = rank
            normalized = 1.0 - (rank / n)      # 1.0 for best, ->0 for last
            fused[m.id] = fused.get(m.id, 0.0) + weight * normalized

    _fuse(sorted(vector_hits, key=lambda t: -t[1]), _VECTOR_WEIGHT, vector_ranks)
    _fuse(sorted(keyword_hits, key=lambda t: -t[1]), _KEYWORD_WEIGHT, keyword_ranks)

    ranked = sorted(fused.items(), key=lambda kv: (-kv[1], kv[0]))
    out: list[Evidence] = []
    used_chars = 0
    for (mid, _) in ranked:
        if len(out) >= k:
            break
        m = memos[mid]
        snippet = _trim((m.summary or m.text or "").strip(), snippet_chars)
        if not snippet:
            continue
        # Respect the total context budget (at least one record always fits)
        if total_chars and used_chars + len(snippet) > total_chars and out:
            continue
        used_chars += len(snippet)
        out.append(Evidence(label=f"S{len(out)+1}", memory=m, text=snippet,
                            score=fused[mid],
                            vector_rank=vector_ranks.get(mid),
                            keyword_rank=keyword_ranks.get(mid)))
    # Keep the fused relevance order: it IS the hybrid ranking. Previously this
    # re-sorted by source then recency, which discarded fusion entirely and
    # made every answer look like a keyword/source-preference result.
    for i, e in enumerate(out):
        e.label = f"S{i + 1}"
    return out


def _format_evidence(evidence: list[Evidence], person: Optional[Person]) -> str:
    lines = []
    for e in evidence:
        when = _as_datetime(e.memory.occurred_at)
        when_s = when.strftime("%Y-%m-%d %H:%M") if when else "unknown"
        header = (f"[{e.label}] {e.memory.source.value} {e.memory.kind} | "
                  f"{when_s}" + (f" | person: {person.display_name}" if person else ""))
        lines.append(f"{header}\n\"{e.text}\"")
    return "\n\n".join(lines)


def _sources_payload(evidence: list[Evidence]) -> list[dict[str, Any]]:
    return [{
        "label": e.label,
        "memory_id": e.memory.id,
        "record_id": e.memory.record_id,
        "source": e.memory.source.value,
        "kind": e.memory.kind,
        "origin": e.memory.origin.value,
        "occurred_at": e.memory.occurred_at.isoformat() if isinstance(e.memory.occurred_at, datetime) else e.memory.occurred_at,
        "citation": e.citation,
        "snippet": e.text[:220],
    } for e in evidence]


def _validate_citations(answer: str, evidence: list[Evidence]) -> tuple[str, list[dict]]:
    valid = {e.label: e for e in evidence}
    used: list[str] = []

    def _replace(match: re.Match) -> str:
        tokens = re.findall(r"S\d+", match.group(1))
        good = [t for t in tokens if t in valid]
        for t in good:
            if t not in used:
                used.append(t)
        return "[" + ", ".join(good) + "]" if good else ""

    # Remove citations that don't map to real evidence; keep valid ones
    cleaned = _CITE_RE.sub(_replace, answer)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    payload = []
    for label in used:
        e = valid[label]
        payload.append({
            "label": label,
            "memory_id": e.memory.id,
            "record_id": e.memory.record_id,
            "source": e.memory.source.value,
            "kind": e.memory.kind,
            "origin": e.memory.origin.value,
            "occurred_at": e.memory.occurred_at.isoformat() if isinstance(e.memory.occurred_at, datetime) else e.memory.occurred_at,
            "citation": e.citation,
            "snippet": e.text[:220],
        })
    return cleaned, payload


INSUFFICIENT = "I couldn't find enough evidence in your imported data."


# ---------------------------------------------------------------------------
# Main entry points
# ---------------------------------------------------------------------------
class RagPipeline:
    def __init__(self, store: MongoStore, embedder: EmbeddingProvider,
                 llm: LLMProvider):
        self.store = store
        self.embedder = embedder
        self.llm = llm

    # ------------------------------------------------------------------
    def _gather(self, question: str, person_id: Optional[str] = None):
        """Shared front half of every ask: plan → person → retrieval."""
        import time
        result = AnswerResult(answer="")
        plan = understand(question)
        result.intent = plan.intent

        # Counting questions are answered from the interaction ledger itself.
        # Retrieving messages and asking the model to rank people produces
        # whoever happens to appear in the retrieved sample, so this branch
        # skips retrieval and generation entirely.
        if plan.intent == "top_people":
            result.person_id = None
            result.person_name = None
            self._top_people(plan, result)
            return plan, None, [], result

        # The other aggregates need the person resolved first ("how many
        # messages did Navaneedan send me"), so they run after identification.
        person: Optional[Person] = None
        if person_id:
            person = self.store.get_person(person_id)
        if person is None:
            person = identify_person(self.store, question)
        if person:
            result.person_id, result.person_name = person.id, person.display_name

        if plan.intent in _DETERMINISTIC_INTENTS:
            self._archive_fact(plan, result, person, question)
            return plan, person, [], result

        t0 = time.time()
        filters = _filters_for(person, plan)
        prep = plan.intent == "prepare"
        k, snippet_chars, total_chars = rag_budget(prep)
        evidence = hybrid_retrieve(self.store, self.embedder, question, filters,
                                   k=k, snippet_chars=snippet_chars,
                                   total_chars=total_chars)
        result.latency_ms["retrieval"] = int((time.time() - t0) * 1000)
        result.evidence_count = len(evidence)
        return plan, person, evidence, result

    def _fact_source(self, result: AnswerResult, label: str, kind: str,
                     snippet: str, citation: str, person_id: Optional[str] = None,
                     person_name: Optional[str] = None,
                     occurred_at: Optional[str] = None) -> None:
        """One aggregate, shown as a source so the answer stays checkable."""
        result.sources.append({
            "label": label, "memory_id": None, "person_id": person_id,
            "person_name": person_name, "source": "archive", "kind": kind,
            "origin": "local", "occurred_at": occurred_at,
            "citation": citation, "snippet": snippet,
        })

    def _archive_fact(self, plan: QuestionPlan, result: AnswerResult,
                      person: Optional[Person], question: str) -> None:
        """Answer a countable question from MongoDB, never from the model.

        Each branch is a single aggregate. There is no generation step, so the
        number in the answer is the number in the database.
        """
        import time
        t0 = time.time()
        scope = _window_phrase(plan)
        who = person.display_name if person else None
        pid = person.id if person else None

        try:
            if plan.intent == "activity_count":
                kind, unit = _activity_kind(question)
                totals = self.store.activity_totals(
                    since=plan.after, person_id=pid, source=plan.source_hint)
                by_kind = totals["by_kind"]
                if kind:
                    # A named unit counts only that kind, not everything.
                    count = next((v for k, v in by_kind.items()
                                  if k == kind), 0)
                else:
                    count = totals["total"]
                subject = f"with {who}" if who else "in your archive"
                label = f"{_fmt_int(count)} {_plural(count, unit)}"
                result.evidence_count = 1
                result.answer = (
                    f"You have {label} {subject} {scope}.\n\n"
                    f"Counted directly from your imported records, not "
                    f"estimated.")
                self._fact_source(
                    result, "C1", "activity_count",
                    f"{label} {subject} {scope}.",
                    f"{label} {scope}")
                if len(by_kind) > 1:
                    detail = ", ".join(
                        f"{_plural(v, _ACTIVITY_SINGULAR.get(k, k))} "
                        f"{_fmt_int(v)}" for k, v in
                        sorted(by_kind.items(), key=lambda kv: -kv[1]))
                    result.answer += f"\n\nBy type: {detail}."

            elif plan.intent == "people_count":
                count = self.store.distinct_people(since=plan.after)
                total = self.store.count_people()
                result.evidence_count = 1
                result.answer = (
                    f"You have {_fmt_int(count)} distinct "
                    f"{_plural(count, 'person', 'people')} in your archive "
                    f"{scope}.")
                # Only worth saying when the window actually narrows it.
                if total and total != count:
                    result.answer += (
                        f"\n\nThat is {_fmt_int(count)} of "
                        f"{_fmt_int(total)} people in your archive, "
                        f"{_fmt_int(total - count)} with nothing recorded "
                        f"in that period.")
                self._fact_source(
                    result, "C1", "people_count",
                    f"{_fmt_int(count)} distinct people {scope}.",
                    f"{_fmt_int(count)} distinct people {scope}")

            elif plan.intent == "last_interaction":
                last = self.store.last_interaction(person_id=pid)
                when = _as_datetime(last)
                if not when:
                    result.insufficient = True
                    result.answer = (
                        f"I could not find any recorded interaction "
                        f"{'with ' + who if who else 'in your archive'}.")
                    return
                profile = self.store.get_profile(pid) if pid else None
                count = profile.interaction_count if profile else 0
                label = who or "anyone"
                result.evidence_count = 1
                result.answer = (
                    f"Your last interaction {('with ' + who) if who else 'in your archive'} "
                    f"was on {when.strftime('%B %d, %Y at %H:%M UTC')}.\n\n"
                    + (f"That is {_fmt_int(count)} recorded interactions with "
                       f"{who} in total." if who else ""))
                self._fact_source(
                    result, "C1", "last_interaction",
                    f"Most recent interaction {label}: {last}.",
                    f"last interaction {last}", person_id=pid,
                    person_name=who, occurred_at=last)

            elif plan.intent == "recent_people":
                rows = self.store.recent_people(plan.after, limit=10)
                result.evidence_count = len(rows)
                if not rows:
                    result.insufficient = True
                    result.answer = (f"I could not find any recorded "
                                     f"interactions {scope}.")
                    return
                people = {p.id: p for p in self.store.list_people(limit=1000)}
                lines = []
                for rank, (rpid, count, last) in enumerate(rows, start=1):
                    rperson = people.get(rpid)
                    name = rperson.display_name if rperson else "Unknown contact"
                    when = _as_datetime(last)
                    when_s = when.strftime("%b %d, %Y") if when else "unknown"
                    lines.append(f"{rank}. {name}: {_fmt_int(count)} "
                                 f"interactions, last on {when_s}")
                    self._fact_source(
                        result, f"P{rank}", "interaction_count",
                        f"{_fmt_int(count)} interactions {scope}.",
                        f"{name}: {_fmt_int(count)} interactions {scope}",
                        person_id=rpid, person_name=name, occurred_at=last)
                result.answer = (
                    f"People you interacted with {scope}, most active first:\n\n"
                    + "\n".join(lines)
                    + "\n\nCounted from your own import history.")

            elif plan.intent == "top_topics":
                rows = self.store.topic_totals(limit=8)
                result.evidence_count = len(rows)
                if not rows:
                    result.insufficient = True
                    result.answer = ("I have not extracted any topics from "
                                     "your archive yet.")
                    return
                lines = [f"{i}. {topic}: {_fmt_int(count)} mentions"
                         for i, (topic, count) in enumerate(rows, start=1)]
                for i, (topic, count) in enumerate(rows, start=1):
                    self._fact_source(
                        result, f"T{i}", "topic_count",
                        f"{topic}: {count} mentions across your profiles.",
                        f"{topic}: {_fmt_int(count)} mentions")
                result.answer = (
                    f"Most-discussed topics across your archive:\n\n"
                    + "\n".join(lines)
                    + "\n\nSummed from the per-person topic profiles.")
        except Exception as e:
            log.error("archive fact failed for %s: %s", plan.intent, e)
            # A failed aggregate must not produce a fabricated number.
            result.insufficient = True
            result.answer = ("I could not read that from your archive just "
                             "now. Try asking again.")
            return
        result.latency_ms["retrieval"] = int((time.time() - t0) * 1000)

    def _top_people(self, plan: QuestionPlan, result: AnswerResult) -> None:
        """Answer "who do I talk to the most" from interaction counts.

        Deterministic by design. The model is never asked to rank people from
        a sample of retrieved messages, because a sample cannot express
        frequency: it can only surface whoever is in it.
        """
        import time
        t0 = time.time()
        try:
            totals = self.store.interaction_totals(limit=5, since=plan.after)
        except Exception as e:
            log.error("interaction_totals failed: %s", e)
            totals = []
        result.latency_ms["retrieval"] = int((time.time() - t0) * 1000)
        result.evidence_count = len(totals)

        if not totals:
            window = "in that period" if plan.after else "in your imported data"
            result.answer = (f"I could not find any recorded interactions "
                             f"{window}.")
            result.insufficient = True
            return

        people = {p.id: p for p in self.store.list_people(limit=1000)}
        scope = ("in that period" if plan.after
                 else "across every import you have added")
        lines: list[str] = []
        for rank, (pid, count, last) in enumerate(totals, start=1):
            person = people.get(pid)
            name = person.display_name if person else "Unknown contact"
            when = _as_datetime(last)
            last_s = when.strftime("%b %d, %Y") if when else "unknown date"
            lines.append(f"{rank}. {name}: {count:,} interactions, last on {last_s}")
            result.sources.append({
                "label": f"P{rank}",
                "memory_id": None,
                "person_id": pid,
                "person_name": name,
                "source": "archive",
                "kind": "interaction_count",
                "origin": "local",
                "occurred_at": last,
                "citation": f"{name}: {count:,} interactions, last {last_s}",
                "snippet": f"{count:,} recorded interactions {scope}.",
            })
        result.answer = (
            f"Ranked by recorded interaction count {scope}:\n\n"
            + "\n".join(lines)
            + "\n\nCounts come from your own import history, summed per person."
        )

    def _answer_prompt(self, person: Optional[Person], question: str,
                       evidence: list[Evidence]) -> str:
        person_block = (f"{person.display_name} (id {person.id})" if person
                        else "not specified")
        return prompts.fill(
            prompts.QUESTION_ANSWER_PROMPT,
            person_block=person_block,
            question=question,
            evidence=_format_evidence(evidence, person),
        )

    def ask_stream(self, question: str, person_id: Optional[str] = None):
        """Yield SSE-style events so answers render while they are generated.

        Event shapes:
          {"type": "meta", person_id, person_name, intent, evidence_count}
          {"type": "token", "text": "..."}
          {"type": "done", ...AnswerResult.to_dict(), "metrics": {...}}
          {"type": "error", "error": "..."}
        """
        plan, person, evidence, result = self._gather(question, person_id)
        yield {
            "type": "meta",
            "person_id": result.person_id,
            "person_name": result.person_name,
            "intent": plan.intent,
            "evidence_count": result.evidence_count,
        }
        if plan.intent in ("top_people", *_DETERMINISTIC_INTENTS):
            yield {"type": "done", **result.to_dict()}
            return
        if not evidence:
            result.answer = INSUFFICIENT
            result.insufficient = True
            yield {"type": "done", **result.to_dict()}
            return

        if plan.intent == "summary" and person:
            yield {"type": "done", **self._summary(person, evidence, result).to_dict()}
            return

        if plan.intent == "prepare":
            prompt = prompts.fill(
                prompts.MEETING_PREP_PROMPT,
                name=person.display_name if person else "this person",
                meeting_line=question,
                evidence=_format_evidence(evidence, person))
            system = prompts.MEETING_PREP_SYSTEM
            max_tokens = get_settings().llm_max_prep_tokens
        else:
            prompt = self._answer_prompt(person, question, evidence)
            system = prompts.QUESTION_ANSWER_SYSTEM
            max_tokens = get_settings().llm_max_answer_tokens

        import time
        t = time.time()
        chunks: list[str] = []
        try:
            for chunk in self.llm.stream(prompt, system=system, temperature=0.2,
                                         max_tokens=max_tokens):
                chunks.append(chunk)
                yield {"type": "token", "text": chunk}
        except Exception as e:
            log.error("streaming LLM failed: %s", e)
            yield {"type": "error", "error": f"Local AI is unavailable: {e}"}
            result.answer = f"Local AI is unavailable: {e}"
            yield {"type": "done", **result.to_dict()}
            return
        result.latency_ms["generation"] = int((time.time() - t) * 1000)

        answer = "".join(chunks)
        cleaned, sources = _validate_citations(answer, evidence)
        if not sources:
            result.answer = INSUFFICIENT
            result.insufficient = True
            result.sources = []
        else:
            result.answer = cleaned.strip()
            result.sources = sources
        yield {"type": "done", **result.to_dict(),
               "metrics": dict(getattr(self.llm, "last_metrics", {}) or {})}

    def ask(self, question: str, person_id: Optional[str] = None,
            history: Optional[list[dict]] = None) -> AnswerResult:
        import time
        plan, person, evidence, result = self._gather(question, person_id)

        if plan.intent in ("top_people", *_DETERMINISTIC_INTENTS):
            return result

        if not evidence:
            result.answer = INSUFFICIENT
            result.insufficient = True
            return result

        if plan.intent == "prepare":
            return self._prepare(person, evidence, result, question)
        if plan.intent == "summary" and person:
            return self._summary(person, evidence, result)

        prompt = self._answer_prompt(person, question, evidence)
        t1 = time.time()
        try:
            answer = self.llm.complete(
                prompt, system=prompts.QUESTION_ANSWER_SYSTEM,
                temperature=0.2,
                max_tokens=get_settings().llm_max_answer_tokens)
        except Exception as e:
            log.error("LLM failed: %s", e)
            result.answer = ("Local AI is unavailable right now "
                             f"({e}). Your data and search still work.")
            result.sources = _sources_payload(evidence[:5])
            return result
        result.latency_ms["generation"] = int((time.time() - t1) * 1000)

        if INSUFFICIENT.lower()[:30] in answer.lower() and "[" not in answer:
            result.answer = INSUFFICIENT
            result.insufficient = True
            return result

        cleaned, sources = _validate_citations(answer, evidence)
        if not sources:
            # Model gave no citations: fall back to refusing rather than
            # presenting uncited claims as evidence-backed.
            result.answer = INSUFFICIENT
            result.insufficient = True
            result.sources = []
            return result
        result.answer = cleaned.strip()
        result.sources = sources
        return result

    def prepare(self, person_id: str, meeting_line: str = "") -> AnswerResult:
        person = self.store.get_person(person_id)
        result = AnswerResult(answer="", person_id=person_id,
                              person_name=person.display_name if person else None,
                              intent="prepare")
        if not person:
            result.answer = INSUFFICIENT
            result.insufficient = True
            return result
        filters = {"person_id": person.id}
        k, snippet_chars, total_chars = rag_budget(prep=True)
        evidence = hybrid_retrieve(self.store, self.embedder,
                                   f"recent conversations with {person.display_name}",
                                   filters, k=k, snippet_chars=snippet_chars,
                                   total_chars=total_chars)
        result.evidence_count = len(evidence)
        if not evidence:
            result.answer = INSUFFICIENT
            result.insufficient = True
            return result
        return self._prepare(person, evidence, result, meeting_line)

    def _prepare(self, person: Optional[Person], evidence: list[Evidence],
                 result: AnswerResult, meeting_line: str) -> AnswerResult:
        import time
        prompt = prompts.fill(
            prompts.MEETING_PREP_PROMPT,
            name=person.display_name if person else "this person",
            meeting_line=meeting_line or "upcoming meeting",
            evidence=_format_evidence(evidence, person),
        )
        t = time.time()
        try:
            answer = self.llm.complete(prompt, system=prompts.MEETING_PREP_SYSTEM,
                                       temperature=0.2,
                                       max_tokens=get_settings().llm_max_prep_tokens)
        except Exception as e:
            log.error("LLM failed in prepare: %s", e)
            result.answer = f"Local AI unavailable: {e}"
            return result
        result.latency_ms["generation"] = int((time.time() - t) * 1000)
        cleaned, sources = _validate_citations(answer, evidence)
        if not sources:
            result.answer = INSUFFICIENT
            result.insufficient = True
            return result
        result.answer = cleaned.strip()
        result.sources = sources
        return result

    def _summary(self, person: Person, evidence: list[Evidence],
                 result: AnswerResult) -> AnswerResult:
        import time
        profile = self.store.get_profile(person.id) if person.id else None
        if profile is None:
            result.answer = INSUFFICIENT
            result.insufficient = True
            return result
        excerpts = "\n".join(f"- {e.citation}: {e.text[:200]}" for e in evidence[:8])
        prompt = prompts.fill(
            prompts.RELATIONSHIP_SUMMARY_PROMPT,
            name=person.display_name,
            total=str(profile.interaction_count),
            recent14=str(profile.interactions_14d),
            last=str(profile.last_interaction_at or "never"),
            sources=", ".join(f"{k}:{v}" for k, v in profile.source_breakdown.items()),
            topics=", ".join(t.topic for t in profile.topics[:6]),
            excerpts=excerpts,
        )
        t = time.time()
        try:
            answer = self.llm.complete(
                prompt, system=prompts.RELATIONSHIP_SUMMARY_SYSTEM,
                temperature=0.3, max_tokens=300)
        except Exception as e:
            log.error("LLM summary failed: %s", e)
            result.answer = f"Local AI unavailable: {e}"
            return result
        result.latency_ms["generation"] = int((time.time() - t) * 1000)
        result.answer = answer.strip()
        result.sources = _sources_payload(evidence[:5])
        # refresh stored profile summary
        try:
            if person.id:
                profile.summary = answer.strip()
                profile.summary_sources = [e.memory.id or "" for e in evidence[:5]]
                self.store.upsert_profile(profile)
        except Exception:
            pass
        return result

"""Relationship metrics (explainable status) + RAG citation validation."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from circle.ai.rag import (
    INSUFFICIENT, AnswerResult, Evidence, _validate_citations, understand,
)
from circle.domain.models import (
    Memory, Message, Person, RelationshipEvent, SourceType,
)
from circle.parsers.common import message_id
from circle.relationship.metrics import compute_profile, extract_topics


def _add_events(store, person_id: str, n: int, within: timedelta,
                source: str = "whatsapp"):
    now = datetime.now(timezone.utc)
    for i in range(n):
        store.insert_relationship_event(RelationshipEvent(
            person_id=person_id, kind="message", source=source,  # type: ignore
            occurred_at=now - within + timedelta(minutes=i),
            summary=f"msg {i}", record_id=f"r{person_id}-{i}-{n}"))


class TestRelationshipStatus:
    def test_no_data_status(self, clean_store, resolver):
        p, _ = resolver.resolve_sender("Alice", SourceType.WHATSAPP)
        prof = compute_profile(clean_store, p.id)
        assert prof.status.value == "No Recent Activity"
        assert prof.status_reason

    def test_very_active(self, clean_store, resolver):
        p, _ = resolver.resolve_sender("Alice", SourceType.WHATSAPP)
        _add_events(clean_store, p.id, 20, timedelta(days=3))
        prof = compute_profile(clean_store, p.id)
        assert prof.status.value == "Very Active"
        assert "20 interactions" in prof.status_reason or \
            "interactions in the last 14 days" in prof.status_reason

    def test_active_explainable(self, clean_store, resolver):
        p, _ = resolver.resolve_sender("Bob", SourceType.WHATSAPP)
        _add_events(clean_store, p.id, 12, timedelta(days=5))
        prof = compute_profile(clean_store, p.id)
        assert prof.status.value == "Active"
        assert "last 14 days" in prof.status_reason

    def test_occasional(self, clean_store, resolver):
        p, _ = resolver.resolve_sender("Cara", SourceType.TELEGRAM)
        _add_events(clean_store, p.id, 4, timedelta(days=40))
        prof = compute_profile(clean_store, p.id)
        assert prof.status.value in ("Occasional", "Low Activity")

    def test_stale_no_recent(self, clean_store, resolver):
        p, _ = resolver.resolve_sender("Dan", SourceType.TELEGRAM)
        now = datetime.now(timezone.utc)
        clean_store.insert_relationship_event(RelationshipEvent(
            person_id=p.id, kind="message", source=SourceType.TELEGRAM,  # type: ignore
            occurred_at=now - timedelta(days=90), summary="old",
            record_id="old-1"))
        prof = compute_profile(clean_store, p.id)
        assert prof.status.value == "No Recent Activity"
        assert "90 days" in prof.status_reason

    def test_never_infers_emotions(self, clean_store, resolver):
        p, _ = resolver.resolve_sender("Eve", SourceType.WHATSAPP)
        _add_events(clean_store, p.id, 1, timedelta(days=40))
        prof = compute_profile(clean_store, p.id)
        banned = ("angry", "depressed", "sad", "hate", "like you",
                  "disappointed", "upset")
        blob = (prof.status_reason + " " + prof.summary).lower()
        assert not any(b in blob for b in banned)

    def test_topics_extracted(self, clean_store, resolver):
        p, _ = resolver.resolve_sender("Farah", SourceType.WHATSAPP)
        mem = Memory(id="mem-x", person_id=p.id, kind="message",
                     source=SourceType.WHATSAPP,
                     occurred_at=datetime.now(timezone.utc),
                     text="Farah: SIH project update", record_id="x",
                     citation="WhatsApp",
                     topics=extract_topics("Farah: SIH project update"))
        clean_store.insert_memories([mem])
        prof = compute_profile(clean_store, p.id)
        assert any("SIH" in t.topic for t in prof.topics)

    def test_upcoming_events_listed(self, clean_store, resolver):
        p, _ = resolver.resolve_sender("Gia", SourceType.CALENDAR)
        from circle.domain.models import CalendarEvent
        clean_store.insert_calendar_if_new(CalendarEvent(
            id="ev1", title="Sync",
            starts_at=datetime.now(timezone.utc) + timedelta(days=2),
            person_ids=[p.id], source=SourceType.CALENDAR))
        prof = compute_profile(clean_store, p.id)
        assert prof.upcoming_events


class TestTopicExtraction:
    def test_capitalized_phrases(self):
        topics = extract_topics("Working on the SIH project with Machine Learning")
        assert any("SIH" in t for t in topics)

    def test_empty(self):
        assert extract_topics("") == []

    def test_stopwords_excluded(self):
        topics = extract_topics("the and for you your that with this")
        assert not any(t in ("the", "and", "for") for t in topics)


class TestCitationValidation:
    def _evidence(self):
        m1 = Memory(id="m1", kind="message", source=SourceType.WHATSAPP,
                    occurred_at=datetime(2024, 9, 28, 20, 42,
                                         tzinfo=timezone.utc),
                    text="SIH meeting", record_id="r1",
                    citation="WhatsApp — Sept 28, 8:42 PM")
        m2 = Memory(id="m2", kind="note", source=SourceType.NOTES,
                    occurred_at=datetime(2024, 9, 30, tzinfo=timezone.utc),
                    text="note text", record_id="r2", citation="Note — Sept 30")
        return [Evidence(label="S1", memory=m1, text="SIH meeting"),
                Evidence(label="S2", memory=m2, text="note text")]

    def test_valid_citations_kept(self):
        answer = "You discussed SIH [S1]. Sources: [S1], [S2]"
        cleaned, sources = _validate_citations(answer, self._evidence())
        assert "[S1]" in cleaned
        assert {s["label"] for s in sources} == {"S1", "S2"}

    def test_fabricated_citation_stripped(self):
        answer = "Claim [S99] and claim [S1]. Sources: [S99], [S1]"
        cleaned, sources = _validate_citations(answer, self._evidence())
        assert "[S99]" not in cleaned
        assert "[S99]" not in str(sources)
        assert "S1" in {s["label"] for s in sources}

    def test_no_citations_yields_no_sources(self):
        answer = "I made this up entirely."
        _, sources = _validate_citations(answer, self._evidence())
        assert sources == []


class TestEvidenceBudget:
    """Prompt size dominates CPU latency, so the context budget must hold."""

    def _seed(self, clean_store, resolver, fake_embedder, n=10):
        p, _ = resolver.resolve_sender("Budget Person", SourceType.WHATSAPP)
        text = "budget " * 60          # 420 chars each
        clean_store.insert_memories([
            Memory(id=f"mem-b{i}", person_id=p.id, kind="message",
                   source=SourceType.WHATSAPP,
                   occurred_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
                   text=text, record_id=f"rb{i}", citation=f"c{i}",
                   embedding=fake_embedder.embed(text))
            for i in range(n)])
        return p

    def test_total_char_budget_respected(self, clean_store, resolver,
                                         fake_embedder):
        from circle.ai.rag import hybrid_retrieve
        p = self._seed(clean_store, resolver, fake_embedder)
        ev = hybrid_retrieve(clean_store, fake_embedder, "budget",
                             {"person_id": p.id}, k=8, snippet_chars=200,
                             total_chars=600)
        assert ev, "at least one record must always fit the budget"
        assert len(ev) < 10, "budget must actually limit context size"
        assert sum(len(e.text) for e in ev) <= 700

    def test_per_snippet_cap_respected(self, clean_store, resolver, fake_embedder):
        from circle.ai.rag import hybrid_retrieve
        p = self._seed(clean_store, resolver, fake_embedder)
        ev = hybrid_retrieve(clean_store, fake_embedder, "budget",
                             {"person_id": p.id}, k=4, snippet_chars=120,
                             total_chars=10000)
        assert all(len(e.text) <= 122 for e in ev)

    def test_labels_are_sequential_after_budgeting(self, clean_store, resolver,
                                                   fake_embedder):
        from circle.ai.rag import hybrid_retrieve
        p = self._seed(clean_store, resolver, fake_embedder)
        ev = hybrid_retrieve(clean_store, fake_embedder, "budget",
                             {"person_id": p.id}, k=5, snippet_chars=200,
                             total_chars=600)
        assert [e.label for e in ev] == [f"S{i+1}" for i in range(len(ev))]


class TestQuestionUnderstanding:
    def test_prepare_intent(self):
        assert understand("prepare me for meeting Aravinth").intent == "prepare"

    def test_relationship_intent(self):
        assert understand("how is our relationship with Aravinth").intent == "summary"

    def test_time_cutoff(self):
        plan = understand("what did we talk about last week?")
        assert plan.after is not None

    def test_source_hint(self):
        assert understand("what did I say in voice notes?").source_hint == "voice"

    def test_plain_ask(self):
        plan = understand("what did we discuss recently?")
        assert plan.intent == "ask"
        assert plan.after is None


class TestRagAnswerFlow:
    def _pipeline(self, store, embedder, llm):
        from circle.ai.rag import RagPipeline
        return RagPipeline(store, embedder, llm)

    def test_no_evidence_returns_insufficient_without_llm(self, clean_store,
                                                          fake_embedder, fake_llm):
        pipe = self._pipeline(clean_store, fake_embedder, fake_llm)
        res = pipe.ask("what did we discuss?")
        assert res.insufficient is True
        assert res.answer == INSUFFICIENT
        assert fake_llm.calls == []   # never call the LLM without evidence

    def test_cited_answer_with_evidence(self, clean_store, resolver,
                                        fake_embedder, fake_llm):
        from circle.domain.models import Memory
        p, _ = resolver.resolve_sender("Aravinth", SourceType.WHATSAPP)
        text = "Aravinth: SIH meeting tomorrow"
        clean_store.insert_memories([Memory(
            id="mem-rag-1", person_id=p.id, kind="message",
            source=SourceType.WHATSAPP,
            occurred_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
            text=text, record_id="r1",
            citation="WhatsApp — Jan 1, 2024", topics=["SIH"],
            embedding=fake_embedder.embed(text))])
        pipe = self._pipeline(clean_store, fake_embedder, fake_llm)
        res = pipe.ask("what did we discuss with Aravinth?", person_id=p.id)
        assert res.evidence_count >= 1
        if not res.insufficient:
            assert res.sources, "cited answers must expose sources"

    def test_fabricating_model_never_fabricates_sources(self, clean_store,
                                                        resolver, fake_embedder):
        from circle.ai.rag import RagPipeline
        from circle.domain.models import Memory
        p, _ = resolver.resolve_sender("Aravinth", SourceType.WHATSAPP)
        text = "Aravinth: SIH meeting"
        clean_store.insert_memories([Memory(
            id="mem-rag-2", person_id=p.id, kind="message",
            source=SourceType.WHATSAPP,
            occurred_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
            text=text, record_id="r1",
            citation="WhatsApp — Jan 1, 2024",
            embedding=fake_embedder.embed(text))])
        from tests.conftest import FakeLLM
        # model that cites a non-existent source AND no real one
        bad = FakeLLM(answer="Random claim [S7]. Sources: [S7]")
        pipe = RagPipeline(clean_store, fake_embedder, bad)
        res = pipe.ask("what did we discuss?", person_id=p.id)
        # either refused or stripped the fabricated citation
        assert res.insufficient or "[S7]" not in str(res.sources)

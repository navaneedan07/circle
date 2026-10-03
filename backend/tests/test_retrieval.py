"""Hybrid retrieval must actually use BOTH branches.

Regression tests for two bugs that made search effectively keyword-only:
  1. keyword_search sent the whole question (stopwords included) to $text,
     which matched almost nothing.
  2. Plain RRF let a short result list outrank a long one, and the final
     ordering discarded the fused score in favour of source/recency.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from circle.ai.rag import (Evidence, RagPipeline, classify, hybrid_retrieve,
                           understand)
from circle.domain.models import Memory, Person, SourceType
from circle.relationship.metrics import _is_noise_topic, compute_profile
from circle.repository.mongo import _content_terms


def _mem(mid: str, text: str, person_id: str = "p1",
         when: datetime | None = None, embedding=None,
         kind: str = "message", source: SourceType = SourceType.WHATSAPP):
    m = Memory(id=mid, person_id=person_id, kind=kind, source=source,
               occurred_at=when or datetime(2024, 1, 5, tzinfo=timezone.utc),
               text=text, record_id=f"rec-{mid}", citation=f"c-{mid}",
               topics=["x"], embedding=embedding)
    return m


class _StubStore:
    """Minimal store returning canned retrieval results (rank-order tests)."""

    def __init__(self, vector, keyword):
        self._vector = vector
        self._keyword = keyword

    def vector_search(self, vector, filters, k=12):
        return self._vector[:k]

    def keyword_search(self, query, filters, k=12):
        return self._keyword[:k]


@pytest.fixture()
def seeded(clean_store, fake_embedder):
    """One person with memories that are only findable one way or the other."""
    st = clean_store
    pid = "p1"
    # Lexically obvious for the term "internship".
    st.insert_memories([
        _mem("m-lex-1", "the internship application deadline is friday", pid,
             embedding=[1.0, 0.0] + [0.0] * 14),
        _mem("m-lex-2", "please send me the internship form", pid,
             embedding=[1.0, 0.0] + [0.0] * 14),
    ])
    # Semantically related but shares NO words with the question.
    st.insert_memories([
        _mem("m-sem-1", "we should talk about the placement drive soon", pid,
             embedding=[0.0, 1.0] + [0.0] * 14),
        _mem("m-sem-2", "let us catch up about the hiring pipeline", pid,
             embedding=[0.0, 1.0] + [0.0] * 14),
    ])
    # A filler record to give the vector branch a longer list.
    st.insert_memories([
        _mem("m-fill-1", "unrelated chatter about lunch", pid,
             embedding=[0.9, 0.1] + [0.0] * 14),
        _mem("m-fill-2", "another unrelated line of text", pid,
             embedding=[0.8, 0.2] + [0.0] * 14),
    ])
    return st


class TestContentTerms:
    def test_stopwords_removed(self):
        terms = _content_terms("what did we discuss about the machine learning model")
        assert "what" not in terms
        assert "did" not in terms
        assert "we" not in terms
        assert "the" not in terms
        assert "about" not in terms

    def test_content_words_kept_and_stemmed(self):
        terms = _content_terms("what did we discuss about the machine learning models")
        assert "machine" in terms
        # "learning" -> "learn", "models" -> "model"
        assert "learn" in terms
        assert "model" in terms

    def test_empty_for_pure_stopwords(self):
        assert _content_terms("what is it about") == []


class TestKeywordSearch:
    def test_stopword_question_still_matches(self, seeded):
        """A full sentence must not collapse the lexical branch to zero."""
        hits = seeded.keyword_search(
            "what did we discuss about the internship", {"person_id": "p1"}, k=10)
        assert hits, "keyword search returned nothing for a natural question"
        assert any("internship" in m.text for m, _ in hits)

    def test_respects_person_filter(self, seeded):
        seeded.insert_memories([
            _mem("m-other", "internship elsewhere", "p2",
                 embedding=[1.0, 0.0] + [0.0] * 14)])
        hits = seeded.keyword_search("internship", {"person_id": "p1"}, k=10)
        assert all(m.person_id == "p1" for m, _ in hits)


class TestHybridUsesBothBranches:
    def test_keyword_branch_not_empty_for_sentences(self, seeded, fake_embedder):
        ev = hybrid_retrieve(seeded, fake_embedder,
                             "what did we discuss about the internship",
                             {"person_id": "p1"}, k=4,
                             snippet_chars=200, total_chars=0)
        assert ev
        assert any(e.keyword_rank is not None for e in ev), \
            "lexical branch contributed nothing"

    def test_semantic_branch_contributes(self, seeded, fake_embedder):
        """Vector results must survive fusion even when they rank lower."""
        ev = hybrid_retrieve(seeded, fake_embedder,
                             "what did we discuss about the internship",
                             {"person_id": "p1"}, k=4,
                             snippet_chars=200, total_chars=0)
        assert any(e.vector_rank is not None for e in ev), \
            "semantic branch contributed nothing"

    def test_one_short_list_cannot_crowd_out_long_list(self, seeded, fake_embedder):
        """Regression: 1 keyword hit used to outrank 8 vector hits."""
        # Force a tiny lexical result by searching an unlisted-but-present term
        # that only one document contains.
        seeded.insert_memories([
            _mem("m-rare", "quarterly retrospective notes", "p1",
                 embedding=[0.0, 0.0, 1.0] + [0.0] * 13)])
        ev = hybrid_retrieve(seeded, fake_embedder,
                             "what did we discuss about the internship",
                             {"person_id": "p1"}, k=4,
                             snippet_chars=200, total_chars=0)
        assert len(ev) >= 4
        # At least some evidence comes from the longer vector list.
        assert sum(1 for e in ev if e.vector_rank is not None) >= 2

    def test_matched_by_labels(self):
        ev = Evidence(label="S1", memory=_mem("a", "x"), text="x",
                      vector_rank=0, keyword_rank=1)
        assert ev.matched_by == "hybrid"
        ev2 = Evidence(label="S1", memory=_mem("a", "x"), text="x",
                       vector_rank=2, keyword_rank=None)
        assert ev2.matched_by == "semantic"
        ev3 = Evidence(label="S1", memory=_mem("a", "x"), text="x",
                       vector_rank=None, keyword_rank=0)
        assert ev3.matched_by == "keyword"
        ev4 = Evidence(label="S1", memory=_mem("a", "x"), text="x")
        assert ev4.matched_by == "none"


class TestFusionOrdering:
    def test_results_sorted_by_fused_score(self, seeded, fake_embedder):
        """Ordering must reflect relevance, not source preference + recency."""
        ev = hybrid_retrieve(seeded, fake_embedder,
                             "what did we discuss about the internship",
                             {"person_id": "p1"}, k=6,
                             snippet_chars=200, total_chars=0)
        scores = [e.score for e in ev]
        assert scores == sorted(scores, reverse=True)

    def test_relevance_beats_source_and_recency(self, fake_embedder):
        """The top hit must be the best-matching one.

        Regression: the old code re-sorted by source preference then recency,
        which threw the fused ranking away. Here the strongest match is an
        email (not the preferred WhatsApp source) and the oldest of the set, so
        a source/recency sort would demote it. Retrieval results are stubbed so
        the ranking is deterministic regardless of the fake embedder.
        """
        old = datetime(2020, 1, 1, tzinfo=timezone.utc)
        best = _mem("m-best", "the internship offer came through", when=old,
                    source=SourceType.EMAIL)
        chats = [
            _mem(f"m-whats-{i}", f"chatter {i} about lunch",
                 when=datetime(2024, 6, 1 + i, tzinfo=timezone.utc))
            for i in range(3)
        ]
        # Both branches agree the email is the best match; the WhatsApp records
        # are newer and from the preferred source, so a source/recency sort
        # would push the email to the bottom.
        store = _StubStore(
            vector=[(best, 0.99)] + [(m, 0.5 - i * 0.01) for i, m in enumerate(chats)],
            keyword=[(best, 5.0)],
        )
        ev = hybrid_retrieve(store, fake_embedder, "internship", {}, k=4,
                             snippet_chars=200, total_chars=0)
        assert ev, "no evidence retrieved"
        assert ev[0].memory.id == "m-best", (
            "best-matching record was not ranked first; got "
            f"{[e.memory.id for e in ev]}")

    def test_branch_scores_are_length_normalized(self, fake_embedder):
        """The semantic branch must outrank a lone keyword hit.

        Regression: plain RRF scores every rank-0 as 1/(k+1) regardless of list
        length, so a single keyword hit tied the best of eight vector hits and
        could displace the whole semantic neighbourhood. With per-list
        normalization the vector rank-0 scores full weight and the keyword-only
        item (weighted less) cannot tie it.
        """
        vec = _mem("m-v0", "vector best")
        lex = _mem("m-k0", "keyword only hit")
        store = _StubStore(
            vector=[(vec, 0.9)] + [(_mem(f"m-v{i}", f"vector {i}"), 0.8 - i * 0.05)
                                   for i in range(1, 8)],
            keyword=[(lex, 5.0)])
        ev = hybrid_retrieve(store, fake_embedder, "q", {}, k=9,
                             snippet_chars=200, total_chars=0)
        by_id = {e.memory.id: e for e in ev}
        assert "m-v0" in by_id and "m-k0" in by_id
        assert by_id["m-v0"].score > by_id["m-k0"].score, (
            "a single keyword hit matched the best semantic result")
        assert ev[0].memory.id == "m-v0"

    def test_labels_are_sequential(self, seeded, fake_embedder):
        ev = hybrid_retrieve(seeded, fake_embedder, "internship",
                             {"person_id": "p1"}, k=4,
                             snippet_chars=200, total_chars=0)
        assert [e.label for e in ev] == [f"S{i + 1}" for i in range(len(ev))]

    def test_total_budget_respected(self, seeded, fake_embedder):
        ev = hybrid_retrieve(seeded, fake_embedder, "internship placement",
                             {"person_id": "p1"}, k=10,
                             snippet_chars=200, total_chars=120)
        assert ev
        assert sum(len(e.text) for e in ev) <= 120 + len(ev) * 1


class TestTopPeopleIntent:
    """'Who do I talk to the most' is a counting question, not a retrieval one.

    Regression: the old code retrieved a handful of keyword-matching messages
    and let the model name people from them, which produced a confidently wrong
    ranking (the answer was whoever happened to appear in those 4 records).
    """

    @pytest.mark.parametrize("question", [
        "to which people i talk the most",
        "who do i talk to the most",
        "who do I speak with most often",
        "who are my closest contacts",
        "who have i spoken to the most",
        "which people do i message most frequently",
        "how often do i talk to each person",
        "who do i know best",
        # Regression: these phrasings used to miss the router entirely and fall
        # through to the model, which then invented a ranking from 4 records.
        "to whom i talk most",
        "to whom do i talk the most",
        "with whom do i speak most often",
        "whom do i talk to most",
        "who i talk most",
        "to whom i talk most in the last week",
    ])
    def test_counting_questions_classify_as_top_people(self, question):
        assert understand(question).intent == "top_people"

    @pytest.mark.parametrize("question", [
        "what did navaneedan say about the internship",
        "who is navaneedan",
        "what promises did i make to hitesh",
        "summarize my relationship with saravana",
        # "talk ABOUT" is a retrieval question, never a ranking one.
        "who did I talk to about the internship",
        "what do people talk about most",
    ])
    def test_other_questions_are_not_top_people(self, question):
        assert understand(question).intent != "top_people"

    def _seed(self, store):
        """Two people with clearly different interaction volumes."""
        from circle.relationship.metrics import record_event
        now = datetime.now(timezone.utc)
        counts = {"navaneedan": 40, "saravana": 12, "hitesh": 3}
        ids = {}
        for name, n in counts.items():
            p = store.insert_person(Person(display_name=name))
            ids[name] = p.id
            for i in range(n):
                record_event(store, person_id=p.id, kind="message",
                             source="whatsapp",
                             occurred_at=now - timedelta(hours=i),
                             summary=f"{name} message {i}",
                             record_id=f"{name}-{i}")
        return ids

    def test_answer_ranks_by_interaction_count(self, clean_store,
                                               fake_embedder, fake_llm):
        ids = self._seed(clean_store)
        pipe = RagPipeline(clean_store, fake_embedder, fake_llm)
        res = pipe.ask("to which people i talk the most")

        assert res.intent == "top_people"
        assert res.insufficient is False
        # Ranked by real totals: navaneedan (40) > saravana (12) > hitesh (3).
        order = [s["person_id"] for s in res.sources]
        assert order == [ids["navaneedan"], ids["saravana"], ids["hitesh"]]
        assert res.sources[0]["snippet"].startswith("40")
        assert "navaneedan" in res.answer.lower()
        assert "40" in res.answer
        # The ranking is not allowed to come from the model.
        assert fake_llm.calls == []

    def test_llm_is_not_called(self, clean_store, fake_embedder, fake_llm):
        self._seed(clean_store)
        pipe = RagPipeline(clean_store, fake_embedder, fake_llm)
        pipe.ask("who do I talk to the most")
        assert fake_llm.calls == []

    def test_unmatched_phrasing_never_reaches_the_model(
            self, clean_store, fake_embedder, fake_llm):
        """'to whom i talk most' must be answered by counting, not by guessing."""
        ids = self._seed(clean_store)
        pipe = RagPipeline(clean_store, fake_embedder, fake_llm)
        res = pipe.ask("to whom i talk most")
        assert res.intent == "top_people"
        assert fake_llm.calls == []
        assert [s["person_id"] for s in res.sources] == [
            ids["navaneedan"], ids["saravana"], ids["hitesh"]]

    def test_answer_uses_real_newlines(self, clean_store, fake_embedder,
                                       fake_llm):
        """Regression: the answer shipped literal backslash-n sequences, so the
        ranking rendered as one long line of visible '\\n' characters."""
        self._seed(clean_store)
        pipe = RagPipeline(clean_store, fake_embedder, fake_llm)
        res = pipe.ask("who do I talk to the most")
        assert "\\n" not in res.answer
        assert "\n" in res.answer
        lines = [ln for ln in res.answer.splitlines() if ln.strip()]
        # a header, one line per ranked person, then the footnote
        assert len(lines) == len(res.sources) + 2
        assert lines[1].startswith("1. ")
        assert lines[-1].startswith("Counts come from")

    def test_sources_link_to_the_people(self, clean_store, fake_embedder,
                                       fake_llm):
        ids = self._seed(clean_store)
        pipe = RagPipeline(clean_store, fake_embedder, fake_llm)
        res = pipe.ask("who do I talk to the most")
        assert all(s["memory_id"] is None for s in res.sources)
        assert {s["person_id"] for s in res.sources} == set(ids.values())
        assert all(s["person_name"] for s in res.sources)

    def test_time_window_is_honoured(self, clean_store, fake_embedder,
                                     fake_llm):
        """'in the last week' must exclude older interactions."""
        from circle.relationship.metrics import record_event
        now = datetime.now(timezone.utc)
        old = clean_store.insert_person(Person(display_name="oldfriend"))
        recent = clean_store.insert_person(Person(display_name="newfriend"))
        for i in range(25):
            record_event(clean_store, person_id=old.id, kind="message",
                         source="whatsapp",
                         occurred_at=now - timedelta(days=90),
                         summary="old", record_id=f"o{i}")
        for i in range(4):
            record_event(clean_store, person_id=recent.id, kind="message",
                         source="whatsapp", occurred_at=now - timedelta(hours=i),
                         summary="new", record_id=f"n{i}")
        pipe = RagPipeline(clean_store, fake_embedder, fake_llm)
        res = pipe.ask("who do I talk to the most in the last week")
        assert [s["person_id"] for s in res.sources] == [recent.id]
        assert fake_llm.calls == []

    def test_time_window_excludes_future_records(self, clean_store,
                                                 fake_embedder, fake_llm):
        """A future-dated record must not pass an 'in the last week' filter."""
        from circle.relationship.metrics import record_event
        now = datetime.now(timezone.utc)
        future = clean_store.insert_person(Person(display_name="futureguy"))
        past = clean_store.insert_person(Person(display_name="pastguy"))
        for i in range(6):
            record_event(clean_store, person_id=future.id, kind="message",
                         source="whatsapp",
                         occurred_at=now + timedelta(days=30),
                         summary="future", record_id=f"f{i}")
        for i in range(2):
            record_event(clean_store, person_id=past.id, kind="message",
                         source="whatsapp", occurred_at=now - timedelta(hours=i),
                         summary="past", record_id=f"p{i}")
        pipe = RagPipeline(clean_store, fake_embedder, fake_llm)
        res = pipe.ask("who do I talk to the most in the last week")
        assert [s["person_id"] for s in res.sources] == [past.id]

    def test_stream_path_matches(self, clean_store, fake_embedder, fake_llm):
        ids = self._seed(clean_store)
        pipe = RagPipeline(clean_store, fake_embedder, fake_llm)
        events = list(pipe.ask_stream("who do I talk to the most"))
        done = events[-1]
        assert done["type"] == "done"
        assert done["intent"] == "top_people"
        assert done["sources"][0]["person_id"] == ids["navaneedan"]
        assert fake_llm.calls == []

    def test_empty_archive_does_not_claim_a_ranking(self, clean_store,
                                                    fake_embedder, fake_llm):
        pipe = RagPipeline(clean_store, fake_embedder, fake_llm)
        res = pipe.ask("who do I talk to the most")
        assert res.insufficient is True
        assert res.sources == []
        assert fake_llm.calls == []


class TestNoEvidence:
    def test_empty_corpus_returns_nothing(self, clean_store, fake_embedder):
        ev = hybrid_retrieve(clean_store, fake_embedder, "anything",
                             {"person_id": "nobody"}, k=5,
                             snippet_chars=200, total_chars=0)
        assert ev == []


class TestDeterministicArchiveFacts:
    """Countable questions must be answered by aggregation, not generation.

    Regression: "how many people have I talked to" reached the model, which
    answered "109 people responded to a trip" -- a number it had read inside a
    chat, not one it had counted. Asking a language model for a count over
    100k records produces a confident wrong number, so these paths must never
    call it.
    """

    def _seed(self, store):
        from circle.relationship.metrics import record_event
        now = datetime.now(timezone.utc)
        ids = {}
        for name, n in (("navaneedan", 30), ("saravana", 11), ("hitesh", 4)):
            p = store.insert_person(Person(display_name=name))
            ids[name] = p.id
            for i in range(n):
                record_event(store, person_id=p.id, kind="message",
                             source="whatsapp",
                             occurred_at=now - timedelta(hours=i),
                             summary=f"{name} message {i}",
                             record_id=f"{name}-{i}")
        # A second kind so "how many emails" must not count messages.
        p = store.get_person(ids["navaneedan"])
        for i in range(3):
            record_event(store, person_id=p.id, kind="email", source="email",
                         occurred_at=now - timedelta(days=i),
                         summary="mail", record_id=f"mail-{i}")
        return ids

    @pytest.mark.parametrize("question,expected", [
        ("how many messages did I send last week", "activity_count"),
        ("how many emails did I get this month", "activity_count"),
        ("how many meetings do I have", "activity_count"),
        ("how many times did I talk to navaneedan", "activity_count"),
        ("how many people have I talked to", "people_count"),
        ("how many contacts do I have", "people_count"),
        ("when did I last talk to navaneedan", "last_interaction"),
        ("when was the last time I spoke with hitesh", "last_interaction"),
        ("who did I talk to in the last 7 days", "recent_people"),
        ("what are my top topics", "top_topics"),
        ("what do I talk about most", "top_topics"),
    ])
    def test_countable_questions_never_reach_the_model(self, question,
                                                       expected):
        assert classify(question) == expected

    @pytest.mark.parametrize("question", [
        # A count scoped to a subject cannot be answered by a plain aggregate,
        # so it must stay a retrieval question.
        "how many messages did navaneedan say about the trip",
        "who did I talk to about the internship",
        "what did I discuss with Aravinth recently",
        "who is navaneedan",
        "what promises did i make to hitesh",
    ])
    def test_retrieval_questions_are_not_captured(self, question):
        assert classify(question) == "ask"

    def test_message_count_is_the_database_number(self, clean_store,
                                                  fake_embedder, fake_llm):
        self._seed(clean_store)
        pipe = RagPipeline(clean_store, fake_embedder, fake_llm)
        res = pipe.ask("how many messages did I send last week")
        assert res.intent == "activity_count"
        # 30 + 11 + 4 messages; the 3 emails are a different kind.
        assert "45" in res.answer
        assert fake_llm.calls == []

    def test_email_count_excludes_messages(self, clean_store, fake_embedder,
                                           fake_llm):
        self._seed(clean_store)
        pipe = RagPipeline(clean_store, fake_embedder, fake_llm)
        res = pipe.ask("how many emails did I get this month")
        assert res.intent == "activity_count"
        assert "3" in res.answer
        assert fake_llm.calls == []

    def test_people_count_counts_people_not_messages(self, clean_store,
                                                    fake_embedder, fake_llm):
        """45 interactions came from 3 people: the answer must say 3."""
        self._seed(clean_store)
        pipe = RagPipeline(clean_store, fake_embedder, fake_llm)
        res = pipe.ask("how many people have I talked to")
        assert res.intent == "people_count"
        assert "3 distinct people" in res.answer
        assert fake_llm.calls == []

    def test_last_interaction_uses_the_stored_timestamp(self, clean_store,
                                                        fake_embedder,
                                                        fake_llm):
        self._seed(clean_store)
        pipe = RagPipeline(clean_store, fake_embedder, fake_llm)
        res = pipe.ask("when did I last talk to navaneedan")
        assert res.intent == "last_interaction"
        assert "navaneedan" in res.answer.lower()
        assert fake_llm.calls == []

    def test_recent_people_respects_the_window(self, clean_store,
                                               fake_embedder, fake_llm):
        self._seed(clean_store)
        pipe = RagPipeline(clean_store, fake_embedder, fake_llm)
        res = pipe.ask("who did I talk to in the last 7 days")
        assert res.intent == "recent_people"
        assert res.sources
        assert all(s["kind"] == "interaction_count" for s in res.sources)
        assert fake_llm.calls == []

    def test_facts_carry_a_citation(self, clean_store, fake_embedder,
                                    fake_llm):
        """An aggregate answer stays checkable: it needs a source row."""
        self._seed(clean_store)
        pipe = RagPipeline(clean_store, fake_embedder, fake_llm)
        for q in ("how many messages did I send last week",
                  "how many people have I talked to",
                  "when did I last talk to navaneedan"):
            res = pipe.ask(q)
            assert res.sources, q
            assert all(s["source"] == "archive" for s in res.sources), q
            assert all(s["memory_id"] is None for s in res.sources), q

    def test_stream_path_answers_facts_without_generation(self, clean_store,
                                                         fake_embedder,
                                                         fake_llm):
        self._seed(clean_store)
        pipe = RagPipeline(clean_store, fake_embedder, fake_llm)
        done = list(pipe.ask_stream("how many people have I talked to"))[-1]
        assert done["type"] == "done"
        assert done["intent"] == "people_count"
        assert "3 distinct people" in done["answer"]
        assert fake_llm.calls == []

    def test_empty_archive_reports_no_facts(self, clean_store, fake_embedder,
                                            fake_llm):
        pipe = RagPipeline(clean_store, fake_embedder, fake_llm)
        res = pipe.ask("how many people have I talked to")
        assert res.intent == "people_count"
        assert "0 distinct people" in res.answer
        assert fake_llm.calls == []


class TestTopicNoise:
    """Extracted topics must be subjects, not export-format artifacts.

    Regression: over a 120k-message archive the top "topics" were Reacted
    (2,416), Liked (796) and Hmm (694) -- Instagram system events and chat
    interjections written into the export as message text -- followed by
    people's names.
    """

    @pytest.mark.parametrize("phrase", [
        "Reacted", "Liked", "Shared", "Forwarded", "sent an attachment",
        "Everyone", "Guys", "Hmm", "Haha", "Okay", "Yeah", "Bro",
        "JPG", "Good morning", "Thank you",
    ])
    def test_system_events_are_not_topics(self, phrase):
        assert _is_noise_topic(phrase)

    def test_real_subjects_survive(self):
        """Regression: an earlier 3-character prefix rule for code-mixed
        filler ("ser", "ena") silently dropped real subjects. Filtering must
        be exact-match, never prefix-match."""
        for phrase in ("Flipkart", "Machine Learning", "DBMS", "Students",
                       "Applied Data Engineering", "Unit Seminar",
                       "Register Number", "Server", "Series", "Networking",
                       "Placement", "Kubernetes", "React Native", "Selenium"):
            assert not _is_noise_topic(phrase), phrase

    @pytest.mark.parametrize("phrase", [
        "Enna", "Seri", "Poda", "Illa", "Okk", "Tmrw", "Deiii", "Deii",
        "Naa", "Those", "Everyone", "Happy", "Tomorrow", "Append", "Yeah",
        "Dai", "Enaku", "Okayy", "ASAP", "Naan", "Machan", "Sis",
    ])
    def test_code_mixed_filler_is_rejected(self, phrase):
        assert _is_noise_topic(phrase)

    @pytest.mark.parametrize("phrase", [
        "Data Structures", "Server", "Series", "Networking", "Placement",
        "Kubernetes", "React Native", "Selenium", "Password", "Login",
        "Register Number", "Unit Seminar", "WhatsApp", "Follow",
    ])
    def test_no_false_positives_on_subjects(self, phrase):
        """The filler lists must not swallow genuine subjects that happen to
        contain filler-ish substrings."""
        assert not _is_noise_topic(phrase), phrase

    def test_compute_profile_filters_stored_topics(self, clean_store):
        """The API serves compute_profile, which rebuilds topics from memory.
        Memories extracted before the filter existed must not leak through."""
        from circle.domain.models import Memory
        person = clean_store.insert_person(Person(display_name="Shrijesh"))
        now = datetime.now(timezone.utc)
        for i, topic in enumerate(["Reacted", "Naa", "Flipkart", "Those"]):
            clean_store.insert_memories([Memory(
                id=f"m{i}", person_id=person.id, kind="message",
                source=SourceType.WHATSAPP, occurred_at=now,
                text="x", record_id=f"r{i}", topics=[topic])])
        profile = compute_profile(clean_store, person.id)
        assert {t.topic for t in profile.topics} == {"Flipkart"}

    def test_person_names_are_not_topics(self):
        known = {"hitesh", "saravana", "navaneedan"}
        assert _is_noise_topic("Hitesh", known)
        assert not _is_noise_topic("Placement", known)

    def test_extractor_skips_system_events(self):
        from circle.relationship.metrics import extract_topics
        topics = extract_topics("Reacted to your message. Liked. Flipkart "
                                "interview. DBMS notes.")
        assert "Reacted" not in topics
        assert "Liked" not in topics
        assert any("Flipkart" in t for t in topics)

    def test_profile_topics_are_cleaned_on_read(self, clean_store):
        """Profiles written before the filter existed must not still serve
        'Naa' and 'Reacted' as topics. Cleaning happens at read time so no
        re-import is needed."""
        from circle.domain.models import (RelationshipProfile, TopicStat)
        clean_store.upsert_profile(RelationshipProfile(
            person_id="p-noise",
            status="Active", status_reason="", interaction_count=4,
            interactions_14d=1, interactions_60d=1,
            source_breakdown={"whatsapp": 4},
            topics=[TopicStat(topic="Reacted", count=9),
                    TopicStat(topic="Naa", count=5),
                    TopicStat(topic="Flipkart", count=3)],
            active_topics=[TopicStat(topic="Dei", count=2)],
            summary="", summary_sources=[], upcoming_events=[],
        ))
        profile = clean_store.get_profile("p-noise")
        kept = {t.topic for t in profile.topics}
        assert kept == {"Flipkart"}
        assert [t.topic for t in profile.active_topics] == []

    def test_person_own_name_is_not_a_topic(self, clean_store):
        from circle.domain.models import (RelationshipProfile, TopicStat)
        person = clean_store.insert_person(Person(display_name="Shrijesh Kannan"))
        clean_store.upsert_profile(RelationshipProfile(
            person_id=person.id,
            status="Active", status_reason="", interaction_count=4,
            interactions_14d=1, interactions_60d=1,
            source_breakdown={"whatsapp": 4},
            topics=[TopicStat(topic="Shrijesh", count=9),
                    TopicStat(topic="DBMS", count=3)],
            active_topics=[], summary="", summary_sources=[],
            upcoming_events=[],
        ))
        profile = clean_store.get_profile(person.id)
        assert {t.topic for t in profile.topics} == {"DBMS"}
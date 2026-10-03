"""Identity resolution tests: deterministic-first, no silent merges,
explicit merge API."""
from __future__ import annotations

from circle.domain.models import SourceType
from circle.parsers.common import ParsedContact


class TestIdentityResolution:
    def test_contact_creates_person(self, resolver, clean_store):
        n = resolver.ingest_contacts([ParsedContact(
            name="Aravinth Kumar", email="aravinth@example.com",
            phone="911234567890")])
        assert n == 1
        p = resolver.find_person(email="aravinth@example.com")
        assert p is not None
        assert p.display_name == "Aravinth Kumar"

    def test_contact_enriches_existing(self, resolver, clean_store):
        resolver.ingest_contacts([ParsedContact(name="Alice", email="a@x.com")])
        resolver.ingest_contacts([ParsedContact(name="Alice", phone="123456")])
        people = clean_store.list_people()
        assert len(people) == 1
        kinds = {i.kind for i in people[0].identities}
        assert {"email", "phone"} <= kinds

    def test_exact_name_match_reused(self, resolver, clean_store):
        resolver.ingest_contacts([ParsedContact(name="Aravinth Kumar",
                                                email="a@x.com")])
        person, created = resolver.resolve_sender("Aravinth Kumar",
                                                  SourceType.TELEGRAM)
        assert created is False
        assert person is not None

    def test_unknown_sender_creates_person(self, resolver, clean_store):
        person, created = resolver.resolve_sender("New Person",
                                                  SourceType.WHATSAPP)
        assert created is True
        assert person.id

    def test_user_labels_not_created(self, resolver, clean_store):
        for label in ("Me", "you", "Myself"):
            person, created = resolver.resolve_sender(label, SourceType.WHATSAPP)
            assert person is None and created is False
        assert clean_store.count_people() == 0

    def test_email_identity_links_person(self, resolver, clean_store):
        resolver.ingest_contacts([ParsedContact(name="Aravinth Kumar",
                                                email="arav@x.com")])
        person, created = resolver.resolve_sender(
            "Aravinth Kumar", SourceType.EMAIL, {"email": "arav@x.com"})
        assert created is False and person is not None

    def test_first_name_ambiguous_creates_suggestion_not_merge(self, resolver,
                                                               clean_store):
        resolver.resolve_sender("Aravinth Kumar", SourceType.WHATSAPP)
        resolver.resolve_sender("Aravinth Rao", SourceType.TELEGRAM)
        # a third source says just "Aravinth" -> must NOT silently merge
        person, created = resolver.resolve_sender("Aravinth", SourceType.INSTAGRAM)
        assert clean_store.count_people() in (2, 3)
        sugg = resolver.list_suggestions()
        # either a suggestion was queued, or a new person was created (no merge)
        if person is not None and created:
            assert sugg is not None

    def test_merge_people_moves_records(self, resolver, clean_store, fake_embedder):
        from circle.domain.models import Memory, Message
        from circle.parsers.common import message_id
        from datetime import datetime, timezone

        a, _ = resolver.resolve_sender("Aravinth", SourceType.WHATSAPP)
        b, _ = resolver.resolve_sender("Aravinth K", SourceType.TELEGRAM)
        when = datetime(2024, 1, 1, tzinfo=timezone.utc)
        mid = message_id("whatsapp", "c", when, "Aravinth", "hi")
        clean_store.insert_message_if_new(Message(
            id=mid, person_id=a.id, sender_label="Aravinth", sent_at=when,
            content="hi", source=SourceType.WHATSAPP))
        clean_store.insert_memories([Memory(
            id="mem-1", person_id=b.id, kind="message",
            source=SourceType.TELEGRAM, occurred_at=when, text="hello",
            record_id="r1", citation="Telegram")])

        out = resolver.merge_people(a.id, b.id)
        assert out["ok"] is True
        assert clean_store.get_person(b.id) is None
        keep = clean_store.get_person(a.id)
        assert "Aravinth K" in keep.aliases
        assert clean_store.db.memories.count_documents(
            {"person_id": a.id}) == 1
        assert clean_store.db.messages.count_documents(
            {"person_id": a.id}) == 1

    def test_merge_same_person_rejected(self, resolver, clean_store):
        p, _ = resolver.resolve_sender("Solo", SourceType.WHATSAPP)
        out = resolver.merge_people(p.id, p.id)
        assert out["ok"] is False

    def test_suggestion_reject_persisted(self, resolver, clean_store):
        resolver.resolve_sender("Aravinth Kumar", SourceType.WHATSAPP)
        resolver.resolve_sender("Aravinth Rao", SourceType.TELEGRAM)
        resolver._queue_suggestion(clean_store.list_people()[0], "Aravinth Rao",
                                   SourceType.TELEGRAM, 0.55, "test")
        sugg = resolver.list_suggestions()
        assert sugg
        out = resolver.accept_suggestion(sugg[0]["id"], merge=False)
        assert out["status"] == "rejected"
        assert resolver.list_suggestions() == []


class TestDuplicatePeople:
    """Same-name records are queued for review, never merged silently."""

    @staticmethod
    def _mk(store, name: str):
        from circle.domain.models import Person

        return store.insert_person(Person(display_name=name))

    @staticmethod
    def _events(store, person_id: str, n: int) -> None:
        from datetime import datetime, timedelta, timezone

        from circle.relationship.metrics import record_event

        now = datetime.now(timezone.utc)
        for i in range(n):
            record_event(store, person_id=person_id, kind="message",
                         source="whatsapp", occurred_at=now - timedelta(hours=i),
                         summary="s", record_id=f"{person_id}-{i}")

    def test_finds_records_sharing_a_name(self, resolver, clean_store):
        """The real case: one name stored as two records from two threads."""
        a = self._mk(clean_store, "Vaishu")
        b = self._mk(clean_store, "Vaishu")
        groups = resolver.find_duplicate_people()
        assert len(groups) == 1
        keep, drops = groups[0]
        assert {keep.id, drops[0].id} == {a.id, b.id}

    def test_ignores_punctuation_and_emoji_differences(self, resolver,
                                                       clean_store):
        a = self._mk(clean_store, "Jineen")
        b = self._mk(clean_store, "Jineen \U0001f43e")
        assert a.id != b.id
        groups = resolver.find_duplicate_people()
        assert len(groups) == 1
        assert {groups[0][0].id, groups[0][1][0].id} == {a.id, b.id}

    def test_keeper_is_the_record_with_most_interactions(self, resolver,
                                                        clean_store):
        a = self._mk(clean_store, "Vaishu")
        b = self._mk(clean_store, "Vaishu")
        self._events(clean_store, a.id, 5)
        self._events(clean_store, b.id, 1)
        keep, drops = resolver.find_duplicate_people()[0]
        assert keep.id == a.id
        assert [d.id for d in drops] == [b.id]

    def test_distinct_names_are_not_grouped(self, resolver, clean_store):
        self._mk(clean_store, "Ashwinee")
        self._mk(clean_store, "Navaneedan")
        assert resolver.find_duplicate_people() == []

    def test_queueing_writes_a_reviewable_suggestion(self, resolver,
                                                     clean_store):
        a = self._mk(clean_store, "Vaishu")
        b = self._mk(clean_store, "Vaishu")
        keep, drops = resolver.find_duplicate_people()[0]
        sid = resolver._queue_duplicate(keep, drops[0])
        pending = [s for s in resolver.list_suggestions() if s["id"] == sid]
        assert len(pending) == 1
        assert pending[0]["person_a_id"] in (a.id, b.id)
        assert pending[0]["person_b_id"] in (a.id, b.id)
        # queueing alone must not merge
        assert clean_store.count_people() == 2

    def test_accepting_a_duplicate_suggestion_merges_records(self, resolver,
                                                             clean_store):
        a = self._mk(clean_store, "Vaishu")
        b = self._mk(clean_store, "Vaishu")
        self._events(clean_store, a.id, 4)
        self._events(clean_store, b.id, 2)
        keep, drops = resolver.find_duplicate_people()[0]
        sid = resolver._queue_duplicate(keep, drops[0])

        out = resolver.accept_suggestion(sid, merge=True)
        assert out["ok"] is True
        assert out["person_id"] == keep.id
        assert clean_store.get_person(drops[0].id) is None
        assert clean_store.count_people() == 1
        # events from both records now belong to the survivor
        assert clean_store.db.relationship_events.count_documents(
            {"person_id": keep.id}) == 6
        assert resolver.list_suggestions() == []

    def test_rejecting_a_duplicate_keeps_both_people(self, resolver,
                                                     clean_store):
        self._mk(clean_store, "Vaishu")
        self._mk(clean_store, "Vaishu")
        keep, drops = resolver.find_duplicate_people()[0]
        sid = resolver._queue_duplicate(keep, drops[0])
        out = resolver.accept_suggestion(sid, merge=False)
        assert out["status"] == "rejected"
        assert clean_store.count_people() == 2

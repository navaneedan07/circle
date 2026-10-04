"""Storage layer tests: the SQLite engine, the query translator, the factory.

The rest of the suite runs against whichever engine is configured, which
proves the app works but not that both engines agree. These tests pin the
SQLite engine's own behaviour, and the Mongo/SQLite parity checks below are
skipped when MongoDB is not installed.
"""
from __future__ import annotations

import os

import pytest

from circle.domain.models import (
    DataOrigin, IdentityLink, Memory, Message, Note, Person, RelationshipEvent,
    RelationshipProfile, SourceType, TopicStat,
)
from circle.repository.sqlite import SQLiteStore


@pytest.fixture()
def lite(tmp_path):
    from circle.config import Settings
    store = SQLiteStore(Settings(import_root=str(tmp_path / "imports")),
                        path=str(tmp_path / "t.db"))
    yield store
    store.close()


def _person(store, name="Ada Lovelace", **kw) -> Person:
    return store.insert_person(Person(display_name=name, **kw))


class TestPeople:
    def test_roundtrip_and_lookup(self, lite):
        p = _person(lite, identities=[IdentityLink(
            kind="email", value="ada@example.com", source=SourceType.EMAIL)])
        assert lite.get_person(p.id).display_name == "Ada Lovelace"
        assert lite.count_people() == 1

    def test_find_by_identity_uses_array_membership(self, lite):
        p = _person(lite, identities=[IdentityLink(
            kind="email", value="ada@example.com", source=SourceType.EMAIL)])
        assert lite.find_person_by_identity("email", "ada@example.com").id == p.id
        assert lite.find_person_by_identity("email", "other@x.com") is None
        assert lite.find_person_by_identity("phone", "ada@example.com") is None

    def test_duplicate_name_gets_a_distinct_id(self, lite):
        a = _person(lite)
        b = _person(lite)
        assert a.id != b.id and lite.count_people() == 2

    def test_delete_removes_the_profile_too(self, lite):
        p = _person(lite)
        lite.upsert_profile(RelationshipProfile(person_id=p.id))
        lite.delete_person(p.id)
        assert lite.get_profile(p.id) is None

    def test_list_people_is_newest_first_and_paginates(self, lite):
        import time
        ids = [_person(lite, f"Person {i}").id for i in range(5)]
        for pid in ids:                       # distinct updated_at, stable order
            person = lite.get_person(pid)
            person.updated_at = person.updated_at.replace(
                microsecond=ids.index(pid))
            lite.update_person(person)
        page = lite.list_people(limit=2)
        assert len(page) == 2
        assert lite.list_people(limit=2, skip=2)[0].id != page[0].id


class TestMessagesAndDuplicates:
    def test_insert_is_idempotent_by_id(self, lite):
        m = Message(id="m1", source=SourceType.WHATSAPP, content="hi")
        assert lite.insert_message_if_new(m) is True
        assert lite.insert_message_if_new(m) is False
        assert lite.count_messages() == 1

    def test_sorted_newest_first_per_person(self, lite):
        import datetime as dt
        p = _person(lite)
        for i in range(3):
            lite.insert_message_if_new(Message(
                id=f"m{i}", person_id=p.id, source=SourceType.WHATSAPP,
                sent_at=dt.datetime(2024, 1, i + 1, tzinfo=dt.timezone.utc)))
        got = [m.id for m in lite.list_messages_for_person(p.id)]
        assert got == ["m2", "m1", "m0"]

    def test_attachment_lookup_is_an_array_match(self, lite):
        lite.insert_message_if_new(Message(
            id="m1", source=SourceType.WHATSAPP,
            attachments=[{"filename_key": "photo.jpg"}]))
        lite.insert_message_if_new(Message(
            id="m2", source=SourceType.WHATSAPP,
            attachments=[{"filename_key": "other.png"}]))
        hits = lite.find_messages_by_attachment_key("photo.jpg")
        assert [m.id for m in hits] == ["m1"]
        assert lite.find_messages_by_attachment_key("") == []

    def test_media_ids_round_trip_through_set_fields(self, lite):
        lite.insert_message_if_new(Message(id="m1", source=SourceType.WHATSAPP,
                                           media_ids=["old"]))
        lite.update_message_media_ids("m1", ["a", "b"])
        assert lite.get_message("m1")["media_ids"] == ["a", "b"]
        # the rest of the document must survive a partial update
        assert lite.get_message("m1")["source"] == "whatsapp"


class TestMemories:
    def _mem(self, mid, **kw):
        return Memory(id=mid, kind="message", source=SourceType.WHATSAPP, **kw)

    def test_embedding_survives_the_round_trip(self, lite):
        vec = [i / 768.0 for i in range(768)]
        lite.insert_memories([self._mem("m1", text="hello", embedding=vec)])
        got = lite.get_memory("m1")
        assert got.embedding is not None and len(got.embedding) == 768
        assert abs(got.embedding[5] - vec[5]) < 1e-6

    def test_duplicate_ids_are_skipped(self, lite):
        assert lite.insert_memories(
            [self._mem("m1", text="a"), self._mem("m1", text="b")]) == 1
        assert lite.count_memories() == 1

    def test_keyword_search_matches_stemmed_terms(self, lite):
        lite.insert_memories([self._mem(
            "m1", text="we discussed the internship documents at length")])
        hits = lite.keyword_search("what about the internship?", {}, k=5)
        assert [m.id for m, _ in hits] == ["m1"]
        assert lite.keyword_search("unrelated zebra", {}, k=5) == []

    def test_keyword_search_respects_the_person_filter(self, lite):
        a, b = _person(lite, "A"), _person(lite, "B")
        lite.insert_memories([self._mem("m1", person_id=a.id, text="internship")])
        lite.insert_memories([self._mem("m2", person_id=b.id, text="internship")])
        hits = lite.keyword_search("internship", {"person_id": a.id}, k=5)
        assert [m.id for m, _ in hits] == ["m1"]
        assert lite.keyword_search("internship", {"person_id": "nobody"}, k=5) == []

    def test_vector_search_ranks_by_cosine(self, lite):
        import math
        near = [1.0] + [0.0] * 767
        far = [0.0, 1.0] + [0.0] * 766
        lite.insert_memories([
            self._mem("near", text="near", embedding=near),
            self._mem("far", text="far", embedding=far)])
        hits = lite.vector_search(near, {}, k=2)
        assert hits[0][0].id == "near"
        assert hits[0][1] > hits[1][1]
        assert abs(hits[0][1] - 1.0) < 1e-5
        assert math.isfinite(hits[0][1])

    def test_vector_search_honours_a_time_window(self, lite):
        import datetime as dt
        lite.insert_memories([
            self._mem("old", embedding=[1.0] + [0.0] * 767,
                      occurred_at=dt.datetime(2020, 1, 1, tzinfo=dt.timezone.utc)),
            self._mem("new", embedding=[1.0] + [0.0] * 767,
                      occurred_at=dt.datetime(2025, 1, 1, tzinfo=dt.timezone.utc))])
        q = {"occurred_at": {"$gte": dt.datetime(2024, 1, 1,
                                                 tzinfo=dt.timezone.utc).isoformat()}}
        assert [m.id for m, _ in lite.vector_search([1.0] + [0.0] * 767,
                                                    q, k=5)] == ["new"]

    def test_upsert_person_rewrites_columns(self, lite):
        p = _person(lite)
        lite.insert_memories([
            self._mem("m1", text="a", record_id="r1"),
            self._mem("m2", text="b", record_id="r2", person_id="other")])
        assert lite.update_memories_person("r1", p.id) == 1
        # Re-running changes nothing, so nothing is reported as moved.
        assert lite.update_memories_person("r1", p.id) == 0
        assert lite.update_memories_person("r2", p.id) == 1
        assert [m.person_id for m in lite.memories_for_person(p.id)] != []


class TestRelationshipAggregates:
    def _events(self, store):
        import datetime as dt
        now = dt.datetime.now(dt.timezone.utc)
        a, b = _person(store, "A"), _person(store, "B")
        for i in range(3):
            store.insert_relationship_event(RelationshipEvent(
                id=f"e{i}", person_id=a.id, kind="message",
                source=SourceType.WHATSAPP,
                occurred_at=now - dt.timedelta(days=i + 1), summary="x"))
        store.insert_relationship_event(RelationshipEvent(
            id="e9", person_id=b.id, kind="email", source=SourceType.EMAIL,
            occurred_at=now - dt.timedelta(days=1), summary="y"))
        return a, b, now

    def test_interaction_totals_ranked_by_volume(self, lite):
        self._events(lite)
        rows = lite.interaction_totals(limit=5)
        assert [n for _, n, _ in rows] == [3, 1]

    def test_since_excludes_older_events(self, lite):
        import datetime as dt
        self._events(lite)
        # Events sit at 1, 2 and 3 days back. A window that starts halfway
        # through day two keeps only the one-day-old event; starting halfway
        # through day three keeps the first two and drops the third.
        since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1, hours=12)
        assert [n for _, n, _ in lite.interaction_totals(since=since)] == [1, 1]
        older = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=2, hours=12)
        assert [n for _, n, _ in lite.interaction_totals(since=older)] == [2, 1]

    def test_lifetime_totals_include_future_dated_events(self, lite):
        """No `since` means the whole archive, not a window ending now.

        Exports carry tomorrow's timestamps; the lifetime leaderboard must
        still rank those people, or they silently vanish from "who do I talk
        to most".
        """
        import datetime as dt
        now = dt.datetime.now(dt.timezone.utc)
        p = _person(lite)
        for i in range(2):
            lite.insert_relationship_event(RelationshipEvent(
                id=f"old{i}", person_id=p.id, kind="message",
                source=SourceType.WHATSAPP,
                occurred_at=now - dt.timedelta(days=5), summary="x"))
        lite.insert_relationship_event(RelationshipEvent(
            id="future", person_id=p.id, kind="message",
            source=SourceType.WHATSAPP,
            occurred_at=now + dt.timedelta(days=5), summary="x"))
        assert dict((pid, n) for pid, n, _ in lite.interaction_totals())[p.id] == 3
        # activity_totals IS a period question, so it excludes the future.
        assert lite.activity_totals()["total"] == 2

    def test_period_queries_are_bounded_at_both_ends(self, lite):
        """Future-dated records must not count as "this week".

        Exports routinely carry timestamps in the future, so a period question
        has to close its window at now rather than running to infinity.
        """
        import datetime as dt
        now = dt.datetime.now(dt.timezone.utc)
        p = _person(lite)
        lite.insert_relationship_event(RelationshipEvent(
            id="future", person_id=p.id, kind="message",
            source=SourceType.WHATSAPP,
            occurred_at=now + dt.timedelta(days=400), summary="x"))
        assert lite.activity_totals()["total"] == 0
        assert lite.activity_totals(since=now - dt.timedelta(days=7))["total"] == 0
        assert lite.interaction_totals(
            since=now - dt.timedelta(days=7)) == []
        assert lite.distinct_people(since=now - dt.timedelta(days=7)) == 0

    def test_activity_totals_splits_by_kind_and_source(self, lite):
        self._events(lite)
        out = lite.activity_totals()
        assert out["total"] == 4
        assert out["by_kind"] == {"message": 3, "email": 1}
        assert out["by_source"]["email"] == 1

    def test_activity_totals_filters_by_person(self, lite):
        a, _, _ = self._events(lite)
        assert lite.activity_totals(person_id=a.id)["total"] == 3

    def test_distinct_people_ignores_blank_person_ids(self, lite):
        import datetime as dt
        self._events(lite)
        lite.insert_relationship_event(RelationshipEvent(
            id="blank", person_id="", kind="message",
            source=SourceType.WHATSAPP,
            occurred_at=dt.datetime.now(dt.timezone.utc)))
        assert lite.distinct_people() == 2

    def test_last_interaction_per_person(self, lite):
        a, _, _ = self._events(lite)
        assert lite.last_interaction(a.id) is not None
        assert lite.last_interaction("nobody") is None

    def test_event_counts_by_person(self, lite):
        a, b, _ = self._events(lite)
        assert lite.event_counts_by_person() == {a.id: 3, b.id: 1}


class TestProfiles:
    def test_topic_noise_is_filtered_on_read(self, lite):
        p = _person(lite)
        lite.upsert_profile(RelationshipProfile(
            person_id=p.id,
            topics=[TopicStat(topic="engine", count=3),
                    TopicStat(topic="Ada", count=5),
                    TopicStat(topic="Reacted", count=9)]))
        topics = [t.topic for t in lite.get_profile(p.id).topics]
        assert topics == ["engine"]

    def test_profile_is_one_per_person(self, lite):
        p = _person(lite)
        for n in range(3):
            lite.upsert_profile(RelationshipProfile(
                person_id=p.id, interaction_count=n))
        assert lite.get_profile(p.id).interaction_count == 2
        assert len(lite.list_profiles()) == 1


class TestSettingsAndState:
    def test_settings_round_trip_through_json(self, lite):
        lite.set_setting("user_names", ["me", "myself"])
        lite.set_setting("onboarded", True)
        assert lite.get_setting("user_names") == ["me", "myself"]
        assert lite.get_setting("onboarded") is True
        assert lite.get_setting("absent") is None

    def test_processed_file_state(self, lite):
        assert lite.already_processed("sum") is False
        lite.mark_processed("sum", "/tmp/a.json", "job-1")
        assert lite.already_processed("sum") is True
        assert lite.get_processed_by_path("/tmp/a.json")["job_id"] == "job-1"
        assert lite.get_processed_by_path("/other") is None


class TestIdentitySuggestions:
    def test_upsert_merges_rather_than_replaces(self, lite):
        lite.upsert_identity_suggestion("s1", {"label": "Ada", "status": "pending"})
        lite.upsert_identity_suggestion("s1", {"confidence": 0.5})
        doc = lite.get_identity_suggestion("s1")
        assert doc["label"] == "Ada" and doc["confidence"] == 0.5

    def test_listing_and_status_change(self, lite):
        lite.upsert_identity_suggestion("s1", {"status": "pending"})
        lite.upsert_identity_suggestion("s2", {"status": "pending"})
        assert len(lite.list_identity_suggestions("pending")) == 2
        lite.set_identity_suggestion_status("s1", "accepted")
        assert [d["_id"] for d in lite.list_identity_suggestions("pending")] == ["s2"]


class TestMergeOperations:
    def test_reassign_moves_records(self, lite):
        a, b = _person(lite, "A"), _person(lite, "B")
        lite.insert_message_if_new(Message(id="m1", person_id=b.id,
                                           source=SourceType.WHATSAPP))
        assert lite.reassign_person_records(a.id, b.id,
                                            {"messages": "person_id"}) == 1
        assert lite.count_messages_matching({"person_id": b.id}) == 0
        assert lite.count_messages_matching({"person_id": a.id}) == 1

    def test_array_member_replace_and_pull(self, lite):
        from circle.domain.models import CalendarEvent
        # A merge replaces the whole array with the survivor, exactly as
        # Mongo's $set on that field always has.
        lite.insert_calendar_if_new(CalendarEvent(
            id="c1", source=SourceType.CALENDAR, person_ids=["old", "third"]))
        assert lite.replace_array_member("calendar_events", "person_ids",
                                         "old", "new") == 1
        assert lite.get_calendar("c1").person_ids == ["new"]
        # $pull removes one member and leaves the rest alone.
        lite.insert_calendar_if_new(CalendarEvent(
            id="c2", source=SourceType.CALENDAR, person_ids=["x", "third"]))
        assert lite.pull_array_member("calendar_events", "person_ids", "third") == 1
        assert lite.get_calendar("c2").person_ids == ["x"]
        assert lite.pull_array_member("calendar_events", "person_ids", "third") == 0


class TestQueryTranslator:
    """The Mongo match subset Circle actually uses."""

    @pytest.fixture()
    def eng(self, tmp_path):
        from circle.config import Settings
        from circle.repository.sqlite import SqliteEngine
        return SqliteEngine(str(tmp_path / "q.db"))

    def _sql(self, eng, query):
        params: list = []
        return eng.match_where("messages", query, params), params

    def test_equality_and_in(self, eng):
        sql, p = self._sql(eng, {"person_id": "a", "source": {"$in": ["x", "y"]}})
        assert "person_id = ?" in sql and "IN (?,?)" in sql and p == ["a", "x", "y"]

    def test_comparisons(self, eng):
        sql, p = self._sql(eng, {"occurred_at": {"$gte": "2024", "$lte": "2025"}})
        assert ">=" in sql and "<=" in sql and p == ["2024", "2025"]

    def test_or_and_and(self, eng):
        sql, _ = self._sql(eng, {"$or": [{"source": "a"}, {"source": "b"}]})
        assert sql.count("OR") == 1
        sql, _ = self._sql(eng, {"$and": [{"source": "a"}, {"person_id": "b"}]})
        assert "AND" in sql

    def test_array_containment_uses_json_each(self, eng):
        sql, p = self._sql(eng, {"attachments": "photo.jpg"})
        assert "json_each" in sql and p == ["photo.jpg"]

    def test_anchored_regex_becomes_case_insensitive_equality(self, eng):
        sql, p = self._sql(eng, {"sender_label": {"$regex": "^Ada$",
                                                   "$options": "i"}})
        assert "COLLATE NOCASE" in sql and p == ["Ada"]

    def test_unanchored_regex_becomes_like(self, eng):
        sql, p = self._sql(eng, {"content": {"$regex": "promis"}})
        assert "LIKE" in sql and p == ["%promis%"]

    def test_like_wildcards_in_a_regex_are_escaped(self, eng):
        _, p = self._sql(eng, {"content": {"$regex": "100%"}})
        assert p == ["%100\\%%"]

    def test_elem_match_over_object_array(self, eng):
        sql, p = self._sql(eng, {"attachments": {"$elemMatch":
                                               {"filename_key": "a.jpg"}}})
        assert "json_each" in sql and p == ["a.jpg"]

    def test_unsupported_operator_raises_rather_than_returning_nothing(self, eng):
        with pytest.raises(NotImplementedError):
            self._sql(eng, {"sent_at": {"$near": [1, 2]}})

    def test_empty_query_matches_everything(self, eng):
        assert self._sql(eng, {})[0] == "1=1"


class TestFactory:
    def test_sqlite_is_the_default(self, tmp_path, monkeypatch):
        monkeypatch.setenv("STORAGE_BACKEND", "sqlite")
        from circle.config import Settings
        from circle.repository.factory import get_store
        s = Settings(import_root=str(tmp_path / "i"))
        store = get_store(s, sqlite_path=str(tmp_path / "f.db"))
        assert store.engine_name() == "sqlite"
        store.close()

    def test_unknown_backend_is_rejected(self, tmp_path, monkeypatch):
        monkeypatch.setenv("STORAGE_BACKEND", "postgres")
        from circle.config import Settings
        from circle.repository.factory import get_store
        with pytest.raises(ValueError, match="STORAGE_BACKEND"):
            get_store(Settings(import_root=str(tmp_path / "i")))

    def test_archive_lives_outside_the_import_folder(self, tmp_path):
        """The watcher walks the import root; the archive is not an export."""
        from circle.config import Settings
        exports = tmp_path / "exports"
        exports.mkdir()
        s = Settings(import_root=str(exports),
                     work_root=str(tmp_path / "work"))
        archive = s.sqlite_path()
        assert archive.parent == tmp_path / "work" / "circle-archive"
        assert s.root_dir() not in archive.parents
        assert archive.suffix == ".db"

    def test_no_folder_chosen_means_no_root(self, tmp_path):
        from circle.config import Settings
        s = Settings(work_root=str(tmp_path / "work"))
        assert s.root_dir() is None
        assert s.sqlite_path().is_absolute()
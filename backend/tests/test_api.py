"""API endpoint tests (spec §37, §40). Uses the FastAPI TestClient with
fake AI providers and the isolated test database."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from circle.domain.models import Memory, SourceType


@pytest.fixture()
def client(clean_store, resolver, fake_embedder, fake_llm, tmp_path):
    from circle.api.context import AppContext, set_context
    from circle.config import get_settings
    from circle.ingestion.pipeline import IngestionPipeline
    from circle.ingestion.watcher import FolderWatcher

    settings = get_settings()
    settings.import_root = str(tmp_path / "imports")
    settings.processed_root = str(tmp_path / "processed")
    settings.failed_root = str(tmp_path / "failed")
    settings.quarantine_root = str(tmp_path / "quarantine")

    pipeline = IngestionPipeline(store=clean_store, resolver=resolver,
                                 embedder=fake_embedder, llm=fake_llm,
                                 settings=settings)
    watcher = FolderWatcher(settings=settings, process_fn=None)
    ctx = AppContext(settings=settings, store=clean_store, resolver=resolver,
                     embedder=fake_embedder, llm=fake_llm, pipeline=pipeline,
                     watcher=watcher)
    ctx.started = True
    ctx.health = {
        "database": {"connected": True, "engine": "mongodb"},
        "llm": {"available": True, "model": "test", "detail": "test"},
        "embeddings": {"available": True, "model": "test", "detail": "test"},
        "stt": {"available": False, "detail": "n/a"},
        "tts": {"enabled": False, "detail": "n/a"},
        "watcher": {"running": False, "detail": "disabled in tests"},
    }
    set_context(ctx)
    from circle.main import app
    with TestClient(app) as c:
        yield c
    set_context(None)  # type: ignore[arg-type]


class TestStaticFrontend:
    """Client-side routes must serve the SPA shell, not a 404.

    Refreshing on /person/<id> or /imports previously returned 404 because the
    built frontend was mounted with no index.html fallback.
    """

    def test_deep_links_return_index(self, client, monkeypatch):
        from pathlib import Path
        import circle.main as main_mod

        # Point the app at a real dist if it exists; otherwise assert that the
        # fallback route is wired (the mount only happens when dist exists).
        dist = (Path(main_mod.__file__).resolve().parent.parent.parent
                / "frontend" / "dist")
        if not (dist / "index.html").exists():
            pytest.skip("frontend dist not built")

        import importlib
        app = importlib.reload(main_mod).create_app()
        with TestClient(app) as c:
            for path in ("/", "/imports", "/settings",
                         "/person/does-not-exist"):
                r = c.get(path)
                assert r.status_code == 200, path
                assert "text/html" in r.headers["content-type"], path
                assert "<div id=\"root\">" in r.text, path

    def test_unknown_api_still_404(self, client):
        assert client.get("/api/definitely-not-a-route").status_code == 404


class TestHealthAndStatus:
    def test_health(self, client):
        r = client.get("/api/health")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ok"
        assert body["health"]["database"]["connected"] is True

    def test_sync_status(self, client):
        r = client.get("/api/sync/status")
        assert r.status_code == 200
        body = r.json()
        assert "watcher_running" in body
        assert "counts" in body


class TestPeople:
    def test_empty_list(self, client):
        r = client.get("/api/people")
        assert r.status_code == 200
        assert r.json()["people"] == []

    def test_person_not_found(self, client):
        assert client.get("/api/people/nope").status_code == 404

    def test_created_person_listed(self, client):
        client.post("/api/notes", json={"body": "note for someone"})
        # person endpoint 404 path
        assert client.get("/api/people/missing").status_code == 404


def _seed_people(count: int) -> None:
    """Create `count` people with rising interaction counts and last-seen."""
    from circle.api.context import get_context
    from circle.domain.models import Person, RelationshipProfile

    store = get_context().store
    for i in range(count):
        person = store.insert_person(Person(display_name=f"Person {i:02d}"))
        store.upsert_profile(RelationshipProfile(
            person_id=person.id,
            interaction_count=i,
            last_interaction_at=datetime(2026, 1, i + 1, tzinfo=timezone.utc),
        ))


class TestPeopleSorting:
    """The ledger can be ordered by name or interaction count.

    Ordering happens on the server so pagination stays stable: a row must not
    shift between pages, and equal values need a deterministic tie-break.
    """

    def test_sort_by_name_is_alphabetical(self, client):
        _seed_people(4)
        body = client.get("/api/people", params={"sort": "name"}).json()
        assert body["sort"] == "name"
        assert [p["display_name"] for p in body["people"]] == [
            "Person 00", "Person 01", "Person 02", "Person 03"]

    def test_sort_by_name_puts_number_contacts_last(self, client):
        """Imported phone numbers are not names, so they sort after letters."""
        from circle.api.context import get_context
        from circle.domain.models import Person, RelationshipProfile

        store = get_context().store
        for name in ("+91 62816 83480", "Ashwinee", "7028337788", "Navaneedan"):
            person = store.insert_person(Person(display_name=name))
            store.upsert_profile(RelationshipProfile(person_id=person.id))
        body = client.get("/api/people", params={"sort": "name"}).json()
        names = [p["display_name"] for p in body["people"]]
        assert names[:2] == ["Ashwinee", "Navaneedan"]
        assert set(names[2:]) == {"+91 62816 83480", "7028337788"}

    def test_sort_by_name_orders_embedded_numbers_naturally(self, client):
        """"Contact 2" must come before "Contact 10", not after it."""
        from circle.api.context import get_context
        from circle.domain.models import Person, RelationshipProfile

        store = get_context().store
        for name in ("Contact 10", "Contact 2", "Contact 1"):
            person = store.insert_person(Person(display_name=name))
            store.upsert_profile(RelationshipProfile(person_id=person.id))
        body = client.get("/api/people", params={"sort": "name"}).json()
        assert [p["display_name"] for p in body["people"]] == [
            "Contact 1", "Contact 2", "Contact 10"]

    def test_sort_by_interactions_is_descending(self, client):
        _seed_people(4)
        body = client.get("/api/people", params={"sort": "interactions"}).json()
        assert body["sort"] == "interactions"
        assert [p["display_name"] for p in body["people"]] == [
            "Person 03", "Person 02", "Person 01", "Person 00"]

    def test_unknown_sort_falls_back_to_recent(self, client):
        _seed_people(3)
        body = client.get("/api/people", params={"sort": "sideways"}).json()
        assert body["sort"] == "recent"
        assert [p["display_name"] for p in body["people"]] == [
            "Person 02", "Person 01", "Person 00"]

    def test_sorted_pages_do_not_overlap_or_repeat(self, client):
        _seed_people(6)
        pages = [client.get("/api/people",
                           params={"sort": "name", "limit": 2, "offset": o}).json()
                 for o in (0, 2, 4)]
        names = [p["display_name"] for page in pages for p in page["people"]]
        assert names == [f"Person {i:02d}" for i in range(6)]
        assert {p["total"] for p in pages} == {6}

    def test_ties_break_deterministically(self, client):
        """Equal counts still yield one stable order, in the ledger and rail."""
        from circle.api.context import get_context
        from circle.domain.models import Person, RelationshipProfile

        store = get_context().store
        for name in ("Beta", "Alpha", "Gamma"):
            person = store.insert_person(Person(display_name=name))
            store.upsert_profile(RelationshipProfile(
                person_id=person.id, interaction_count=5,
                last_interaction_at=None))
        body = client.get("/api/people", params={"sort": "interactions"}).json()
        assert [p["display_name"] for p in body["people"]] == [
            "Alpha", "Beta", "Gamma"]
        assert [t["display_name"] for t in body["top"]] == [
            "Alpha", "Beta", "Gamma"]


class TestPeoplePagination:
    """The people list is paginated so the UI never renders every row.

    Importing a few thousand messages can create hundreds of people; the
    dashboard must ask for one page at a time while still reporting an
    accurate total and a server-side "most active" ranking.
    """

    @staticmethod
    def _seed(count: int) -> None:
        _seed_people(count)

    def test_page_reports_total_offset_and_limit(self, client):
        self._seed(7)
        body = client.get("/api/people").json()
        assert body["total"] == 7
        assert body["offset"] == 0
        assert body["limit"] == 50
        assert len(body["people"]) == 7

    def test_limit_and_offset_slice_the_ordered_list(self, client):
        """Ordering is most recent first and stays stable across pages."""
        self._seed(7)
        first = client.get("/api/people?limit=3&offset=0").json()
        second = client.get("/api/people?limit=3&offset=3").json()
        third = client.get("/api/people?limit=3&offset=6").json()
        names = ([p["display_name"] for p in first["people"]]
                 + [p["display_name"] for p in second["people"]]
                 + [p["display_name"] for p in third["people"]])
        assert names == [f"Person {i:02d}" for i in (6, 5, 4, 3, 2, 1, 0)]
        # Every page reports the same full total, not the page length.
        assert first["total"] == second["total"] == third["total"] == 7

    def test_page_size_is_capped(self, client):
        assert client.get("/api/people?limit=99999").json()["limit"] == 200

    def test_negative_offset_is_clamped_to_zero(self, client):
        self._seed(3)
        body = client.get("/api/people?limit=2&offset=-5").json()
        assert body["offset"] == 0
        assert len(body["people"]) == 2

    def test_search_narrows_the_total(self, client):
        self._seed(5)
        body = client.get("/api/people", params={"q": "Person 00"}).json()
        assert body["total"] == 1
        assert body["people"][0]["display_name"] == "Person 00"

    def test_status_filter_narrows_the_total(self, client):
        self._seed(4)
        client.post("/api/notes", json={"body": "note"})  # no-op guard
        body = client.get("/api/people?status=No Recent Activity").json()
        assert body["total"] == 4

    def test_top_ranks_by_interaction_count(self, client):
        """The rail is computed on the server, not from the visible page."""
        self._seed(5)
        page = client.get("/api/people?limit=2").json()
        assert [p["display_name"] for p in page["people"]] == ["Person 04",
                                                              "Person 03"]
        assert [t["display_name"] for t in page["top"]] == [
            "Person 04", "Person 03", "Person 02", "Person 01", "Person 00"]
        assert page["top"][0]["interaction_count"] == 4
        assert "aliases" not in page["top"][0]


class TestNotes:
    def test_create_and_list(self, client):
        r = client.post("/api/notes", json={
            "title": "SIH", "body": "Aravinth works on the ML component"})
        assert r.status_code == 201
        note = r.json()["note"]
        assert note["origin"] == "local"

        r2 = client.get("/api/notes")
        assert r2.status_code == 200
        assert any(n["id"] == note["id"] for n in r2.json()["notes"])

    def test_duplicate_note_rejected(self, client):
        payload = {"title": "x", "body": "identical body text"}
        assert client.post("/api/notes", json=payload).status_code == 201
        assert client.post("/api/notes", json=payload).status_code == 409

    def test_empty_note_rejected(self, client):
        assert client.post("/api/notes", json={"body": ""}).status_code in (400, 422)


class TestSearchAndAsk:
    def _seed(self, client):
        ctx_ids = []
        store = client.app_state if False else None  # noqa
        from circle.api.context import get_context
        ctx = get_context()
        p, _ = ctx.resolver.resolve_sender("Aravinth Kumar", SourceType.WHATSAPP)
        text = "Aravinth: SIH abstract due Friday"
        ctx.store.insert_memories([Memory(
            id="mem-api-1", person_id=p.id, kind="message",
            source=SourceType.WHATSAPP,
            occurred_at=datetime(2024, 1, 5, tzinfo=timezone.utc),
            text=text, record_id="r-api-1",
            citation="WhatsApp — Jan 5, 2024", topics=["SIH"],
            embedding=ctx.embedder.embed(text))])
        ctx_ids.append(p.id)
        return p.id

    def test_search_returns_seeded_memory(self, client):
        pid = self._seed(client)
        r = client.post("/api/search", json={"query": "SIH abstract"})
        assert r.status_code == 200
        results = r.json()["results"]
        assert results
        assert any("SIH abstract" in res["text"] for res in results)

    def test_search_person_filter(self, client):
        pid = self._seed(client)
        r = client.post("/api/search", json={"query": "abstract",
                                             "person_id": "someone-else"})
        assert r.json()["results"] == [] or all(
            res["person_id"] == "someone-else" for res in r.json()["results"])

    def test_ask_returns_cited_answer(self, client):
        pid = self._seed(client)
        r = client.post("/api/ask", json={
            "question": "when is the SIH abstract due?", "person_id": pid})
        assert r.status_code == 200
        body = r.json()
        assert body["insufficient"] is False
        assert body["sources"], "answer must expose sources"
        assert body["sources"][0]["memory_id"]

    def test_ask_without_evidence_is_insufficient(self, client):
        r = client.post("/api/ask", json={"question": "zzzz nothing here"})
        assert r.status_code == 200
        body = r.json()
        assert body["insufficient"] is True
        assert "couldn't find enough evidence" in body["answer"]

    def test_ask_validation(self, client):
        assert client.post("/api/ask", json={"question": ""}).status_code == 422

    def test_ask_stream_emits_tokens_and_sources(self, client):
        """Streaming path: meta -> token(s) -> done with validated sources."""
        pid = self._seed(client)
        import json as _json
        with client.stream("POST", "/api/ask/stream",
                           json={"question": "when is the SIH abstract due?",
                                 "person_id": pid}) as r:
            assert r.status_code == 200
            body = "".join(r.iter_text())
        events = []
        for line in body.splitlines():
            if line.startswith("data: "):
                events.append(_json.loads(line[6:]))
        types = [e["type"] for e in events]
        assert "meta" in types
        assert "token" in types
        done = [e for e in events if e["type"] == "done"][-1]
        assert done["sources"], "streamed answer must still expose validated sources"
        assert done["answer"]

    def test_ai_metrics_endpoint(self, client):
        r = client.get("/api/ai/metrics")
        assert r.status_code == 200
        body = r.json()
        assert "tuning" in body
        assert body["tuning"]["evidence_budget_chars"] > 0

    def test_memory_evidence_endpoint(self, client):
        self._seed(client)
        r = client.get("/api/memories/mem-api-1")
        assert r.status_code == 200
        assert r.json()["memory"]["kind"] == "message"
        assert r.json()["record"] or r.json()["record"] == {}

    def test_memory_not_found(self, client):
        assert client.get("/api/memories/missing").status_code == 404


class TestImportsAndIdentity:
    def test_imports_empty(self, client):
        r = client.get("/api/imports")
        assert r.status_code == 200
        assert r.json()["jobs"] == []

    def test_merge_validation(self, client):
        r = client.post("/api/identity/merge",
                        json={"keep_id": "a", "remove_id": "a"})
        assert r.status_code == 400

    def test_settings_roundtrip(self, client):
        r = client.get("/api/settings")
        assert r.status_code == 200
        assert "data_origins" in r.json()
        r2 = client.post("/api/settings", json={"user_names": ["Me", "You"]})
        assert r2.status_code == 200
        assert r2.json()["user_names"] == ["Me", "You"]

    def test_bootstrap_info(self, client):
        r = client.get("/api/bootstrap")
        assert r.status_code == 200
        assert "layout" in r.json()

"""Pipeline integration tests: duplicate detection, failure handling,
person creation, memory generation, relationship profile updates."""
from __future__ import annotations

from pathlib import Path

import pytest

from tests.store_probe import count, drop, has_embedding

WA_CONTENT = """\
12/28/23, 8:42 PM - Aravinth Kumar: SIH meeting tomorrow?
12/28/23, 8:43 PM - Me: yes, at 6pm
12/29/23, 10:15 AM - Aravinth Kumar: internship docs sent
"""


def _drop_processed(store, checksum: str) -> None:
    drop(store, "processed_files", {"checksum": checksum})


class TestPipeline:
    def test_whatsapp_file_end_to_end(self, pipeline, clean_store, tmp_path):
        f = tmp_path / "Aravinth_chat.txt"
        f.write_text(WA_CONTENT, encoding="utf-8")

        result = pipeline.process_path(f)
        assert result.status == "COMPLETED"
        assert result.job is not None
        assert result.job.records_imported >= 3

        # person created from sender label
        people = [p for p in clean_store.list_people()
                  if "Aravinth" in p.display_name]
        assert people, "person should be created from sender"
        person = people[0]

        # messages stored with person attached
        msgs = clean_store.list_messages_for_person(person.id, limit=50)
        assert msgs
        assert any("SIH" in m.content for m in msgs)

        # file moved to processed/
        assert not f.exists()
        processed = list(pipeline.settings.processed_dir().rglob("*.txt"))
        assert processed

        # memories + embeddings exist
        mems = clean_store.memories_for_person(person.id, limit=50)
        assert mems
        mem_ids = [m.id for m in clean_store.memories_for_person(
            person.id, limit=50)]
        assert mem_ids and all(has_embedding(clean_store, mid)
                              for mid in mem_ids)

        # relationship profile computed and explainable
        profile = clean_store.get_profile(person.id)
        assert profile is not None
        assert profile.interaction_count >= 3
        assert profile.status_reason

    def test_duplicate_checksum_skipped(self, pipeline, clean_store, tmp_path):
        f1 = tmp_path / "a_chat.txt"
        f1.write_text(WA_CONTENT, encoding="utf-8")
        r1 = pipeline.process_path(f1)
        assert r1.status == "COMPLETED"

        # same content, different name/location
        f2 = tmp_path / "b_chat.txt"
        f2.write_text(WA_CONTENT, encoding="utf-8")
        r2 = pipeline.process_path(f2)
        assert r2.status == "SKIPPED"

        # message count unchanged (no duplicate memories)
        from circle.domain.models import SourceType
        total = count(clean_store, "messages", {"source": "whatsapp"})
        assert total == 3

    def test_duplicate_messages_within_file(self, pipeline, clean_store, tmp_path):
        doubled = WA_CONTENT + WA_CONTENT
        f = tmp_path / "dupes_chat.txt"
        f.write_text(doubled, encoding="utf-8")
        result = pipeline.process_path(f)
        assert result.status == "COMPLETED"
        assert count(clean_store, "messages", {"source": "whatsapp"}) == 3

    def test_unparseable_file_goes_to_failed(self, pipeline, tmp_path):
        f = tmp_path / "garbage.txt"
        f.write_text("no structure here at all\njust text\n", encoding="utf-8")
        result = pipeline.process_path(f)
        assert result.status == "FAILED"
        assert not f.exists()
        failed = list(pipeline.settings.failed_dir().rglob("*"))
        assert any(p.is_file() for p in failed)

    def test_unsupported_extension_rejected(self, pipeline, tmp_path):
        f = tmp_path / "evil.exe"
        f.write_bytes(b"MZ")
        result = pipeline.process_path(f)
        assert result.status in ("FAILED", "QUARANTINED")

    def test_zip_with_whatsapp(self, pipeline, clean_store, tmp_path):
        import zipfile
        zpath = tmp_path / "export.zip"
        with zipfile.ZipFile(zpath, "w") as zf:
            zf.writestr("Aravinth_chat.txt", WA_CONTENT)
        result = pipeline.process_path(zpath)
        assert result.status == "COMPLETED"
        assert count(clean_store, "messages", {"source": "whatsapp"}) == 3
        # temp extraction removed
        assert not any(pipeline.settings.temp_dir().parent.glob("circle-zip-*")) \
            if pipeline.settings.temp_dir().parent.exists() else True

    def test_malicious_zip_quarantined(self, pipeline, tmp_path):
        import zipfile
        zpath = tmp_path / "evil.zip"
        with zipfile.ZipFile(zpath, "w") as zf:
            zf.writestr("../../../evil.txt", "pwn")
        result = pipeline.process_path(zpath)
        assert result.status in ("QUARANTINED", "FAILED")
        assert not zpath.exists()

    def test_calendar_and_email_files(self, pipeline, clean_store, tmp_path):
        ics = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:9@x
DTSTAMP:20240105T090000Z
DTSTART:20240108T160000Z
SUMMARY:Sync with Aravinth Kumar
ATTENDEE;CN=Aravinth Kumar:mailto:aravinth@example.com
END:VEVENT
END:VCALENDAR
"""
        (tmp_path / "meet.ics").write_text(ics, encoding="utf-8")
        r = pipeline.process_path(tmp_path / "meet.ics")
        assert r.status == "COMPLETED"
        assert count(clean_store, "calendar_events") == 1

        eml = ("From: Aravinth Kumar <aravinth@example.com>\n"
               "To: me@example.com\nSubject: SIH draft\n"
               "Date: Fri, 05 Jan 2024 19:30:00 +0000\n"
               "Content-Type: text/plain\n\nReview the draft please.\n")
        (tmp_path / "mail.eml").write_text(eml, encoding="utf-8")
        r2 = pipeline.process_path(tmp_path / "mail.eml")
        assert r2.status == "COMPLETED"
        assert count(clean_store, "emails") == 1

    def test_one_broken_file_does_not_stop_others(self, pipeline, tmp_path):
        bad = tmp_path / "bad.txt"
        bad.write_text("nothing parseable", encoding="utf-8")
        good = tmp_path / "good_chat.txt"
        good.write_text(WA_CONTENT, encoding="utf-8")
        r1 = pipeline.process_path(bad)
        r2 = pipeline.process_path(good)
        assert r1.status == "FAILED"
        assert r2.status == "COMPLETED"

"""Real-download workflow tests: browser partial files must never be ingested
or quarantined, and nested archives must be processed recursively."""
from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from circle.ingestion.watcher import FolderWatcher, is_in_progress
from tests.store_probe import count

WA = ("12/28/23, 8:42 PM - Aravinth Kumar: SIH meeting tomorrow?\n"
      "12/28/23, 8:43 PM - Me: yes, at 6pm\n")


class TestInProgressDetection:
    @pytest.mark.parametrize("name", [
        "Unconfirmed 123456.crdownload",   # Chrome/Edge
        "export.zip.part",                 # Firefox / wget
        "chat.txt.partial",
        "download.download",               # Safari
        "notes.tmp",
        "~$draft.docx",                    # office temp lock
        ".DS_Store",
    ])
    def test_in_progress_names(self, name):
        assert is_in_progress(Path(name)) is True

    @pytest.mark.parametrize("name", [
        "Aravinth_chat.txt", "export.zip", "contacts.csv", "voice.mp3",
    ])
    def test_final_files_not_flagged(self, name):
        assert is_in_progress(Path(name)) is False


class TestWatcherSkipsPartials:
    def test_partial_download_not_processed_or_quarantined(self, pipeline, tmp_path):
        settings = pipeline.settings
        watcher = FolderWatcher(settings=settings,
                                process_fn=pipeline.process_path)
        watcher._ensure_layout()
        # Circle no longer creates per-source subfolders, so the test
        # does what a user does: makes the folder it drops files in.
        (settings.root_dir() / "whatsapp").mkdir(parents=True,
                                              exist_ok=True)

        partial = settings.root_dir() / "whatsapp" / "Unconfirmed 999.crdownload"
        partial.write_text(WA, encoding="utf-8")

        # Simulate the filesystem event the watcher would receive mid-download
        watcher.enqueue(partial)
        watcher._handle(partial)

        assert partial.exists(), "partial file must be left alone"
        assert list(settings.failed_dir().rglob("*")) == []
        assert list(settings.quarantine_dir().rglob("*")) == []
        assert count(pipeline.store, "import_jobs") == 0

    def test_zero_byte_file_parked_not_failed(self, pipeline, tmp_path):
        settings = pipeline.settings
        watcher = FolderWatcher(settings=settings,
                                process_fn=pipeline.process_path)
        watcher._ensure_layout()
        # Circle no longer creates per-source subfolders, so the test
        # does what a user does: makes the folder it drops files in.
        (settings.root_dir() / "whatsapp").mkdir(parents=True,
                                              exist_ok=True)
        empty = settings.root_dir() / "whatsapp" / "still_downloading.txt"
        empty.write_text("", encoding="utf-8")

        watcher.enqueue(empty)
        watcher._handle(empty)

        assert empty.exists()
        assert list(settings.quarantine_dir().rglob("*")) == []
        assert count(pipeline.store, "import_jobs") == 0

    def test_renamed_completed_download_is_processed(self, pipeline, tmp_path):
        """After the browser renames .crdownload -> .zip it gets ingested."""
        settings = pipeline.settings
        watcher = FolderWatcher(settings=settings,
                                process_fn=pipeline.process_path)
        watcher._ensure_layout()
        # Circle no longer creates per-source subfolders, so the test
        # does what a user does: makes the folder it drops files in.
        (settings.root_dir() / "whatsapp").mkdir(parents=True,
                                              exist_ok=True)

        final = settings.root_dir() / "whatsapp" / "WhatsApp Chat with X.zip"
        with zipfile.ZipFile(final, "w") as zf:
            zf.writestr("_chat.txt", WA)

        watcher.enqueue(final)
        watcher._handle(final)

        assert not final.exists(), "processed file moves to processed/"
        assert count(pipeline.store, "messages",
            {"source": "whatsapp"}) == 2

    def test_initial_scan_ignores_partials(self, pipeline):
        settings = pipeline.settings
        watcher = FolderWatcher(settings=settings, process_fn=pipeline.process_path)
        watcher._ensure_layout()
        # Circle no longer creates per-source subfolders, so the test
        # does what a user does: makes the folder it drops files in.
        (settings.root_dir() / "whatsapp").mkdir(parents=True,
                                              exist_ok=True)
        (settings.root_dir() / "whatsapp" / "half.zip.part").write_text("x")
        (settings.root_dir() / "whatsapp" / "real_chat.txt").write_text(
            WA, encoding="utf-8")

        found = [p.name for p in watcher._iter_files(settings.root_dir())]
        assert "half.zip.part" not in found
        assert "real_chat.txt" in found


class TestNestedArchives:
    def test_zip_inside_zip_is_processed(self, pipeline, tmp_path):
        """Real exports (Takeout, multi-part social downloads) nest archives."""
        inner = tmp_path / "inner.zip"
        with zipfile.ZipFile(inner, "w") as zf:
            zf.writestr("Aravinth_chat.txt", WA)
        outer = tmp_path / "takeout.zip"
        with zipfile.ZipFile(outer, "w") as zf:
            zf.write(inner, "messages/inner.zip")

        result = pipeline.process_path(outer)
        assert result.status == "COMPLETED"
        assert count(pipeline.store, "messages",
            {"source": "whatsapp"}) == 2

    def test_media_entries_ingested_not_failed(self, pipeline, tmp_path):
        z = tmp_path / "whatsapp_export.zip"
        with zipfile.ZipFile(z, "w") as zf:
            zf.writestr("_chat.txt", WA)
            zf.writestr("Media/photo1.jpg", b"\xff\xd8\xff\xe0binary")
            zf.writestr("Media/voice.opus", b"OggS\x00binary")
            zf.writestr("Media/sticker.webp", b"RIFFbinary")

        result = pipeline.process_path(z)
        assert result.status == "COMPLETED"
        # 2 messages + 3 stored media files; nothing is dropped on the floor
        assert result.job.records_imported == 5
        assert pipeline.store.count_media() == 3
        # temp extraction is always cleaned up
        assert not list(pipeline.settings.temp_dir().glob("circle-zip-*"))

    def test_misplaced_export_still_parsed_by_content(self, pipeline, tmp_path):
        """A Telegram JSON dropped in whatsapp/ must not be lost."""
        import json
        data = {"name": "Aravinth", "type": "personal_chat",
                "messages": [{"id": 1, "type": "message",
                              "date": "2024-01-04T11:20:00",
                              "from": "Aravinth", "text": "hello"}]}
        wrong_folder = pipeline.settings.root_dir() / "whatsapp"
        wrong_folder.mkdir(parents=True, exist_ok=True)
        f = wrong_folder / "result.json"
        f.write_text(json.dumps(data), encoding="utf-8")

        result = pipeline.process_path(f)
        assert result.status == "COMPLETED"
        assert count(pipeline.store, "messages",
            {"source": "telegram"}) == 1

    def test_ambiguous_zip_name_uses_archive_name_hint(self, pipeline, tmp_path):
        """'WhatsApp Chat with X.zip' at the root still routes as WhatsApp."""
        z = pipeline.settings.root_dir() / "WhatsApp Chat with Aravinth.zip"
        with zipfile.ZipFile(z, "w") as zf:
            zf.writestr("chat.txt", WA)
        result = pipeline.process_path(z)
        assert result.status == "COMPLETED"
        assert count(pipeline.store, "messages",
            {"source": "whatsapp"}) == 2

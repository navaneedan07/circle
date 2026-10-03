"""Watching several folders, including cloud-synced ones (spec §7, §43).

Two properties matter most here:

  1. Files in a folder the user owns are never moved. Moving a file inside a
     Google Drive folder would delete it from their Drive and re-upload it,
     which is data loss from the user's point of view.
  2. A cloud placeholder reports its full size before any bytes are local, so
     it must be materialized before it is measured or hashed.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from circle.config import get_settings
from circle.ingestion.watcher import FolderWatcher
from circle.integration import cloudfolder

WA_CONTENT = """\
12/28/23, 8:42 PM - Aravinth Kumar: SIH meeting tomorrow?
12/28/23, 8:43 PM - Me: yes, at 6pm
12/29/23, 10:15 AM - Aravinth Kumar: internship docs sent
"""


class _Counter:
    def __init__(self):
        self.paths: list[Path] = []

    def __call__(self, path: Path):
        self.paths.append(Path(path))
        return type("R", (), {"status": "COMPLETED"})()


class TestMultipleRoots:
    def test_roots_are_normalized_and_deduped(self, tmp_path):
        a, b = tmp_path / "a", tmp_path / "b"
        a.mkdir()
        b.mkdir()
        w = FolderWatcher(roots=[a, b, a, str(a) + "\\"])
        assert [str(p) for p in w.roots] == [str(a.resolve()), str(b.resolve())]

    def test_root_property_is_the_first_folder(self, tmp_path):
        a, b = tmp_path / "a", tmp_path / "b"
        a.mkdir()
        b.mkdir()
        w = FolderWatcher(roots=[b, a])
        assert w.root == b.resolve()

    def test_rescan_queues_every_folder(self, tmp_path):
        a, b = tmp_path / "a", tmp_path / "b"
        for root, name in ((a, "one_chat.txt"), (b, "two_chat.txt")):
            root.mkdir(parents=True)
            (root / name).write_text(WA_CONTENT, encoding="utf-8")
        w = FolderWatcher(roots=[a, b])
        queued = []
        w.enqueue = lambda p: queued.append(Path(p))  # type: ignore[method-assign]
        assert w.rescan() == 2
        assert len(queued) == 2

    def test_set_roots_replaces_the_list(self, tmp_path):
        a, b, c = tmp_path / "a", tmp_path / "b", tmp_path / "c"
        for p in (a, b, c):
            p.mkdir()
        w = FolderWatcher(roots=[a, b])
        w.set_roots([a, c], rescan=False)
        assert [str(p) for p in w.roots] == [str(a.resolve()), str(c.resolve())]

    def test_owns_reports_membership(self, tmp_path):
        a, b = tmp_path / "a", tmp_path / "b"
        a.mkdir()
        b.mkdir()
        w = FolderWatcher(roots=[a])
        assert w.owns(a / "x.txt")
        assert not w.owns(b / "x.txt")


class TestFilesInUserFoldersAreNeverMoved:
    """Regression: the pipeline used to move every processed file to
    processed/. With a Google Drive folder that deletes the user's backup."""

    def test_drive_style_folder_file_is_left_in_place(self, pipeline, tmp_path):
        pipeline.settings.import_root = str(tmp_path / "managed")
        drive = tmp_path / "My Drive" / "Backups"
        drive.mkdir(parents=True)
        f = drive / "Aravinth_chat.txt"
        f.write_text(WA_CONTENT, encoding="utf-8")

        result = pipeline.process_path(f)
        assert result.status == "COMPLETED"
        assert f.exists(), "the user's backup file must not be moved"
        # nothing was written into the Drive folder either
        assert [p.name for p in drive.iterdir()] == ["Aravinth_chat.txt"]
        assert not list(pipeline.settings.processed_dir().rglob("*.txt"))

    def test_failed_file_stays_in_a_user_folder(self, pipeline, tmp_path):
        pipeline.settings.import_root = str(tmp_path / "managed")
        usb = tmp_path / "Backups"
        usb.mkdir(parents=True)
        f = usb / "garbage.txt"
        f.write_text("not parseable at all\n", encoding="utf-8")

        result = pipeline.process_path(f)
        assert result.status == "FAILED"
        assert f.exists(), "a file in your own folder must not be moved away"
        assert not list(pipeline.settings.failed_dir().rglob("*.txt"))

    def test_managed_folder_is_still_archived(self, pipeline, tmp_path):
        pipeline.settings.import_root = str(tmp_path / "managed")
        managed = tmp_path / "managed"
        managed.mkdir(parents=True)
        f = managed / "Aravinth_chat.txt"
        f.write_text(WA_CONTENT, encoding="utf-8")

        assert pipeline.process_path(f).status == "COMPLETED"
        assert not f.exists()
        assert list(pipeline.settings.processed_dir().rglob("*.txt"))

    def test_rescan_of_a_left_in_place_file_is_a_noop(self, pipeline, tmp_path):
        pipeline.settings.import_root = str(tmp_path / "managed")
        drive = tmp_path / "My Drive" / "Backups"
        drive.mkdir(parents=True)
        f = drive / "Aravinth_chat.txt"
        f.write_text(WA_CONTENT, encoding="utf-8")
        assert pipeline.process_path(f).status == "COMPLETED"
        # Duplicate detection is by content checksum, so a repeat import of the
        # same file sitting in the folder does no harm.
        assert pipeline.process_path(f).status == "SKIPPED"
        assert f.exists()

    def test_classify_folder(self):
        assert cloudfolder.classify_folder(
            Path("G:\\My Drive\\Circle")) == "google_drive"
        assert cloudfolder.classify_folder(
            Path("/home/me/Dropbox/Chat")) == "dropbox"
        assert cloudfolder.classify_folder(Path("/home/me/Chat")) == "local"

    def test_root_is_managed_is_false_for_drive(self, pipeline, tmp_path):
        drive = tmp_path / "My Drive"
        drive.mkdir()
        pipeline.settings.import_root = str(drive)
        assert pipeline.settings.root_is_managed() is False
        assert pipeline.settings.should_archive(drive / "x.zip") is False

    def test_source_subfolders_not_created_in_a_user_folder(self, pipeline,
                                                             tmp_path):
        drive = tmp_path / "My Drive" / "Backups"
        drive.mkdir(parents=True)
        pipeline.settings.import_root = str(drive)
        watcher = FolderWatcher(settings=pipeline.settings, roots=[drive])
        watcher._ensure_layout()
        assert not (drive / "whatsapp").exists(), (
            "Circle must not litter the user's Drive with empty subfolders")


class TestCloudPlaceholders:
    def test_placeholder_detection_is_safe_off_windows(self, tmp_path):
        f = tmp_path / "a.txt"
        f.write_text("hello", encoding="utf-8")
        # Never raises, and is False for an ordinary local file.
        assert cloudfolder.is_placeholder(f) in (True, False)

    def test_materialize_reads_the_whole_file(self, tmp_path):
        f = tmp_path / "big.bin"
        payload = os.urandom(3 * 1024 * 1024)
        f.write_bytes(payload)
        assert cloudfolder.materialize(f, timeout=10) is True
        assert f.read_bytes() == payload

    def test_materialize_reports_failure_for_a_missing_file(self, tmp_path):
        assert cloudfolder.materialize(tmp_path / "gone.bin", timeout=5) is False

    def test_already_settled_file_does_not_wait_the_full_window(self, tmp_path):
        """An old, unchanged file must not cost the whole settle window.

        A synced Drive backup is thousands of files that were written hours
        ago. Paying 1.5s of sleep per file was over an hour of pure waiting.
        """
        import time as _time
        f = tmp_path / "old_chat.txt"
        f.write_text(WA_CONTENT, encoding="utf-8")
        old = _time.time() - 3600
        import os
        os.utime(f, (old, old))

        settings = get_settings()
        watcher = FolderWatcher(settings=settings, roots=[tmp_path])
        started = _time.time()
        assert watcher._wait_stable(f) is True
        assert _time.time() - started < 1.0, "settled file still waited"

    def test_file_still_being_written_is_not_accepted_early(self, tmp_path):
        """The fast path must not fire for a file whose mtime is recent."""
        import time as _time
        f = tmp_path / "growing_chat.txt"
        f.write_text(WA_CONTENT, encoding="utf-8")
        settings = get_settings()
        watcher = FolderWatcher(settings=settings, roots=[tmp_path])
        started = _time.time()
        assert watcher._wait_stable(f, timeout=6) is True
        # mtime is now, so the normal settle window applies.
        assert _time.time() - started >= settings.watcher_stability_seconds

    def test_wait_stable_materializes_before_measuring(self, tmp_path, monkeypatch):
        """A placeholder must be downloaded before it is measured.

        Regression risk: a placeholder reports its full size from remote
        metadata, so a size-stability check passes instantly and the file is
        hashed while still empty.
        """
        f = tmp_path / "backup.zip"
        f.write_bytes(b"zip-bytes")

        calls: list[Path] = []
        monkeypatch.setattr(cloudfolder, "is_placeholder", lambda p: True)
        monkeypatch.setattr(cloudfolder, "materialize",
                            lambda p, timeout=0: (calls.append(Path(p)), True)[1])

        settings = get_settings()
        watcher = FolderWatcher(settings=settings, roots=[tmp_path])
        assert watcher._wait_stable(f) is True
        assert calls == [f], "the cloud file was never materialized"

    def test_wait_stable_returns_false_when_download_never_finishes(
            self, tmp_path, monkeypatch):
        f = tmp_path / "backup.zip"
        f.write_bytes(b"zip-bytes")
        monkeypatch.setattr(cloudfolder, "is_placeholder", lambda p: True)
        monkeypatch.setattr(cloudfolder, "materialize",
                            lambda p, timeout=0: False)
        settings = get_settings()
        settings.cloud_materialize_timeout = 5.0
        watcher = FolderWatcher(settings=settings, roots=[tmp_path])
        assert watcher._wait_stable(f) is False

    def test_cloud_files_bypass_the_local_size_ceiling(self, pipeline, tmp_path):
        """Media in a backup archive is governed by the media ceiling, not the
        50MB text ceiling, so a Drive folder of photos still imports."""
        drive = tmp_path / "My Drive" / "Backups"
        drive.mkdir(parents=True)
        pipeline.settings.import_root = str(tmp_path / "managed")
        big = drive / "IMG-0001.jpg"
        big.write_bytes(b"\xff\xd8\xff" + b"0" * (2 * 1024 * 1024))
        result = pipeline.process_path(big)
        assert result.status == "COMPLETED"
        assert big.exists()


class TestWatchFolderApi:
    def test_list_add_and_remove(self, tmp_path):
        from fastapi.testclient import TestClient
        from circle.api import context as ctx_mod
        from circle.main import create_app

        extra = tmp_path / "My Drive" / "CircleBackups"
        managed = tmp_path / "managed"
        managed.mkdir()

        ctx = ctx_mod.AppContext.build(settings=_settings(managed))
        ctx_mod._ctx = ctx
        client = TestClient(create_app())

        try:
            listing = client.get("/api/watch/folders").json()
            assert listing["folders"][0]["primary"] is True
            assert listing["archives_files"] is True

            added = client.post("/api/watch/folders",
                                json={"path": str(extra)}).json()
            assert len(added["folders"]) == 2
            drive = added["folders"][1]
            assert drive["cloud"] is True
            assert drive["kind"] == "google_drive"
            assert drive["primary"] is False
            # A folder the user owns is read in place, never reorganized.
            assert drive["managed"] is False

            assert extra.exists()

            removed = client.request(
                "DELETE", "/api/watch/folders",
                params={"path": str(extra)}).json()
            assert len(removed["folders"]) == 1
        finally:
            ctx_mod._ctx = None

    def test_relative_path_is_rejected(self, tmp_path):
        from fastapi.testclient import TestClient
        from circle.api import context as ctx_mod
        from circle.main import create_app

        managed = tmp_path / "managed"
        managed.mkdir()
        ctx = ctx_mod.AppContext.build(settings=_settings(managed))
        ctx_mod._ctx = ctx
        client = TestClient(create_app())
        try:
            r = client.post("/api/watch/folders", json={"path": "relative/dir"})
            assert r.status_code == 400
        finally:
            ctx_mod._ctx = None

    def test_bootstrap_accepts_extra_folders(self, tmp_path):
        from fastapi.testclient import TestClient
        from circle.api import context as ctx_mod
        from circle.main import create_app

        managed = tmp_path / "managed"
        drive = tmp_path / "My Drive" / "Backups"
        managed.mkdir()
        ctx = ctx_mod.AppContext.build(settings=_settings(managed))
        ctx_mod._ctx = ctx
        client = TestClient(create_app())
        try:
            out = client.post("/api/bootstrap", json={
                "watch_folders": [str(drive)], "mark_done": True}).json()
            assert out["ok"] is True
            assert str(drive.resolve()) in out["roots"]
            assert drive.exists()
        finally:
            ctx_mod._ctx = None


def _settings(managed: Path):
    settings = get_settings().model_copy()
    settings.import_root = str(managed)
    settings.watcher_enabled = False
    settings.processed_root = str(managed.parent / "processed")
    settings.failed_root = str(managed.parent / "failed")
    settings.quarantine_root = str(managed.parent / "quarantine")
    return settings


@pytest.mark.parametrize("raw", ["", "  ", "a", "b"])
def test_extra_watch_roots_parses_env_style_lists(raw):
    settings = get_settings().model_copy()
    settings.watch_roots = raw
    settings.import_root = str(Path("managed"))
    # Unparseable/blank entries are dropped rather than raising.
    settings.extra_watch_roots()
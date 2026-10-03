"""Desktop launcher tests.

The launcher only ever runs for real inside a packaged build, where a mistake
shows up as an app that starts and serves nothing. These tests cover the two
decisions that are hard to get right and impossible to notice until then:
where the bundled assets are, and where data is allowed to be written.
"""
from __future__ import annotations

import os
import socket
import sys

import pytest

from circle import desktop

_LAUNCHER_ENV = ("IMPORT_ROOT", "SQLITE_FILE", "STORAGE_BACKEND",
                 "ACCESS_KEY", "WATCHER_ENABLED", "CIRCLE_DATA_DIR",
                 "CIRCLE_FRONTEND_DIST", "CIRCLE_NO_BROWSER")


@pytest.fixture(autouse=True)
def _restore_environment():
    """Undo what prepare_environment writes.

    It calls os.environ.setdefault directly, which monkeypatch cannot see, so
    without this the launcher's settings leak into every later test -- and a
    leaked IMPORT_ROOT or SQLITE_FILE points at a folder that no longer exists.
    """
    saved = {k: os.environ.get(k) for k in _LAUNCHER_ENV}
    yield
    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


class TestBundleLocation:
    def test_source_tree_points_at_the_repo_frontend(self):
        assert (desktop.bundle_dir() / "frontend" / "dist").name == "dist"
        assert (desktop.bundle_dir() / "frontend").exists(), \
            "source builds must find frontend/dist"

    def test_frozen_build_prefers_meipass(self, monkeypatch, tmp_path):
        """A one-file build unpacks to _MEIPASS, not next to the .exe."""
        unpacked = tmp_path / "_MEI12345"
        (unpacked / "frontend").mkdir(parents=True)
        monkeypatch.setattr(desktop, "is_frozen", lambda: True)
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        monkeypatch.setattr(sys, "_MEIPASS", str(unpacked), raising=False)
        monkeypatch.setattr(sys, "executable", str(tmp_path / "Circle.exe"))
        assert desktop.bundle_dir() == unpacked
        assert (desktop.bundle_dir() / "frontend").is_dir()

    def test_frozen_build_without_meipass_uses_the_executable_folder(
            self, monkeypatch, tmp_path):
        monkeypatch.setattr(desktop, "is_frozen", lambda: True)
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        monkeypatch.delattr(sys, "_MEIPASS", raising=False)
        monkeypatch.setattr(sys, "executable", str(tmp_path / "Circle.exe"))
        assert desktop.bundle_dir() == tmp_path


class TestDataLocation:
    def test_environment_override_wins(self, monkeypatch, tmp_path):
        monkeypatch.setenv("CIRCLE_DATA_DIR", str(tmp_path / "elsewhere"))
        assert desktop.app_data_dir() == (tmp_path / "elsewhere").resolve()

    def test_data_folder_is_created_and_configured(self, monkeypatch, tmp_path):
        data = tmp_path / "circle-home"
        monkeypatch.setenv("CIRCLE_DATA_DIR", str(data))
        for key in ("IMPORT_ROOT", "SQLITE_FILE", "STORAGE_BACKEND",
                    "ACCESS_KEY", "WATCHER_ENABLED"):
            monkeypatch.delenv(key, raising=False)
        made = desktop.prepare_environment()
        assert made.is_dir()
        assert os.environ["SQLITE_FILE"] == str(data / "circle.db")
        assert os.environ["STORAGE_BACKEND"] == "sqlite"

    def test_no_import_folder_is_invented(self, monkeypatch, tmp_path):
        """The launcher must not decide which folder the user reads.

        Creating an "imports" folder here would both make a directory nobody
        asked for and silently watch it, so the choice has to be the user's.
        """
        monkeypatch.setenv("CIRCLE_DATA_DIR", str(tmp_path / "home"))
        monkeypatch.delenv("IMPORT_ROOT", raising=False)
        desktop.prepare_environment()
        assert "IMPORT_ROOT" not in os.environ
        assert not (tmp_path / "home" / "imports").exists()

    def test_existing_environment_is_not_overwritten(self, monkeypatch, tmp_path):
        """A user who set IMPORT_ROOT in .env keeps their folder."""
        monkeypatch.setenv("CIRCLE_DATA_DIR", str(tmp_path / "home"))
        monkeypatch.setenv("IMPORT_ROOT", str(tmp_path / "my-exports"))
        desktop.prepare_environment()
        assert os.environ["IMPORT_ROOT"] == str(tmp_path / "my-exports")


class TestPortSelection:
    def _busy(self, port: int) -> socket.socket:
        s = socket.socket()
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", port))
        return s

    def test_preferred_port_used_when_free(self):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            free = probe.getsockname()[1]
        assert desktop.find_free_port(free) == free

    def test_falls_back_when_the_preferred_port_is_taken(self):
        with self._busy(0) as held:
            taken = held.getsockname()[1]
            chosen = desktop.find_free_port(taken)
            assert chosen != taken

    def test_chosen_port_is_always_bindable(self):
        chosen = desktop.find_free_port(0)
        with socket.socket() as s:
            s.bind(("127.0.0.1", chosen))     # must not raise

class TestSingleDataLocation:
    """One install must live in one folder.

    Deleting or backing up Circle is the promise that makes a local archive
    feel safe. It only holds if the archive and the working folders agree.
    """

    def test_archive_and_working_folders_share_one_root(self, tmp_path, monkeypatch):
        from circle.config import Settings

        for name in ("SQLITE_FILE", "WORK_ROOT", "IMPORT_ROOT", "DATA_HOME_DIR"):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("CIRCLE_DATA_DIR", str(tmp_path / "home"))
        data = desktop.prepare_environment()

        s = Settings(_env_file=None)
        for path in (s.sqlite_path(), s.processed_dir(), s.failed_dir(),
                     s.quarantine_dir(), s.temp_dir()):
            assert str(path).startswith(str(data)), path

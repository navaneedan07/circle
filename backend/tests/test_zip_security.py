"""ZIP security (spec §7/§32): zip-slip, traversal, bombs, absolute paths."""
from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from circle.security.files import (
    FileSecurityError, safe_extract_zip, sanitize_filename, sha256_file,
    validate_extension,
)


def _make_zip(path: Path, entries: dict[str, str | bytes]) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        for name, content in entries.items():
            zf.writestr(name, content)
    return path


class TestZipSecurity:
    def test_normal_extract(self, tmp_path):
        z = _make_zip(tmp_path / "ok.zip", {"a.txt": "hello",
                                            "sub/b.txt": "world"})
        out = safe_extract_zip(z, tmp_path / "out")
        assert len(out) == 2
        assert (tmp_path / "out" / "a.txt").read_text() == "hello"

    def test_zip_slip_blocked(self, tmp_path):
        z = _make_zip(tmp_path / "evil.zip", {"../../evil.txt": "pwned"})
        with pytest.raises(FileSecurityError):
            safe_extract_zip(z, tmp_path / "out")
        assert not (tmp_path / "evil.txt").exists()

    def test_absolute_path_blocked(self, tmp_path):
        z = _make_zip(tmp_path / "evil2.zip", {"/etc/passwd": "pwned"})
        with pytest.raises(FileSecurityError):
            safe_extract_zip(z, tmp_path / "out")

    def test_windows_drive_path_blocked(self, tmp_path):
        z = _make_zip(tmp_path / "evil3.zip", {"C:/Windows/evil.txt": "x"})
        with pytest.raises(FileSecurityError):
            safe_extract_zip(z, tmp_path / "out")

    def test_backslash_traversal_blocked(self, tmp_path):
        z = _make_zip(tmp_path / "evil4.zip", {"..\\..\\evil.txt": "x"})
        with pytest.raises(FileSecurityError):
            safe_extract_zip(z, tmp_path / "out")

    def test_corrupt_zip_blocked(self, tmp_path):
        bad = tmp_path / "bad.zip"
        bad.write_bytes(b"this is not a zip file at all")
        with pytest.raises(FileSecurityError):
            safe_extract_zip(bad, tmp_path / "out")

    def test_entry_count_limit(self, tmp_path):
        entries = {f"f{i}.txt": "x" for i in range(5001)}
        z = _make_zip(tmp_path / "many.zip", entries)
        with pytest.raises(FileSecurityError):
            safe_extract_zip(z, tmp_path / "out")


class TestFileValidation:
    def test_sanitize_strips_traversal(self):
        assert sanitize_filename("../../etc/passwd") == "passwd"
        assert "/" not in sanitize_filename("a/b/c.txt")
        assert "\\" not in sanitize_filename("a\\b\\c.txt")

    def test_sanitize_reserved_windows_names(self):
        assert sanitize_filename("CON.txt").startswith("_")

    def test_sanitize_control_chars(self):
        assert "\n" not in sanitize_filename("bad\nname.txt")

    def test_allowed_extensions(self):
        p = Path("x.whatsapp")
        with pytest.raises(FileSecurityError):
            validate_extension(p)
        assert validate_extension(Path("chat.txt")) == ".txt"

    def test_sha256_file(self, tmp_path):
        f = tmp_path / "a.txt"
        f.write_text("hello")
        h1 = sha256_file(f)
        assert h1 == sha256_file(f)
        f.write_text("hello2")
        h2 = sha256_file(f)
        assert h1 != h2

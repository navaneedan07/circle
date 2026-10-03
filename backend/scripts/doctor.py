"""Check that this machine can actually run Circle, and say how to fix it.

The usual first-run failure is not a bug in Circle: it is one prerequisite
that was skipped. An unwritable archive folder, a stopped Ollama, or a model
that was never pulled all look the same from the UI (nothing works), so this
script checks each one and prints the exact command that fixes it.

    .venv/Scripts/python scripts/doctor.py
    .venv/Scripts/python scripts/doctor.py --json

Exit code is 0 when nothing required is missing, 1 otherwise. Warnings (Node
absent, so the interface cannot be rebuilt) do not fail the run.

Only the standard library is used for the probes themselves, so the checks
still work if a dependency failed to install; the config import is wrapped so
a broken install reports a clear message instead of a traceback.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

OK = "ok"
WARN = "warn"
FAIL = "fail"

# Marker, so the output is readable in a plain terminal with no colour and no
# emoji (which can raise UnicodeEncodeError on a default Windows console).
_MARK = {OK: "[ok]  ", WARN: "[warn]", FAIL: "[fail]"}


def _short(text: str, limit: int = 90) -> str:
    """Trim a driver error to one readable line, cutting at a word boundary."""
    text = " ".join(str(text).split())
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0]
    return f"{cut}..."


@dataclass
class Check:
    name: str
    status: str
    detail: str = ""
    fix: str = ""

    @property
    def failed(self) -> bool:
        return self.status == FAIL


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)

    @property
    def failures(self) -> list[Check]:
        return [c for c in self.checks if c.failed]

    @property
    def ok(self) -> bool:
        return not self.failures


def _load_settings():
    """Return (settings, error). Never raises: a broken install is a finding."""
    try:
        from circle.config import get_settings
        return get_settings(), None
    except Exception as exc:  # noqa: BLE001 - report, do not crash
        return None, str(exc)


def check_python() -> Check:
    # Index access, not attributes: sys.version_info is a named tuple, but a
    # plain tuple (as tests pass in) has no .major/.minor.
    v = tuple(sys.version_info)[:3]
    shown = ".".join(str(part) for part in v)
    if v >= (3, 11):
        return Check("Python", OK, shown)
    return Check(
        "Python", FAIL, f"{v[0]}.{v[1]} is too old (need 3.11+)",
        "Install Python 3.11 or newer from python.org, then rebuild .venv.")


def check_archive(settings) -> Check:
    """The store must be openable and writable.

    SQLite is a file, so the failure mode is a folder that cannot be created
    or written rather than a service that is not running. Checked by opening
    the real database, because that is the only honest test.
    """
    backend = (getattr(settings, "storage_backend", "sqlite") or "sqlite").lower()
    if backend.startswith("mongo"):
        return check_mongodb(settings.database_url)
    path = settings.sqlite_path()
    try:
        import sqlite3
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path))
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS doctor_probe (id INTEGER PRIMARY KEY)")
            conn.execute("DROP TABLE doctor_probe")
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        return Check(
            "Archive (SQLite)", FAIL,
            f"cannot write {path} ({_short(exc)})",
            "Check that the folder exists and you have permission to write to "
            "it, or set SQLITE_FILE to a path you own.")
    exists = " (existing archive)" if path.exists() else ""
    return Check("Archive (SQLite)", OK, f"{path}{exists}")


def check_mongodb(database_url: str) -> Check:
    try:
        import pymongo
    except Exception as exc:  # noqa: BLE001
        return Check(
            "MongoDB", FAIL, f"pymongo is not installed ({exc})",
            "Install dependencies: .venv/Scripts/python -m pip install -r requirements.txt")
    try:
        client = pymongo.MongoClient(database_url, serverSelectionTimeoutMS=2500)
        client.admin.command("ping")
        client.close()
        return Check("MongoDB", OK, f"reachable at {database_url}")
    except Exception as exc:  # noqa: BLE001
        return Check(
            "MongoDB", FAIL,
            f"cannot reach {database_url} ({_short(exc)})",
            "Start MongoDB, or run one with Docker:\n"
            "docker run -d -p 27017:27017 --name circle-mongo mongo:7")


def _ollama_tags(base: str) -> tuple[list[str], str]:
    """Return (model names, error). Empty error means success."""
    url = f"{base.rstrip('/')}/api/tags"
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return [m.get("name", "") for m in data.get("models", [])], ""
    except urllib.error.URLError as exc:
        return [], str(exc.reason)
    except Exception as exc:  # noqa: BLE001
        return [], _short(exc)


def check_ollama(ollama_url: str) -> tuple[Check, list[str]]:
    names, err = _ollama_tags(ollama_url)
    if err:
        return Check(
            "Ollama", FAIL, f"cannot reach {ollama_url} ({err})",
            "Start Ollama (it runs in the background after install), or install it:\n"
            "https://ollama.com/download"), []
    return Check("Ollama", OK, f"reachable at {ollama_url} ({len(names)} model(s))"), names


def _has_model(names: list[str], wanted: str) -> bool:
    base = wanted.split(":")[0].lower()
    return any(n.split(":")[0].lower() == base for n in names)


def check_model(names: list[str], wanted: str, label: str, note: str = "") -> Check:
    if _has_model(names, wanted):
        return Check(label, OK, wanted)
    detail = f"{wanted} is not pulled yet"
    if note:
        detail += f" ({note})"
    return Check(label, FAIL, detail, f"Run:  ollama pull {wanted}")


def check_node() -> Check:
    npm = shutil.which("npm")
    if not npm:
        return Check(
            "Node", WARN, "npm not found; the web interface cannot be rebuilt",
            "Only needed to rebuild the UI. Install Node 18+ from nodejs.org.\n"
            "          Circle runs without it once frontend/dist exists.")
    try:
        out = subprocess.run([npm, "--version"], capture_output=True, text=True,
                             timeout=15).stdout.strip()
    except Exception as exc:  # noqa: BLE001
        return Check("Node", WARN, f"npm found but not runnable ({exc})",
                     "Reinstall Node 18+ from nodejs.org.")
    return Check("Node", OK, f"npm {out}")


def run_checks() -> Report:
    report = Report()
    report.checks.append(check_python())

    settings, err = _load_settings()
    if settings is None:
        report.checks.append(Check(
            "Configuration", FAIL, f"could not load settings ({err})",
            "Reinstall dependencies: .venv/Scripts/python -m pip install -r requirements.txt"))
        report.checks.append(check_node())
        return report

    report.checks.append(check_archive(settings))

    ollama_check, names = check_ollama(settings.ollama_url)
    report.checks.append(ollama_check)
    if ollama_check.failed:
        # Without Ollama there is nothing to say about models; avoid three
        # "not pulled" lines that all really mean "Ollama is not running".
        report.checks.append(Check(
            "Reasoning model", WARN, f"not checked ({settings.ollama_model})",
            "Start Ollama, then run this check again."))
        report.checks.append(Check(
            "Embedding model", WARN, f"not checked ({settings.ollama_embedding_model})",
            "Start Ollama, then run this check again."))
    else:
        report.checks.append(check_model(
            names, settings.ollama_model, "Reasoning model", "~5GB, one time"))
        report.checks.append(check_model(
            names, settings.ollama_embedding_model, "Embedding model", "~300MB, one time"))

    report.checks.append(check_node())
    return report


def render(report: Report) -> str:
    lines = ["", "Circle first-run check", "=" * 52]
    for c in report.checks:
        lines.append(f"{_MARK.get(c.status, '[????]')} {c.name:<16} {c.detail}")
    lines.append("=" * 52)

    if report.ok:
        lines.append("Everything Circle needs is ready. Start it with:")
        lines.append("  scripts/serve.sh        (Windows: scripts\\serve.bat)")
    else:
        lines.append("Fix these, then run this check again:")
        for c in report.failures:
            lines.append("")
            lines.append(f"  {c.name}: {c.detail}")
            for fix_line in c.fix.splitlines():
                lines.append(f"          {fix_line}")
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Check that the local archive, Ollama, the models and Node are ready.")
    parser.add_argument("--json", action="store_true",
                        help="print the result as JSON instead of text")
    args = parser.parse_args(argv[1:])

    report = run_checks()
    if args.json:
        print(json.dumps({
            "ok": report.ok,
            "checks": [
                {"name": c.name, "status": c.status, "detail": c.detail,
                 "fix": c.fix}
                for c in report.checks
            ],
        }, indent=2))
    else:
        print(render(report))
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

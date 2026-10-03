"""Dry-run a watch folder: what would Circle do with these files?

Walks a folder exactly the way the watcher does, reports the routing decision
per file, and parses (in memory only) to estimate records. Nothing is written
to MongoDB, so it is safe to point at a real Drive backup of several GB.

    .venv/Scripts/python scripts/inspect_folder.py "G:\My Drive\circle"

Useful before pointing Circle at a large folder: it shows how many files would
be ingested, how many are noise, and which parser each one reaches.
"""
from __future__ import annotations

import sys
import tempfile
import zipfile
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from circle.ingestion.watcher import is_in_progress  # noqa: E402
from circle.parsers import (  # noqa: E402
    generic as generic_parser,
    instagram as instagram_parser,
    telegram as telegram_parser,
    whatsapp as whatsapp_parser,
    x as x_parser,
)
from circle.security.media import MEDIA_EXTENSIONS  # noqa: E402

TEXT_EXT = {".txt", ".json", ".csv", ".html", ".htm", ".md", ".ics", ".vcf",
            ".eml", ".mbox", ".js"}
MEDIA_SKIP_EXTS = MEDIA_EXTENSIONS


def walk(root: Path, limit: int | None = None):
    n = 0
    for p in sorted(root.rglob("*")):
        if not p.is_file() or is_in_progress(p):
            continue
        if p.name.startswith("."):
            continue
        yield p
        n += 1
        if limit and n >= limit:
            return


def route(path: Path) -> str:
    """Mirror of IngestionPipeline._detect_source + _sniff."""
    name = path.name.lower()
    if name.endswith("_chat.txt") or "whatsapp" in name:
        return "whatsapp"
    if "telegram" in name:
        return "telegram"
    if "instagram" in name or "meta" in name:
        return "instagram"
    if name.startswith("direct-message") or "twitter" in name or \
            (name.endswith(".js") and "data" in name):
        return "x"
    ext = path.suffix.lower()
    if ext == ".js":
        return "x"
    if ext in (".eml", ".mbox"):
        return "email"
    if ext == ".ics":
        return "calendar"
    if ext == ".vcf":
        return "contacts"
    if ext in MEDIA_SKIP_EXTS:
        return "media"
    if ext in (".json", ".txt"):
        try:
            head = path.read_bytes()[:4000].decode("utf-8", errors="replace")
        except OSError:
            return "unreadable"
        if "sender_name" in head and "timestamp_ms" in head:
            return "instagram"
        if '"messages"' in head and '"from"' in head:
            return "telegram"
        if "participants" in head and "title" in head:
            return "instagram"
        if "message_create" in head or "YTD" in head:
            return "x"
    return "unknown"


def parse_count(path: Path) -> int | None:
    ext = path.suffix.lower()
    if ext not in TEXT_EXT:
        return None
    attempts = []
    if ext in (".txt", ".md"):
        attempts = [whatsapp_parser.parse_whatsapp, generic_parser.parse_generic]
    elif ext == ".json":
        attempts = [telegram_parser.parse_telegram, instagram_parser.parse_instagram,
                    x_parser.parse_x, generic_parser.parse_generic]
    elif ext == ".js":
        attempts = [x_parser.parse_x]
    elif ext in (".html", ".htm"):
        attempts = [generic_parser.parse_generic]
    else:
        return None
    for fn in attempts:
        try:
            res = fn(path)
            if not res.is_empty():
                return (len(res.messages) + len(res.emails) + len(res.events)
                        + len(res.notes) + len(res.documents) + len(res.contacts))
        except Exception:
            continue
    return None


def zip_entries(path: Path, limit: int = 40) -> Counter:
    kinds: Counter = Counter()
    try:
        with zipfile.ZipFile(path) as zf:
            for name in zf.namelist()[:limit]:
                ext = Path(name).suffix.lower()
                if ext in MEDIA_SKIP_EXTS and ext != ".zip":
                    kinds["media"] += 1
                elif ext in TEXT_EXT:
                    kinds["text"] += 1
                else:
                    kinds["other"] += 1
    except Exception as e:
        kinds[f"unreadable: {type(e).__name__}"] += 1
    return kinds


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    root = Path(sys.argv[1]).expanduser()
    limit = int(sys.argv[2]) if len(sys.argv) > 2 else None
    if not root.exists():
        print(f"not found: {root}")
        return 1

    files = list(walk(root, limit))
    print(f"{root}\n{len(files)} files\n")
    routed = Counter()
    est_records = 0
    parsed_json = 0
    unknown_examples: list[tuple[str, str]] = []
    per_dir: Counter = Counter()

    for p in files:
        src = route(p)
        routed[src] += 1
        try:
            rel = p.relative_to(root)
        except ValueError:
            rel = p
        per_dir[str(rel.parent)[:70]] += 1
        n = parse_count(p)
        if n:
            parsed_json += 1
            est_records += n
        elif src == "unknown" and len(unknown_examples) < 15:
            unknown_examples.append((str(rel)[:80], f"{p.stat().st_size}b"))

    print("routing:")
    for k, v in routed.most_common():
        print(f"  {v:6}  {k}")
    print(f"\nloose text/json files parsed: {parsed_json}, ~records: {est_records}")

    zips = [p for p in files if p.suffix.lower() == ".zip"]
    if zips:
        print(f"\narchives: {len(zips)}")
        agg: Counter = Counter()
        for z in zips:
            agg += zip_entries(z)
        for k, v in agg.most_common():
            print(f"  {v:6}  {k}")

    if unknown_examples:
        print("\nunrouted files (would fall back to the generic parser):")
        for rel, size in unknown_examples:
            print(f"  {rel}  ({size})")

    print("\nbusiest folders:")
    for d, n in per_dir.most_common(12):
        print(f"  {n:6}  {d}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
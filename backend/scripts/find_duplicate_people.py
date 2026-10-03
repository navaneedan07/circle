"""Queue same-name duplicate people for review in Settings.

Importing chat exports can split one human into several records when the same
name arrives from different threads or platforms. This script finds those
groups and queues them as suggestions so the user decides, in Settings >
Possible matches, whether to merge. It never merges anything itself.

    .venv/Scripts/python scripts/find_duplicate_people.py
    .venv/Scripts/python scripts/find_duplicate_people.py --dry-run
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from circle.config import get_settings  # noqa: E402
from circle.identity.resolver import IdentityResolver  # noqa: E402
from circle.repository.mongo import MongoStore  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true",
                    help="list what would be queued without writing")
    args = ap.parse_args()

    settings = get_settings()
    store = MongoStore(settings)
    resolver = IdentityResolver(store=store)

    groups = resolver.find_duplicate_people()
    if not groups:
        print("No duplicate names found.")
        return 0

    print(f"{len(groups)} duplicate group(s):\n")
    queued = 0
    for keep, drops in groups:
        print(f"keep: {keep.display_name}  [{keep.id}]")
        for drop in drops:
            line = (f"  duplicate: {drop.display_name}  [{drop.id}]")
            if args.dry_run:
                print(f"{line}   (would queue)")
            else:
                sid = resolver._queue_duplicate(keep, drop)
                print(f"{line}   queued as {sid}")
                queued += 1
        print()

    if args.dry_run:
        print("Dry run: nothing written.")
    else:
        print(f"Queued {queued} suggestion(s). "
              "Review them in Settings > Possible matches.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

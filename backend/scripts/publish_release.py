#!/usr/bin/env python
"""Publish a Circle release: verify it, then stage it for the download page.

Publishing was three hand-typed steps that had to agree with each other, and
one of them silently could not work: git hosts reject a file over 100 MB, and
the packaged app was 104 MB. Nothing failed loudly -- the page simply linked to
a file that was never there.

So this checks the things that must be true, and refuses to stage a release
that would 404:

  * the executable exists and is under the host's hard per-file limit
  * the download page and the app agree on the version and filename
  * the filename carries the version, so a cached page cannot serve a stale
    binary under a fresh name

Usage:
    python scripts/publish_release.py --check          # verify only
    python scripts/publish_release.py                  # stage into site/downloads
    python scripts/publish_release.py --force          # stage even if large
"""
from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
REPO = BACKEND.parent
SITE = REPO / "site"
DOWNLOADS = SITE / "downloads"

# GitHub refuses a file over 100 MB and the API rejects it outright, so this is
# a wall rather than a warning. Exceeding it means the file cannot be committed
# and the download link will 404 forever.
HOST_FILE_LIMIT_MB = 100


def fail(msg: str) -> None:
    print(f"FAIL  {msg}")


def ok(msg: str) -> None:
    print(f"ok    {msg}")


def exe_path() -> Path:
    return BACKEND / "dist" / "Circle.exe"


def declared_name() -> str | None:
    """The filename the download page currently points at."""
    html = (SITE / "index.html").read_text(encoding="utf8")
    m = re.search(r'href="downloads/([^"]+)"', html)
    return m.group(1) if m else None


def declared_size(html: str) -> int | None:
    m = re.search(r"about\s*(\d+)\s*(?:&nbsp;|nbsp)?\s*MB", html)
    return int(m.group(1)) if m else None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true",
                    help="verify only; do not copy anything")
    ap.add_argument("--force", action="store_true",
                    help=f"stage even if larger than {HOST_FILE_LIMIT_MB} MB")
    args = ap.parse_args()

    problems: list[str] = []

    exe = exe_path()
    if not exe.is_file():
        fail(f"no build at {exe}. Run: python -m PyInstaller circle.spec --noconfirm")
        return 1
    size_mb = exe.stat().st_size / (1024 * 1024)
    ok(f"build found: {size_mb:.1f} MB")

    if size_mb > HOST_FILE_LIMIT_MB and not args.force:
        fail(f"{size_mb:.1f} MB exceeds the {HOST_FILE_LIMIT_MB} MB host limit, "
             "so it cannot be committed and the download would 404.")
        print("      Trim the packaged dependencies (see circle.spec excludes) "
              "or publish outside git.")
        problems.append("too large to host")
    else:
        ok(f"within the {HOST_FILE_LIMIT_MB} MB host limit")

    name = declared_name()
    if not name:
        fail("site/index.html has no downloads/ link to check against")
        return 1

    version = None
    m = re.match(r"Circle-(\d+\.\d+\.\d+)-windows-x64\.exe$", name)
    if m:
        version = m.group(1)
        ok(f"download link is versioned: {name} (v{version})")
    else:
        fail(f"download filename does not carry a version: {name}. A cached page "
             "could then serve a stale binary under a fresh name.")
        problems.append("unversioned filename")

    # The app reads the same facts from the frontend; they must agree.
    vts = REPO / "frontend" / "src" / "version.ts"
    if vts.is_file():
        ts = vts.read_text(encoding="utf8")
        if version and f'"{version}"' not in ts:
            fail(f"frontend/src/version.ts does not mention version {version}; the "
                 "in-app download link would disagree with the site")
            problems.append("version drift")
        else:
            ok("frontend version constants agree with the site")

    html = (SITE / "index.html").read_text(encoding="utf8")
    stated = declared_size(html)
    if stated is not None:
        # The page describes the download; a stale number is a small lie.
        drift = abs(stated - round(size_mb))
        if drift <= 5:
            ok(f"stated size ({stated} MB) matches the build ({size_mb:.1f} MB)")
        else:
            fail(f"site says {stated} MB but the build is {size_mb:.1f} MB; "
                 "update the copy in site/index.html")
            problems.append("size drift")

    if problems:
        print(f"\n{len(problems)} problem(s). Nothing was staged.")
        return 1

    if args.check:
        print("\nAll checks pass. Run without --check to stage the download.")
        return 0

    DOWNLOADS.mkdir(parents=True, exist_ok=True)
    dest = DOWNLOADS / name
    shutil.copy2(exe, dest)
    print(f"\nstaged {dest.relative_to(REPO)} ({dest.stat().st_size / 1048576:.1f} MB)")
    print("It is gitignored, so commit it explicitly:")
    print(f"    git add -f site/downloads/{name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
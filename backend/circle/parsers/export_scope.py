"""Recognising platform data exports, and what inside them is worth reading.

Instagram ("Meta") hands you a folder that is mostly *not* conversations:
account settings, ad interests, login history, follower lists, and every photo
you have ever posted. In a real export that is thousands of files against a
few hundred message threads, and the non-message JSON is actively harmful: fed
to a generic parser it invents "people" and junk memories.

So inside an export we only read the conversation tree:

    <export>/your_instagram_activity/messages/inbox/<thread>/message_1.json
    <export>/your_instagram_activity/messages/inbox/<thread>/photos/*.jpg

Everything else is skipped, cheaply, before any file is opened.
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterable, Optional

# Directory names that only appear in an Instagram/Meta data export. Used as
# an unambiguous marker so a WhatsApp folder called "meta-something" is not
# mistaken for an export.
EXPORT_MARKER_DIRS = frozenset({
    "personal_information",
    "your_instagram_activity",
    "connections",
    "ads_information",
    "security_and_login_information",
    "apps_and_websites_off_of_instagram",
    "logged_information",
    "preferences",
    "monetization",
})

# Folder prefixes Meta uses when it names the export folder itself.
EXPORT_MARKER_PREFIXES = ("instagram-", "meta-")

# The conversation tree, and the media that belongs to it.
_INBOX_PARTS = ("messages", "inbox")
_ATTACHMENT_DIRS = frozenset({"photos", "videos", "audio", "gifs", "files"})

# Message files we know how to read. Instagram names them message_1.json (or
# message_1.html in newer exports); requiring that name keeps unrelated JSON in
# the thread folder from being mistaken for a conversation.
_MESSAGE_SUFFIXES = frozenset({".json", ".ndjson", ".html", ".htm"})


def _parts_of(path: Path | str) -> list[str]:
    return [p.lower() for p in Path(path).parts]


def is_data_export(path: Path | str) -> bool:
    """True when the path sits inside an Instagram/Meta data export."""
    for part in _parts_of(path):
        if part in EXPORT_MARKER_DIRS:
            return True
        if part.startswith(EXPORT_MARKER_PREFIXES):
            return True
    return False


def _in_inbox(parts: list[str]) -> bool:
    return all(p in parts for p in _INBOX_PARTS)


def in_conversation_tree(path: Path | str) -> bool:
    """True for message files and their attachments, inside an export."""
    p = Path(path)
    parts = _parts_of(p)
    if not _in_inbox(parts):
        return False
    suffix = p.suffix.lower()
    if p.stem.lower().startswith("message") and suffix in _MESSAGE_SUFFIXES:
        return True
    # media sitting next to the thread it belongs to
    return suffix != "" and any(part in _ATTACHMENT_DIRS for part in parts[:-1])


def skip_reason(path: Path | str) -> Optional[str]:
    """Why this file should not be ingested, or None if it should be.

    Applies only inside a recognised data export. Anything outside an export is
    left entirely to the normal routing rules.
    """
    if not is_data_export(path):
        return None
    if in_conversation_tree(path):
        return None
    suffix = Path(path).suffix.lower()
    if suffix == "":
        return "export directory marker"
    return "not a conversation file in a Meta data export"


def conversation_files(root: Path) -> Iterable[Path]:
    """Every ingestible file under an export root (used by tooling/tests)."""
    for p in sorted(root.rglob("*")):
        if p.is_file() and skip_reason(p) is None:
            yield p
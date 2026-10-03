"""X/Twitter data-export parser (user-provided export only -- no scraping).

Handles the official archive formats:
  - JS assignment files:  window.YTD.dl.part0 = [ ... ]   (data/*.js)
  - Plain JSON:           [{"message_create": {...}}, ...]
  - Newer DM json files:  {"messages": [...], "participants": [...]}

Extracts DM conversations and normalizes them into Messages.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Optional

from circle.domain.models import DataOrigin, Message, SourceType
from circle.parsers.common import ParseResult, clean_text, message_id, parse_iso

_JS_ASSIGN_RE = re.compile(r"^[^=]*=\s*", re.DOTALL)


def _load(path: Path) -> Any:
    raw = path.read_bytes()
    text: Optional[str] = None
    for enc in ("utf-8", "utf-8-sig", "utf-16", "latin-1"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        text = raw.decode("utf-8", errors="replace")
    text = text.strip().rstrip(";").strip()
    if not text:
        raise ValueError("empty X export")

    # Strip JS wrapper: window.YTD.xxx.part0 = [...]
    if not text[0] in "[{":
        m = re.search(r"=\s*", text)
        if m:
            text = text[m.end():].strip().rstrip(";").strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # maybe concatenated objects / NDJSON
        rows = []
        for line in text.splitlines():
            line = line.strip().rstrip(";")
            if not line:
                continue
            if not line[0] in "[{":
                mm = re.search(r"=\s*", line)
                if mm:
                    line = line[mm.end():].strip().rstrip(";")
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        if rows:
            return rows
        raise ValueError("not parseable as X export JSON/JS")


def _iter_dm_items(data: Any):
    if isinstance(data, list):
        for item in data:
            yield item
    elif isinstance(data, dict):
        for key in ("direct_messages", "direct_messages-1", "messages",
                    "dm", "conversations"):
            if isinstance(data.get(key), list):
                for item in data[key]:
                    yield item


def _message_text(md: dict) -> str:
    parts = []
    text = md.get("text")
    if isinstance(text, str):
        parts.append(text)
    for url in md.get("urls") or []:
        if isinstance(url, dict) and url.get("url"):
            parts.append(str(url["url"]))
    return "\n".join(p for p in parts if p)


def parse_x(path: Path) -> ParseResult:
    data = _load(path)
    result = ParseResult()

    # Gather account/user id -> display name map if present
    user_names: dict[str, str] = {}

    def harvest_users(obj: Any):
        if isinstance(obj, dict):
            acct = obj.get("account")
            if isinstance(acct, dict) and acct.get("accountId"):
                user_names[str(acct["accountId"])] = clean_text(
                    acct.get("userName") or acct.get("fullName") or "")
            for v in obj.values():
                harvest_users(v)
        elif isinstance(obj, list):
            for v in obj:
                harvest_users(v)

    if "js" in path.name.lower() and "user" in path.name.lower() or path.name.startswith("users"):
        harvest_users(data)

    # Newer shape: {"messages": [{"message_create": ...}], "participants": [...]}
    # Classic:     [{"message_create": ...}, ...]  (per DM file)
    # Wrapped:      [{"direct_messages": [...]}]
    candidates: list[dict] = []
    if isinstance(data, dict) and "messages" in data:
        candidates = [{"messages": data["messages"],
                       "participants": data.get("participants") or []}]
    elif isinstance(data, list):
        for item in data:
            if isinstance(item, dict) and "messages" in item:
                candidates.append(item)
            elif isinstance(item, dict) and "message_create" in item:
                candidates.append({"messages": [item], "participants": []})
            elif isinstance(item, dict):
                for sub in _iter_dm_items(item):
                    if isinstance(sub, dict) and ("message_create" in sub or "messages" in sub):
                        candidates.append(sub if "messages" in sub
                                          else {"messages": [sub], "participants": []})

    count = 0
    for cand in candidates:
        raw_messages = cand.get("messages") or []
        participants = [clean_text(p.get("participant") or p.get("name") or "")
                        if isinstance(p, dict) else clean_text(p)
                        for p in (cand.get("participants") or [])]
        conv_key = "x:" + re.sub(r"[^a-z0-9]+", "-",
                                 "|".join(sorted(participants)).lower()).strip("-") or "dm"
        if not any(c["external_key"] == conv_key for c in result.conversations):
            result.conversations.append({
                "external_key": conv_key,
                "title": "Direct messages",
                "source": SourceType.X,
                "participants": [p for p in participants if p],
            })

        for raw in raw_messages:
            mc = raw.get("message_create") if isinstance(raw, dict) else None
            if not isinstance(mc, dict):
                # newer flat shape: {"id", "send_timestamp", "text", "sender_id", ...}
                if isinstance(raw, dict) and ("text" in raw or "body" in raw):
                    mc = raw
                else:
                    continue
            md = mc.get("message_data") if isinstance(mc.get("message_data"), dict) else {}
            body = _message_text(md) if md else clean_text(
                mc.get("text") or mc.get("body") or "")
            if not body:
                continue
            sender_id = str(mc.get("senderId") or mc.get("sender_id") or "")
            sender = user_names.get(sender_id) or mc.get("sender_screen_name") \
                or (sender_id or "unknown")
            when = parse_iso(mc.get("createdAt") or mc.get("send_timestamp")
                             or mc.get("created_at"))
            msg = Message(
                id=message_id("x", conv_key, when, sender, body),
                conversation_id=conv_key,
                sender_label=str(sender),
                sent_at=when,
                content=clean_text(body),
                source=SourceType.X,
                origin=DataOrigin.IMPORTED,
                external_id=conv_key,
            )
            result.messages.append(msg)
            count += 1

    if count == 0:
        raise ValueError("X export contained no messages")
    return result

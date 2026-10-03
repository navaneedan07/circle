"""Relationship metrics + profiles (spec §22, §23, §63).

Observable interaction data only: counts, recency, topics, upcoming events.
Status is always explainable ("12 interactions in the last 14 days").
NEVER emotional/mental-health inferences.
"""
from __future__ import annotations

import logging
import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from circle.domain.models import (
    CalendarEvent, Email, Memory, Message, Note, RelationshipEvent,
    RelationshipProfile, RelationshipStatus, TopicStat, VoiceRecording,
)
from circle.repository.mongo import MongoStore

log = logging.getLogger("circle.relationship")

STOPWORDS = {
    "the", "and", "for", "you", "your", "that", "with", "this", "have", "was",
    "are", "but", "not", "can", "will", "just", "they", "them", "then", "than",
    "what", "when", "where", "who", "how", "why", "all", "any", "our", "out",
    "get", "got", "had", "has", "him", "his", "her", "she", "his", "been", "from",
    "are", "were", "there", "their", "would", "could", "should", "about", "into",
    "over", "under", "again", "still", "very", "much", "some", "like", "want",
    "don't", "didn't", "can't", "i'm", "it's", "that's", "im", "ive", "ok",
    "okay", "yes", "no", "lol", "haha", "thanks", "thank", "please", "hello",
    "hi", "hey", "good", "morning", "night", "today", "tomorrow", "yesterday",
    "http", "https", "www", "com", "image", "omitted", "media", "sticker",
    "voice", "message", "video", "audio", "attachment", "file",
}


# Client-generated system events. Instagram and WhatsApp write these into the
# export as message text, so a frequency count over them counts the export
# format rather than the conversation: "Reacted" was the top "topic" across a
# 120k-message archive with 2,416 mentions.
SYSTEM_EVENTS = {
    "reacted", "liked", "shared", "forwarded", "removed", "unsupported",
    "attachment", "attachments", "image", "omitted", "sticker", "video",
    "audio", "voice message", "sent an attachment", "media omitted",
    "this message was deleted", "message deleted", "edited", "pinned",
    "replied", "seen", "delivered", "read", "typing", "joined", "left",
    "jpg", "png", "pdf", "mp4", "img", "photo", "link", "document",
}

# Chat filler that carries no subject. This list is deliberately short: an
# earlier version grew to ~200 entries chasing individual observed words
# ("Deii", "Okayy", "Tmrw") and still let new ones through every week.
# Structural rules below do the real work.
CHAT_FILLER = {
    "everyone", "anyone", "someone", "somebody", "nobody", "nothing",
    "guys", "folks", "bros", "bro", "dude", "yaar", "team", "guys",
    "those", "these", "this", "that", "it", "there", "here",
    "happy", "birthday", "good", "morning", "night", "evening", "today",
    "tomorrow", "yesterday", "week", "month", "day", "days", "time", "date",
    "everyone's", "any", "some", "all", "both", "each", "few", "many",
    "yeah", "yep", "yup", "nope", "nah", "okay", "ok", "fine", "cool",
    "nice", "great", "super", "thanks", "thank", "please", "sorry",
    "hmm", "hmm", "hyy", "ohh", "oh", "ah", "ha", "haha", "hehe", "lol",
    "hlo", "hru", "byee", "yoo", "haa", "haan", "take", "stay", "tell",
    "append", "check", "sent", "send", "ping", "reply", "call", "later",
    "tmrw", "tommorow", "aprm", "mr", "mrs", "ms", "sir", "madam", "dear",
    "regards", "welcome", "sorry", "excuse", "meet", "meeting", "call",
    "please", "note", "notes", "kindly", "forward", "fwd", "photo", "video",
}

# Code-mixed chat shorthand. Tamil/English transliterations of function words
# appear constantly in this archive and are not subjects. Matched as full
# words (plus a small set of explicit elongations), NOT by prefix: a 3-char
# prefix test also rejected real subjects ("enna" killed "Enna" but a naive
# "ser" rule would kill "server" and "series").
_FILLER_STEMS = {
    "naa", "naan", "naaa", "dei", "deii", "deiii", "dee", "nee", "nan",
    "nann", "enna", "eppo", "ithu", "ithula", "appa", "akka", "thambi",
    "poda", "seri", "ama", "han", "illa", "illai", "ena", "dm", "ok", "okk",
    "hmm", "hru", "hlo", "tmrw", "mk", "plz", "pls", "thala", "vaa",
    "dai", "day", "enaku", "aku", "lo", "la",
    "okayy", "okey", "asap", "pls", "grp", "machan", "bro", "sis", "k",
}

_WORD_RE = re.compile(r"[a-z0-9]+")


def _is_filler_token(word: str) -> bool:
    """Filler test for a single token, exact match only."""
    return (word in STOPWORDS or word in CHAT_FILLER
            or word in SYSTEM_EVENTS or word in _FILLER_STEMS)


def _is_filler_word(phrase: str) -> bool:
    """True when a phrase is entirely filler, or every word in it is.

    "Thank you" is two filler words and must go; "Machine Learning" is two
    content words and must stay, so this checks all-of rather than any-of.
    """
    low = phrase.strip().lower()
    if low in SYSTEM_EVENTS or low in CHAT_FILLER or low in STOPWORDS:
        return True
    words = _WORD_RE.findall(low)
    if not words:
        return True
    return all(_is_filler_token(w) for w in words)


def _is_noise_topic(phrase: str, person_names: Optional[set[str]] = None) -> bool:
    """True when a phrase is not a subject worth ranking.

    Rejects, in order: empty/short phrases, stopwords, client system events,
    chat filler (including code-mixed stems), and the names of people already
    known to the archive. A person's name is not a topic: it is who was in
    the room, not what was discussed.
    """
    low = phrase.strip().lower()
    if len(low) < 3:
        return True
    if _is_filler_word(low):
        return True
    words = _WORD_RE.findall(low)
    if words and all(_is_filler_token(w) for w in words):
        return True
    if person_names:
        for token in words:
            if token in person_names:
                return True
    return False


def extract_topics(text: str, limit: int = 5,
                   person_names: Optional[set[str]] = None) -> list[str]:
    """Lightweight local topic extraction (no LLM round-trip per message).

    Captures capitalized multi-word phrases and frequent meaningful terms.
    """
    if not text:
        return []
    topics: list[str] = []
    # Capitalized phrases: "SIH project", "Machine Learning"
    for m in re.finditer(r"\b([A-Z][a-zA-Z0-9]+(?:[ \-]+[A-Z][a-zA-Z0-9]+){0,3})\b", text):
        phrase = m.group(1).strip()
        if _is_noise_topic(phrase, person_names):
            continue
        if phrase not in topics:
            topics.append(phrase)
        if len(topics) >= limit:
            return topics[:limit]
    # Frequent meaningful tokens
    words = re.findall(r"[a-zA-Z][a-zA-Z0-9#]{3,}", text.lower())
    counts = Counter(w for w in words
                     if w not in STOPWORDS and not _is_filler_word(w))
    for word, n in counts.most_common(limit * 2):
        if n >= 2 and word not in [t.lower() for t in topics]:
            topics.append(word)
        if len(topics) >= limit:
            break
    return topics[:limit]


def record_event(store: MongoStore, *, person_id: str, kind: str, source: str,
                 occurred_at: Optional[datetime], summary: str,
                 record_id: str) -> None:
    if not person_id or not occurred_at:
        return
    ev = RelationshipEvent(
        person_id=person_id, kind=kind, source=source,  # type: ignore[arg-type]
        occurred_at=occurred_at, summary=summary[:200], record_id=record_id,
    )
    ev.id = f"rel-{record_id}"
    store.insert_relationship_event(ev)


def compute_profile(store: MongoStore, person_id: str) -> Optional[RelationshipProfile]:
    person = store.get_person(person_id)
    if not person:
        return None
    now = datetime.now(timezone.utc)
    cutoff14 = now - timedelta(days=14)
    cutoff60 = now - timedelta(days=60)

    events = store.list_relationship_events(person_id, limit=20000)
    total = len(events)
    recent14 = 0
    recent60 = 0
    last: Optional[datetime] = None
    breakdown: Counter[str] = Counter()

    for e in events:
        occ = e.get("occurred_at")
        dt: Optional[datetime] = None
        if isinstance(occ, datetime):
            dt = occ if occ.tzinfo else occ.replace(tzinfo=timezone.utc)
        elif isinstance(occ, str):
            try:
                dt = datetime.fromisoformat(occ.replace("Z", "+00:00"))
            except ValueError:
                dt = None
        if dt:
            if last is None or dt > last:
                last = dt
            if dt >= cutoff14:
                recent14 += 1
            if dt >= cutoff60:
                recent60 += 1
        breakdown[str(e.get("source", "unknown"))] += 1

    # Topics from memories (all-time + active window)
    memories = store.memories_for_person(person_id, limit=3000)
    topic_counter: Counter[str] = Counter()
    active_counter: Counter[str] = Counter()
    # The person's own name is not a topic. Build the exclusion set from the
    # live person plus aliases so a rename cannot leave a stale filter.
    own_names: set[str] = set()
    own_low = person.display_name.strip().lower()
    if own_low:
        own_names.add(own_low)
        own_names.update(p for p in re.split(r"[^a-z0-9]+", own_low) if len(p) >= 3)
    for alias in person.aliases or []:
        al = str(alias).strip().lower()
        if al:
            own_names.add(al)
            own_names.update(p for p in re.split(r"[^a-z0-9]+", al) if len(p) >= 3)
    for mem in memories:
        occ = mem.occurred_at
        if isinstance(occ, str):
            try:
                occ = datetime.fromisoformat(occ.replace("Z", "+00:00"))
            except ValueError:
                occ = None
        if isinstance(occ, datetime) and occ.tzinfo is None:
            occ = occ.replace(tzinfo=timezone.utc)
        is_active = bool(occ and occ >= cutoff14)
        for t in mem.topics:
            # Memories were extracted before the noise filter existed, so their
            # topics still carry system events and chat filler. Filter here as
            # well as at read time: this is the path the API actually serves.
            if _is_noise_topic(t, own_names):
                continue
            topic_counter[t] += 1
            if is_active:
                active_counter[t] += 1

    status, reason = _status(total, recent14, recent60, last)

    upcoming: list[dict[str, Any]] = []
    for ev in store.list_calendar_upcoming(person_id, limit=5):
        starts = ev.starts_at
        upcoming.append({
            "id": ev.id, "title": ev.title,
            "starts_at": starts.isoformat() if isinstance(starts, datetime) else starts,
            "location": ev.location,
        })

    existing = store.get_profile(person_id)
    profile = RelationshipProfile(
        person_id=person_id,
        status=status,
        status_reason=reason,
        interaction_count=total,
        interactions_14d=recent14,
        interactions_60d=recent60,
        last_interaction_at=last,
        source_breakdown=dict(breakdown.most_common()),
        topics=[TopicStat(topic=t, count=n,
                          last_seen=_last_seen_for(memories, t))
                for t, n in topic_counter.most_common(8)],
        active_topics=[TopicStat(topic=t, count=n) for t, n in active_counter.most_common(6)],
        upcoming_events=upcoming,
        summary=existing.summary if existing else "",
        summary_sources=existing.summary_sources if existing else [],
        updated_at=now,
    )
    store.upsert_profile(profile)
    return profile


def _last_seen_for(memories: list[Memory], topic: str) -> Optional[datetime]:
    for mem in memories:
        if topic in mem.topics:
            occ = mem.occurred_at
            if isinstance(occ, str):
                try:
                    return datetime.fromisoformat(occ.replace("Z", "+00:00"))
                except ValueError:
                    return None
            if isinstance(occ, datetime):
                return occ if occ.tzinfo else occ.replace(tzinfo=timezone.utc)
    return None


def _status(total: int, recent14: int, recent60: int,
            last: Optional[datetime]) -> tuple[RelationshipStatus, str]:
    """Explainable status from observable interaction data only."""
    now = datetime.now(timezone.utc)
    if last is None:
        return (RelationshipStatus.NO_RECENT_ACTIVITY,
                "No recorded interactions yet.")
    days_since = (now - last).days
    if days_since > 45:
        return (RelationshipStatus.NO_RECENT_ACTIVITY,
                f"No interactions in the last {days_since} days.")
    if recent14 >= 15:
        return (RelationshipStatus.VERY_ACTIVE,
                f"{recent14} interactions in the last 14 days.")
    if recent14 >= 5:
        return (RelationshipStatus.ACTIVE,
                f"{recent14} interactions in the last 14 days.")
    if recent60 >= 3:
        return (RelationshipStatus.OCCASIONAL,
                f"{recent60} interactions in the last 60 days "
                f"({recent14} in the last 14).")
    if days_since <= 30:
        return (RelationshipStatus.LOW_ACTIVITY,
                f"{recent60} interactions in the last 60 days; "
                f"last interaction {days_since} days ago.")
    return (RelationshipStatus.NO_RECENT_ACTIVITY,
            f"Last interaction {days_since} days ago.")


def ingest_memory_topics(memories: list[Memory]) -> None:
    for mem in memories:
        if not mem.topics:
            mem.topics = extract_topics(mem.text)

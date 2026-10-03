"""Document <-> model conversion shared by every storage engine.

MongoDB and SQLite both store a document and materialise the same pydantic
models, so the conversion lives here exactly once. Two engines with two
copies of this would drift, and a model field silently missing from one
engine is the kind of bug that only shows up in production.
"""
from __future__ import annotations

import re
from typing import Any, Optional

_STOPWORDS = frozenset("""
a about after all also an and any are as at be been but by can did do does for
from had has have he her him his how i if in into is it its me my no not of on
or our out she should so some such than that the their them then there these
they this to too up us was we were what when where which who whom why will with
would you your
""".split())

# Crude suffix stripping so "learning" also matches "learn", "models" -> "model".
_SUFFIXES = ("ing", "ed", "es", "s")


def content_terms(query: str) -> list[str]:
    """Meaningful, de-duplicated search terms (lowercase, stemmed)."""
    raw = re.sub(r"[^\w\s]", " ", query or "").lower().split()
    out: list[str] = []
    for w in raw:
        if w in _STOPWORDS or len(w) < 3 or w.isdigit():
            continue
        stem = w
        if len(w) > 5:
            for suf in _SUFFIXES:
                if w.endswith(suf) and len(w) - len(suf) >= 3:
                    stem = w[: -len(suf)]
                    break
        if stem and stem not in out:
            out.append(stem)
    return out


def serialize(obj: Any) -> Any:
    """Pydantic -> storage-safe dict (keep datetimes, drop None ids)."""
    if hasattr(obj, "model_dump"):
        data = obj.model_dump(mode="json")
        if data.get("id") is None:
            data.pop("id", None)
        return data
    return obj


def deserialize(model, data: Optional[dict]):
    if data is None:
        return None
    data = dict(data)
    oid = data.pop("_id", None)
    if oid is not None and "id" in getattr(model, "model_fields", {}) \
            and data.get("id") is None:
        data["id"] = oid
    try:
        return model.model_validate(data)
    except Exception:
        # Tolerate legacy/odd docs: strip unknown keys
        known = set(model.model_fields)
        data = {k: v for k, v in data.items() if k in known}
        return model.model_validate(data)


def clean_profile_topics(profile) -> None:
    """Drop system events, interjections and the person's own name from topics.

    Profiles were written before the noise filter existed, so they still carry
    "Reacted", "Naa" and "Dei" as topics. Cleaning on read means existing
    profiles get fixed without a re-import, and no consumer can reintroduce
    the noise by reading the collection directly.
    """
    from circle.relationship.metrics import _is_noise_topic
    person = person_name_cache.get(profile.person_id)
    own = set()
    if person:
        low = person.lower()
        own.add(low)
        # "Shrijesh Kannan" also has to suppress the topic "Shrijesh".
        own.update(p for p in re.split(r"[^a-z0-9]+", low) if len(p) >= 3)
    profile.topics = [t for t in profile.topics
                      if not _is_noise_topic(t.topic, own)]
    profile.active_topics = [t for t in profile.active_topics
                             if not _is_noise_topic(t.topic, own)]


# Filled by each store so profile reads can suppress a person's own name as a
# "topic" without a second query per profile. Shared, because the caller only
# ever has one store open.
person_name_cache: dict[str, str] = {}

# Back-compat aliases: mongo.py historically exported these underscored names
# and other modules import them from there.
_Person_NAME_CACHE = person_name_cache


def refresh_person_names(rows) -> None:
    """Rebuild the name cache from (person_id, display_name) pairs."""
    try:
        person_name_cache.clear()
        for pid, name in rows:
            cleaned = str(name or "").strip().lower()
            if cleaned:
                person_name_cache[str(pid)] = cleaned
    except Exception:
        pass

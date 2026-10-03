"""Identity resolution (spec §5).

Deterministic signals first: email, phone, explicit mapping, contacts,
exact usernames. Fuzzy/LLM matching only ever produces a *suggestion* --
uncertain identities are NEVER merged silently. The user decides.
"""
from __future__ import annotations

import logging
import re
from collections import defaultdict
from dataclasses import dataclass
from typing import Optional

from circle.domain.models import DataOrigin, IdentityLink, Person, SourceType
from circle.parsers.common import ParsedContact, clean_text, normalize_email, normalize_phone

log = logging.getLogger("circle.identity")

USER_NAMES_DEFAULT = {"me", "you", "myself", "self"}


@dataclass
class Suggestion:
    id: str
    person_a_id: str
    person_b_id: str
    confidence: float
    reason: str
    detail: dict


# Sender labels a platform uses when the real account is gone or hidden.
_PLACEHOLDER_LABELS = frozenset({
    "instagram user", "instagram", "messenger", "unknown", "unknown user",
    "deleted user", "deleted account", "deactivated", "former user",
})


def _norm_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (name or "").lower()).strip()


class IdentityResolver:
    def __init__(self, store):
        self.store = store
        user_names = store.get_setting("user_names")
        self.user_names = {str(n).lower() for n in (user_names or [])} | USER_NAMES_DEFAULT

    # ------------------------------------------------------------------
    def is_user_label(self, label: str) -> bool:
        return _norm_name(label) in {_norm_name(n) for n in self.user_names}

    def is_placeholder_label(self, label: str) -> bool:
        """True when a sender label is not a person.

        Instagram replaces deleted accounts with "Instagram user" and allows
        anonymous or emoji-only display names. Creating a Person for those
        invents contacts that do not exist: in one real export, 61 such labels
        accounted for 30,270 of 103,033 messages.
        """
        core = (label or "").strip()
        if not core or len(core) < 2:
            return True
        if not any(ch.isalnum() for ch in core):
            return True     # "~", or an emoji-only name
        return _norm_name(core) in _PLACEHOLDER_LABELS

    def get_or_create_user(self) -> Optional[Person]:
        for p in self.store.list_people(limit=2000):
            if p.is_user:
                return p
        return None

    # ------------------------------------------------------------------
    def find_person(self, *, name: str = "", email: str = "", phone: str = "",
                    username: str = "") -> Optional[Person]:
        if email:
            p = self.store.find_person_by_identity("email", normalize_email(email))
            if p:
                return p
        if phone:
            digits = normalize_phone(phone)
            if digits:
                # match on suffix (country-code tolerance)
                for p in self.store.list_people(limit=5000):
                    for ident in p.identities:
                        if ident.kind == "phone" and ident.value:
                            if ident.value.endswith(digits) or digits.endswith(ident.value):
                                return p
        if username:
            p = self.store.find_person_by_identity("username", username.lower())
            if p:
                return p
        if name:
            n = _norm_name(name)
            if n:
                for p in self.store.list_people(limit=5000):
                    names = {_norm_name(p.display_name)}
                    names |= {_norm_name(a) for a in p.aliases}
                    if n in names:
                        return p
        return None

    def resolve_sender(self, label: str, source: SourceType,
                       extra_identities: Optional[dict[str, str]] = None
                       ) -> tuple[Optional[Person], bool]:
        """Resolve a message sender label to a Person.

        Returns (person_or_none, created_new). None means "this is the app
        user" or "unknown".
        """
        label = clean_text(label)
        if not label or self.is_user_label(label):
            return None, False
        if self.is_placeholder_label(label):
            # Keep the message, do not invent a contact for a deleted account.
            return None, False

        known = self.find_person(name=label)
        if known:
            return known, False

        extra_identities = extra_identities or {}
        email = extra_identities.get("email", "")
        phone = extra_identities.get("phone", "")
        username = extra_identities.get("username", "")
        known = self.find_person(name="", email=email, phone=phone, username=username)
        if known:
            self._add_alias(known, label)
            return known, False

        # Single-token labels ("Aravinth") may link to exactly one existing
        # person's first name. Multi-token labels require an exact match
        # above -- two different surnames are NEVER linked silently.
        tokens = _norm_name(label).split()
        if len(tokens) == 1 and len(tokens[0]) >= 3:
            first = tokens[0]
            candidates = []
            for p in self.store.list_people(limit=5000):
                base = _norm_name(p.display_name).split(" ")[0]
                alias_firsts = [_norm_name(a).split(" ")[0] for a in p.aliases]
                if base == first or first in alias_firsts:
                    candidates.append(p)
            if len(candidates) == 1:
                self._add_alias(candidates[0], label)
                return candidates[0], False
            # ambiguous: suggestion only, never silent merge
            if len(candidates) > 1:
                self._queue_suggestion(candidates[0], label, source,
                                       confidence=0.55,
                                       reason="first name matches an existing person")
        person = Person(
            display_name=label,
            identities=[IdentityLink(kind="name", value=_norm_name(label),
                                     source=source, label=label,
                                     origin=DataOrigin.IMPORTED)],
            origin=DataOrigin.IMPORTED,
        )
        created = self.store.insert_person(person)
        return created, True

    # ------------------------------------------------------------------
    def ingest_contacts(self, contacts: list[ParsedContact],
                        source: SourceType = SourceType.CONTACTS) -> int:
        created = 0
        for c in contacts:
            person = self.find_person(name=c.name, email=c.email, phone=c.phone)
            if person is None:
                person = Person(
                    display_name=c.name or c.email or c.phone or "Unknown",
                    origin=DataOrigin.IMPORTED,
                    identities=_contact_identities(c, source),
                )
                if c.organization:
                    person.aliases.append(c.organization)
                person = self.store.insert_person(person)
                created += 1
                continue
            # enrich existing person with new deterministic identities
            changed = False
            for link in _contact_identities(c, source):
                if not any(i.kind == link.kind and i.value == link.value
                           for i in person.identities):
                    person.identities.append(link)
                    changed = True
            if c.name and _norm_name(c.name) != _norm_name(person.display_name) \
                    and c.name not in person.aliases:
                person.aliases.append(c.name)
                changed = True
            if changed:
                self.store.update_person(person)
        return created

    def _add_alias(self, person: Person, alias: str) -> None:
        alias = clean_text(alias)
        if not alias or alias == person.display_name or alias in person.aliases:
            return
        if _norm_name(alias) == _norm_name(person.display_name):
            return
        person.aliases.append(alias)
        self.store.update_person(person)

    # ------------------------------------------------------------------
    # Suggestions (fuzzy / uncertain -- user confirms)
    # ------------------------------------------------------------------
    def _queue_suggestion(self, person: Person, label: str, source: SourceType,
                          confidence: float, reason: str) -> None:
        s_id = f"sug-{person.id}-{_norm_name(label).replace(' ', '-')}"
        self.store.upsert_identity_suggestion(s_id, {
            "person_a_id": person.id,
            "label": label,
            "source": source.value,
            "confidence": confidence,
            "reason": reason,
            "status": "pending",
            "detail": {"person_name": person.display_name,
                       "identity": label, "source": source.value},
        })

    def _queue_duplicate(self, keep: Person, drop: Person,
                         reason: str = "same name from different threads") -> str:
        """Queue two *existing* people that look like the same human.

        Unlike _queue_suggestion (a name seen for a person we have not met
        yet) this links two real records, so accepting it must merge them
        rather than just record an alias.
        """
        s_id = f"dup-{keep.id}-{drop.id}"
        self.store.upsert_identity_suggestion(s_id, {
            "person_a_id": keep.id,
            "person_b_id": drop.id,
            "label": drop.display_name,
            "source": "import",
            "confidence": 0.6,
            "reason": reason,
            "status": "pending",
            "detail": {"keep_id": keep.id,
                       "keep_name": keep.display_name,
                       "drop_id": drop.id,
                       "drop_name": drop.display_name},
        })
        return s_id

    def find_duplicate_people(self) -> list[tuple[Person, list[Person]]]:
        """Group people whose names are identical once punctuation and
        mojibake are normalised. Returns (keeper, duplicates) pairs; the
        keeper is the record with the most recorded interactions."""
        from circle.parsers.instagram import repair_mojibake

        def key(name: str) -> str:
            cleaned = repair_mojibake(name or "").lower()
            return re.sub(r"[^\w]", "", cleaned, flags=re.UNICODE)

        counts: dict[str, int] = self.store.event_counts_by_person()

        groups: dict[str, list[Person]] = defaultdict(list)
        for p in self.store.all_people_docs():
            if p.get("is_user"):
                continue
            k = key(p.get("display_name", ""))
            if k:
                groups[k].append(self.store.get_person(p["_id"]))

        out: list[tuple[Person, list[Person]]] = []
        for key_name, members in groups.items():
            if len(members) < 2:
                continue
            members.sort(key=lambda p: -counts.get(p.id, 0))
            out.append((members[0], members[1:]))
        return out

    def suggestion_evidence(self, doc: dict, limit: int = 4) -> dict:
        """Where two records disagree, in the archive itself.

        A bare "first name matches an existing person" tells the user nothing
        to judge. This returns both sides (who Circle already has, and the
        record or label that would be folded in), how much history each side
        has, and a few real messages from the conflicting side so the conflict
        can be seen rather than asserted.
        """
        def side(person: Optional[Person]) -> dict:
            if not person:
                return {}
            profile = self.store.get_profile(person.id)
            return {
                "id": person.id,
                "name": person.display_name,
                "aliases": list(person.aliases)[:5],
                "interactions": (profile.interaction_count
                                 if profile else 0),
                "last_seen": (profile.last_interaction_at.isoformat()
                              if profile and profile.last_interaction_at
                              else None),
            }

        existing = self.store.get_person(doc.get("person_a_id", ""))
        drop_id = doc.get("person_b_id")
        label = doc.get("label", "")

        if drop_id:
            kind = "duplicate"
            conflicting = self.store.get_person(drop_id)
            query: dict = {"person_id": drop_id}
        else:
            kind = "name_match"
            conflicting = None
            query = {"sender_label": {"$regex": f"^{re.escape(label)}$",
                                      "$options": "i"}}

        rows = self.store.search_messages(query, limit=limit)

        samples = []
        for row in rows:
            text = (row.get("content") or "").strip()
            if not text:
                continue
            conv = row.get("conversation_id") or ""
            sent = row.get("sent_at")
            # sent_at may be a datetime or an already-serialised string.
            at = (sent.isoformat() if hasattr(sent, "isoformat")
                  else (str(sent) if sent else None))
            samples.append({
                "text": text[:220],
                "at": at,
                "source": row.get("source") or "unknown",
                "conversation": conv.rsplit(":", 1)[-1] if conv else "",
                "sender_label": row.get("sender_label") or "",
            })

        existing_msgs = (self.store.count_messages_matching(
            {"person_id": doc.get("person_a_id")})
            if doc.get("person_a_id") else 0)
        return {
            "kind": kind,
            "existing": side(existing),
            "conflicting": side(conflicting) or {
                "name": label, "id": None, "aliases": [],
                "interactions": None, "last_seen": None},
            "counts": {
                "conflicting_messages":
                    self.store.count_messages_matching(query),
                "existing_messages": existing_msgs,
            },
            "samples": samples,
        }

    def list_suggestions(self) -> list[dict]:
        out = []
        for d in self.store.list_identity_suggestions(status="pending"):
            d["id"] = d.pop("_id")
            out.append(d)
        return out

    def accept_suggestion(self, suggestion_id: str, merge: bool) -> dict:
        doc = self.store.get_identity_suggestion(suggestion_id)
        if not doc:
            return {"ok": False, "error": "suggestion not found"}
        if not merge:
            self.store.set_identity_suggestion_status(suggestion_id, "rejected")
            return {"ok": True, "status": "rejected"}
        person = self.store.get_person(doc["person_a_id"])
        if not person:
            return {"ok": False, "error": "person not found"}
        # A duplicate suggestion links two real people: accepting it merges.
        drop_id = doc.get("person_b_id")
        if drop_id:
            result = self.merge_people(person.id, drop_id)
            self.store.set_identity_suggestion_status(
                suggestion_id, "accepted" if result.get("ok") else "pending")
            return {"ok": bool(result.get("ok")), "status": "merged",
                    "person_id": person.id, "merged": result}
        person.aliases = list(dict.fromkeys(
            person.aliases + [doc.get("label", "")]))
        self.store.update_person(person)
        self.store.set_identity_suggestion_status(suggestion_id, "accepted")
        return {"ok": True, "status": "accepted", "person_id": person.id}

    # ------------------------------------------------------------------
    def merge_people(self, keep_id: str, remove_id: str) -> dict:
        """Explicit user-initiated merge. Reassigns all records."""
        if keep_id == remove_id:
            return {"ok": False, "error": "cannot merge a person with itself"}
        keep = self.store.get_person(keep_id)
        remove = self.store.get_person(remove_id)
        if not keep or not remove:
            return {"ok": False, "error": "person not found"}

        keep.aliases = list(dict.fromkeys(
            keep.aliases + [remove.display_name, *remove.aliases]))
        for ident in remove.identities:
            if not any(i.kind == ident.kind and i.value == ident.value
                       for i in keep.identities):
                keep.identities.append(ident)
        keep.merged_from = list(dict.fromkeys(keep.merged_from + [remove_id]))
        self.store.update_person(keep)

        reassignments = {
            "messages": "person_id", "emails": "person_id",
            "notes": "person_id", "voice_recordings": "person_id",
            "documents": "person_id", "memories": "person_id",
            "relationship_events": "person_id",
        }
        moved = self.store.reassign_person_records(keep_id, remove_id, reassignments)
        self.store.replace_array_member("calendar_events", "person_ids",
                                        remove_id, keep_id)
        # Merge profiles: keep the richer interaction history by recompute
        self.store.delete_profile(remove_id)
        self.store.delete_person(remove_id)
        self.store.pull_array_member("conversations", "participant_ids",
                                      remove_id)
        return {"ok": True, "records_moved": moved, "person_id": keep_id}


def _contact_identities(c: ParsedContact, source: SourceType) -> list[IdentityLink]:
    links: list[IdentityLink] = []
    if c.name:
        links.append(IdentityLink(kind="name", value=_norm_name(c.name),
                                  source=source, label=c.name))
    if c.email:
        links.append(IdentityLink(kind="email", value=normalize_email(c.email),
                                  source=source, label=c.email))
    if c.phone:
        links.append(IdentityLink(kind="phone", value=normalize_phone(c.phone),
                                  source=source, label=c.phone))
    for kind, value in c.usernames:
        links.append(IdentityLink(kind=kind, value=value.lower(),
                                  source=source, label=value))
    return links

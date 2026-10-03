"""Contacts importer: CSV and VCF/vCard.

Contacts are a primary identity-resolution source (spec §13).
"""
from __future__ import annotations

import csv
import io
import re
from pathlib import Path
from typing import Any

from circle.parsers.common import ParseResult, clean_text, normalize_email, normalize_phone
from circle.parsers.common import ParsedContact


def parse_contacts(path: Path) -> ParseResult:
    ext = path.suffix.lower()
    result = ParseResult()
    if ext == ".vcf":
        _parse_vcf(path, result)
    elif ext == ".csv":
        _parse_csv(path, result)
    else:
        raise ValueError(f"unsupported contacts file type: {ext}")
    if not result.contacts:
        raise ValueError("no contacts extracted")
    return result


def _pick(row: dict, names: tuple[str, ...]) -> str:
    lowered = {k.lower().strip(): v for k, v in row.items()}
    for n in names:
        if n in lowered and lowered[n]:
            return str(lowered[n])
    return ""


def _parse_csv(path: Path, result: ParseResult) -> None:
    text = path.read_bytes().decode("utf-8", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    for row in reader:
        name = _pick(row, ("name", "full name", "fullname", "display name",
                           "contact name", "first name"))
        first = _pick(row, ("first name", "firstname", "given name"))
        last = _pick(row, ("last name", "lastname", "family name", "surname"))
        if not name and (first or last):
            name = f"{first} {last}".strip()
        phone = _pick(row, ("phone", "phone number", "mobile", "tel",
                            "telephone", "whatsapp", "cell"))
        email_addr = _pick(row, ("email", "e-mail", "email address", "mail"))
        org = _pick(row, ("organization", "organisation", "company", "org", "work"))
        if not (name or phone or email_addr):
            continue
        aliases = []
        nick = _pick(row, ("nickname", "nick", "alias"))
        if nick:
            aliases.append(clean_text(nick))
        usernames = []
        for key, kind in (("instagram", "instagram"), ("twitter", "twitter"),
                          ("x", "x"), ("telegram", "telegram"),
                          ("username", "username"), ("handle", "handle")):
            v = _pick(row, (key,))
            if v:
                usernames.append((kind, v.lstrip("@")))
        result.contacts.append(ParsedContact(
            name=clean_text(name),
            phone=normalize_phone(phone),
            email=normalize_email(email_addr),
            organization=clean_text(org),
            aliases=aliases,
            usernames=usernames,
        ))


def _parse_vcf(path: Path, result: ParseResult) -> None:
    try:
        import vobject
    except ImportError as e:  # pragma: no cover
        raise ValueError("vobject library not installed") from e

    raw = path.read_bytes().decode("utf-8", errors="replace")
    try:
        cards = list(vobject.readComponents(raw))
    except Exception as e:
        raise ValueError(f"invalid vCard: {e}") from e

    for card in cards:
        if getattr(card, "fn", None) is None and getattr(card, "n", None) is None:
            continue
        name = ""
        if getattr(card, "fn", None):
            name = clean_text(card.fn.value)
        elif getattr(card, "n", None):
            given, family = card.n.value[0], card.n.value[1]
            name = clean_text(f"{given} {family}")
        phones = [normalize_phone(t.value) for t in getattr(card, "tel_list", [])]
        emails = [normalize_email(e.value) for e in getattr(card, "email_list", [])]
        org = ""
        if getattr(card, "org", None):
            org_vals = card.org.value
            org = clean_text(org_vals[0] if isinstance(org_vals, list) else org_vals)
        aliases = [clean_text(a.value) for a in getattr(card, "nickname_list", [])]
        usernames = []
        for nick in getattr(card, "nickname_list", []):
            for part in str(nick.value).split(","):
                p = clean_text(part)
                if p:
                    aliases.append(p)
        if not (name or phones or emails):
            continue
        # store primary phone/email; extra ones ride along in aliases
        result.contacts.append(ParsedContact(
            name=name,
            phone=phones[0] if phones else "",
            email=emails[0] if emails else "",
            organization=org,
            aliases=[a for a in dict.fromkeys(aliases) if a and a != name],
            usernames=usernames,
        ))

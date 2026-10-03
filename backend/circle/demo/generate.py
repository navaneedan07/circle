"""Synthetic demo data generator (spec §46).

Writes clearly synthetic fixture files into the watched import folders so the
real ingestion pipeline processes them end-to-end (no pre-stuffed database),
plus a couple of local-only records (voice transcript, note).

NO real people's conversations are used anywhere.
"""
from __future__ import annotations

import base64
import json
import logging
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger("circle.demo")

DEMO_PEOPLE = [
    ("Aravinth Kumar", "aravinth@example.com", "+911234567890"),
    ("Hari Prashanth", "hari@example.com", "+911234567891"),
    ("Varnika Shah", "varnika@example.com", "+911234567892"),
]

WHATSAPP_TXT = """\
12/28/23, 8:42 PM - Aravinth Kumar: hey! are you coming to the SIH meeting tomorrow?
12/28/23, 8:43 PM - Me: yes, I will join at 6. Need to finish the ML model demo first
12/28/23, 8:44 PM - Aravinth Kumar: cool, I pushed the backend changes for SIH 26078
12/28/23, 8:45 PM - Aravinth Kumar: also can you share the internship docs when free?
12/29/23, 10:15 AM - Me: sent the docs. The hackathon timeline is next week
12/29/23, 10:16 AM - Aravinth Kumar: perfect. Lets sync at 5pm on the ML component
01/02/24, 9:30 PM - Aravinth Kumar: the dataset preprocessing is done
01/02/24, 9:31 PM - Me: great, I will train the classifier tonight
01/03/24, 7:10 PM - Aravinth Kumar: reminder: submit the SIH abstract by Friday
01/03/24, 7:12 PM - Me: noted, I promise I will submit it tomorrow
01/04/24, 3:05 PM - Aravinth Kumar: here is the architecture sketch IMG-20240104-WA0007.png (file attached)
"""

# A real (tiny) 1x1 PNG so the demo exercises the full media pipeline.
_TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8"
    "z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
_DEMO_MEDIA = {"IMG-20240104-WA0007.png": _TINY_PNG}

TELEGRAM_JSON = {
    "name": "Aravinth",
    "type": "personal_chat",
    "id": 1001,
    "messages": [
        {"id": 1, "type": "message", "date": "2024-01-04T11:20:00",
         "from": "Aravinth", "text": "ML model accuracy is at 91% now"},
        {"id": 2, "type": "message", "date": "2024-01-04T11:21:00",
         "from": "Me", "text": "nice! did you tune the learning rate?"},
        {"id": 3, "type": "message", "date": "2024-01-05T18:05:00",
         "from": "Aravinth",
         "text": [{"type": "text", "text": "Yes, and I added "},
                  {"type": "link", "text": "https://example.com/paper"},
                  {"type": "text", "text": " for the attention layer"}]},
        {"id": 4, "type": "message", "date": "2024-01-06T09:00:00",
         "from": "Aravinth", "text": "Internship interview with Amazon is on Tuesday"},
    ],
}

INSTAGRAM_JSON = {
    "participants": ["Aravinth", "Me"],
    "title": "Aravinth",
    "messages": [
        {"sender_name": "Aravinth", "timestamp_ms": 1704622800000,
         "content": "did you see the hackathon announcement?", "type": "Generic"},
        {"sender_name": "Me", "timestamp_ms": 1704622920000,
         "content": "yes! lets register as a team of 4", "type": "Generic"},
        {"sender_name": "Aravinth", "timestamp_ms": 1704630000000,
         "content": "done. topic is ML for the SIH problem statement",
         "type": "Generic"},
    ],
}

CALENDAR_ICS = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Circle Demo//EN
BEGIN:VEVENT
UID:demo-meet-1@circle.local
DTSTAMP:20240105T090000Z
DTSTART:20240108T160000Z
DTEND:20240108T170000Z
SUMMARY:SIH Project Sync
LOCATION:Lab 2
DESCRIPTION:Participants: Aravinth Kumar, Hari Prashanth, Varnika Shah\\nReview ML model progress
END:VEVENT
BEGIN:VEVENT
UID:demo-meet-2@circle.local
DTSTAMP:20240106T090000Z
DTSTART:20240110T110000Z
DTEND:20240110T113000Z
SUMMARY:Amazon Internship Interview Prep
DESCRIPTION:Participants: Aravinth Kumar
END:VEVENT
END:VCALENDAR
"""

CONTACTS_CSV = """name,email,phone,organization,nickname
Aravinth Kumar,aravinth@example.com,+911234567890,SIH Team,Arav
Hari Prashanth,hari@example.com,+911234567891,SIH Team,Harry
Varnika Shah,varnika@example.com,+911234567892,Design,Varni
"""

EMAIL_EML = """From: Aravinth Kumar <aravinth@example.com>
To: me@example.com
Subject: SIH abstract draft
Date: Fri, 05 Jan 2024 19:30:00 +0000
Content-Type: text/plain; charset="utf-8"

Hi,

I finished the abstract draft for SIH 26078. Please review the ML section
and the internship timeline we discussed. I will submit it tomorrow as promised.

Thanks,
Aravinth
"""

X_DM_JSON = [
    {"message_create": {
        "message_id": "1001",
        "senderId": "Aravinth Kumar",
        "createdAt": "2024-01-07T14:00:00.000Z",
        "message_data": {"text": "Hackathon registration closes tonight!"}}},
    {"message_create": {
        "message_id": "1002",
        "senderId": "me",
        "createdAt": "2024-01-07T14:02:00.000Z",
        "message_data": {"text": "Submitting our team now"}}},
]


def _write(root: Path, rel: str, content: str) -> str:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return str(path)


def _write_whatsapp_zip(root: Path, name: str, chat_text: str,
                        media: dict[str, bytes]) -> str:
    """Write a realistic WhatsApp export: _chat.txt plus Media/ attachments."""
    path = root / "whatsapp" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("_chat.txt", chat_text)
        for filename, data in media.items():
            zf.writestr(f"Media/{filename}", data)
    return str(path)


def generate_demo_data(ctx) -> dict:
    """Create synthetic files in watched folders; the watcher ingests them."""
    root = ctx.watcher.root
    written: list[str] = []

    written.append(_write_whatsapp_zip(root, "WhatsApp Chat with Aravinth Kumar.zip",
                                       WHATSAPP_TXT, _DEMO_MEDIA))
    written.append(_write(root, "telegram/telegram_aravinth.json",
                          json.dumps(TELEGRAM_JSON, indent=2)))
    written.append(_write(root, "instagram/instagram_aravinth.json",
                          json.dumps(INSTAGRAM_JSON, indent=2)))
    written.append(_write(root, "calendar/sih_meetings.ics", CALENDAR_ICS))
    written.append(_write(root, "contacts/contacts.csv", CONTACTS_CSV))
    written.append(_write(root, "email/aravinth_sih_abstract.eml", EMAIL_EML))
    written.append(_write(root, "x/direct-messages.json",
                          json.dumps(X_DM_JSON, indent=2)))

    # Local-only records (clearly synthetic)
    note_id = "note-demo-" + str(int(datetime.now(timezone.utc).timestamp()))
    from circle.domain.models import DataOrigin, Memory, Note, SourceType
    person = None
    for p in ctx.store.list_people(limit=100):
        if "aravinth" in p.display_name.lower():
            person = p
            break
    if person is None:
        from circle.domain.models import IdentityLink, Person
        person = ctx.store.insert_person(Person(
            display_name="Aravinth Kumar",
            identities=[IdentityLink(kind="email",
                                     value="aravinth@example.com",
                                     source=SourceType.CONTACTS,
                                     label="aravinth@example.com")]))

    now = datetime.now(timezone.utc)
    note = Note(id=note_id, person_id=person.id,
                title="SIH project context (demo)",
                body="Aravinth is working on the ML component of our SIH project. "
                     "I promised to submit the abstract tomorrow.",
                noted_at=now - timedelta(days=1),
                origin=DataOrigin.LOCAL, external_id=note_id)
    ctx.store.insert_note_if_new(note)
    mem = Memory(
        id="mem-" + note_id, person_id=person.id, kind="note",
        source=SourceType.NOTES, origin=DataOrigin.LOCAL,
        occurred_at=note.noted_at, text=f"{note.title}. {note.body}",
        record_id=note_id, source_id=note_id,
        citation=f"Note — {note.noted_at.strftime('%b %d, %Y')}",
        topics=["SIH", "Machine Learning", "Aravinth"],
    )
    try:
        mem.embedding = ctx.embedder.embed(mem.text)
        ctx.store.insert_memories([mem])
    except Exception as e:
        log.warning("demo note embedding skipped: %s", e)

    # Voice transcript demo (explicitly supplied synthetic recording metadata).
    # Marked as demo; no real audio, no real person's voice.
    voice_id = "voice-demo-aravinth"
    from circle.domain.models import VoiceRecording
    transcript = ("Discussed the SIH project and the ML model. Aravinth said the "
                  "classifier reached 91 percent accuracy and the internship "
                  "interview prep is on Tuesday.")
    rec = VoiceRecording(
        id=voice_id, person_id=person.id, filename="demo_voice_note.mp3",
        duration_seconds=48.0, recorded_at=now - timedelta(days=3),
        transcript=transcript, language="en", checksum="demo-voice-checksum",
        processing_status="COMPLETED", external_id=voice_id,
    )
    ctx.store.insert_voice_if_new(rec)
    vmem = Memory(
        id="mem-" + voice_id, person_id=person.id, kind="voice",
        source=SourceType.VOICE, origin=DataOrigin.IMPORTED,
        occurred_at=rec.recorded_at,
        text=f"[Voice recording demo_voice_note.mp3] {transcript}",
        record_id=voice_id, source_id="demo-voice-checksum",
        citation=f"🎙️ demo_voice_note.mp3 — "
                 f"{rec.recorded_at.strftime('%b %d, %Y')}",
        topics=["SIH", "Machine Learning", "internship"],
    )
    try:
        vmem.embedding = ctx.embedder.embed(vmem.text)
        ctx.store.insert_memories([vmem])
    except Exception as e:
        log.warning("demo voice embedding skipped: %s", e)

    from circle.relationship.metrics import record_event
    record_event(ctx.store, person_id=person.id, kind="voice",
                 source=SourceType.VOICE.value, occurred_at=rec.recorded_at,
                 summary=transcript[:80], record_id=voice_id)

    ctx.store.set_setting("demo_generated", True)
    ctx.pipeline.refresh_profiles()
    ctx.broker.publish("demo", {"generated": True, "files": len(written)})
    return {"generated": True, "files": written,
            "note": "Demo data is synthetic and clearly marked in the UI."}

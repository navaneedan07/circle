"""Parser tests: email (.eml/.mbox/.csv), calendar (.ics/.csv), contacts
(.csv/.vcf), documents."""
from __future__ import annotations

from pathlib import Path

import pytest

from circle.parsers import calendar as calendar_parser
from circle.parsers import contacts as contacts_parser
from circle.parsers import documents as documents_parser
from circle.parsers import email_parser


def _write(tmp_path: Path, name: str, content: str | bytes) -> Path:
    p = tmp_path / name
    if isinstance(content, bytes):
        p.write_bytes(content)
    else:
        p.write_text(content, encoding="utf-8")
    return p


EML = b"""From: Alice <alice@example.com>
To: me@example.com
Subject: =?utf-8?q?Meeting_notes_=E2=80=94_SIH?=
Date: Fri, 05 Jan 2024 19:30:00 +0000
MIME-Version: 1.0
Content-Type: text/plain; charset="utf-8"

Hi,

Here are the notes from today.
We agreed on the ML plan.

Regards,
Alice
"""


class TestEmail:
    def test_eml_parse(self, tmp_path):
        f = _write(tmp_path, "mail.eml", EML)
        res = email_parser.parse_email(f)
        assert len(res.emails) == 1
        e = res.emails[0]
        assert e.from_address == "alice@example.com"
        assert "Meeting notes" in e.subject
        assert e.sent_at is not None and e.sent_at.year == 2024
        assert "ML plan" in e.body

    def test_mbox_parse(self, tmp_path):
        mbox = (b"From alice@example.com Fri Jan  5 19:30:00 2024\n"
                b"From: alice@example.com\nTo: me@example.com\n"
                b"Subject: First\nDate: Fri, 05 Jan 2024 19:30:00 +0000\n"
                b"Content-Type: text/plain\n\nBody one.\n"
                b"\n"
                b"From bob@example.com Sat Jan  6 10:00:00 2024\n"
                b"From: bob@example.com\nSubject: Second\n"
                b"Date: Sat, 06 Jan 2024 10:00:00 +0000\n\nBody two.\n")
        f = _write(tmp_path, "mail.mbox", mbox)
        res = email_parser.parse_email(f)
        assert len(res.emails) == 2
        assert res.emails[0].subject == "First"

    def test_csv_emails(self, tmp_path):
        f = _write(tmp_path, "emails.csv",
                   "from,subject,date,body\n"
                   "alice@example.com,Hello,2024-01-01T10:00:00Z,Test body\n")
        res = email_parser.parse_email(f)
        assert len(res.emails) == 1
        assert res.emails[0].from_address == "alice@example.com"

    def test_garbage_raises(self, tmp_path):
        f = _write(tmp_path, "x.eml", "")
        with pytest.raises(ValueError):
            email_parser.parse_email(f)


class TestCalendar:
    def test_ics_parse(self, tmp_path):
        ics = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:1@x
DTSTAMP:20240105T090000Z
DTSTART:20240108T160000Z
DTEND:20240108T170000Z
SUMMARY:SIH Project Sync
LOCATION:Lab 2
DESCRIPTION:Participants: Aravinth, Hari, Varnika
ATTENDEE;CN=Aravinth:mailto:aravinth@example.com
END:VEVENT
END:VCALENDAR
"""
        f = _write(tmp_path, "cal.ics", ics)
        res = calendar_parser.parse_calendar(f)
        assert len(res.events) == 1
        ev = res.events[0]
        assert ev.title == "SIH Project Sync"
        assert ev.starts_at is not None
        assert any("Aravinth" in p for p in ev.participants)
        assert ev.location == "Lab 2"

    def test_participants_from_description_stop_at_newline(self, tmp_path):
        """Regression: collapsing newlines once swallowed the next line and
        created a junk person like 'Varnika Shah Review ML model progress'."""
        ics = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:2@x
DTSTAMP:20240105T090000Z
DTSTART:20240108T160000Z
SUMMARY:SIH Project Sync
LOCATION:Lab 2
DESCRIPTION:Participants: Aravinth Kumar, Hari Prashanth, Varnika Shah\\nReview ML model progress
END:VEVENT
END:VCALENDAR
"""
        f = _write(tmp_path, "cal2.ics", ics)
        res = calendar_parser.parse_calendar(f)
        parts = res.events[0].participants
        assert parts == ["Aravinth Kumar", "Hari Prashanth", "Varnika Shah"]
        assert not any("progress" in p.lower() for p in parts)

    def test_ics_bad_raises(self, tmp_path):
        f = _write(tmp_path, "bad.ics", "not an ics at all")
        with pytest.raises(ValueError):
            calendar_parser.parse_calendar(f)

    def test_csv_events(self, tmp_path):
        f = _write(tmp_path, "cal.csv",
                   "title,start,end,participants,location\n"
                   "Standup,2024-01-02T10:00:00Z,2024-01-02T10:30:00Z,"
                   "Alice; Bob,Room 1\n")
        res = calendar_parser.parse_calendar(f)
        assert len(res.events) == 1
        assert len(res.events[0].participants) == 2


class TestContacts:
    def test_csv_contacts(self, tmp_path):
        f = _write(tmp_path, "contacts.csv",
                   "name,email,phone,organization\n"
                   "Aravinth Kumar,aravinth@example.com,+911234567890,SIH\n")
        res = contacts_parser.parse_contacts(f)
        assert len(res.contacts) == 1
        c = res.contacts[0]
        assert c.email == "aravinth@example.com"
        assert c.phone == "911234567890"

    def test_vcf_contacts(self, tmp_path):
        vcf = """BEGIN:VCARD
VERSION:3.0
FN:Alice Wonder
ORG:ACME
TEL;TYPE=CELL:+1 555 0100
EMAIL;TYPE=INTERNET:alice@example.com
NICKNAME:Ali
END:VCARD
"""
        f = _write(tmp_path, "contacts.vcf", vcf)
        res = contacts_parser.parse_contacts(f)
        assert len(res.contacts) == 1
        c = res.contacts[0]
        assert c.name == "Alice Wonder"
        assert c.email == "alice@example.com"
        assert c.phone.endswith("5550100")
        assert c.organization == "ACME"

    def test_unsupported_raises(self, tmp_path):
        f = _write(tmp_path, "c.xml", "<x/>")
        with pytest.raises(ValueError):
            contacts_parser.parse_contacts(f)


class TestDocuments:
    def test_txt(self, tmp_path):
        f = _write(tmp_path, "doc.txt", "Project notes\nDetails about SIH work.")
        title, text = documents_parser.extract_text(f)
        assert "SIH" in text
        assert title == "Project notes"

    def test_html(self, tmp_path):
        f = _write(tmp_path, "doc.html",
                   "<html><head><title>Plan</title></head>"
                   "<body><p>Quarterly plan for the team</p>"
                   "<script>alert(1)</script></body></html>")
        title, text = documents_parser.extract_text(f)
        assert title == "Plan"
        assert "Quarterly plan" in text
        assert "alert" not in text

    def test_pdf(self, tmp_path):
        from pypdf import PdfWriter
        w = PdfWriter()
        w.add_blank_page(width=200, height=200)
        p = tmp_path / "blank.pdf"
        with p.open("wb") as fh:
            w.write(fh)
        # blank PDF has no extractable text -> must raise, not crash
        with pytest.raises(ValueError):
            documents_parser.extract_text(p)

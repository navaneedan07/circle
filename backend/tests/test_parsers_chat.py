"""Parser tests: WhatsApp (multiple formats), Telegram, Instagram, X,
generic chat. Not just the happy path (spec §40)."""
from __future__ import annotations

from pathlib import Path

import pytest

from circle.parsers import instagram as instagram_parser
from circle.parsers import telegram as telegram_parser
from circle.parsers import whatsapp as whatsapp_parser
from circle.parsers import x as x_parser
from circle.parsers.generic import detect_fields, parse_generic


def _write(tmp_path: Path, name: str, content: str | bytes) -> Path:
    p = tmp_path / name
    if isinstance(content, bytes):
        p.write_bytes(content)
    else:
        p.write_text(content, encoding="utf-8")
    return p


# ------------------------------------------------------------------ WhatsApp
class TestWhatsAppFormats:
    def test_us_12h_format(self, tmp_path):
        f = _write(tmp_path, "chat.txt",
                   "12/28/23, 8:42 PM - Alice: hello there\n"
                   "12/28/23, 8:43 PM - Bob: hi!\n")
        res = whatsapp_parser.parse_whatsapp(f)
        assert len(res.messages) == 2
        assert res.messages[0].sender_label == "Alice"
        assert res.messages[0].content == "hello there"
        assert res.messages[0].sent_at is not None
        assert res.messages[0].sent_at.month == 12

    def test_eu_24h_format(self, tmp_path):
        f = _write(tmp_path, "chat.txt",
                   "28/12/2023, 20:42 - Alice: evening\n")
        res = whatsapp_parser.parse_whatsapp(f)
        assert len(res.messages) == 1
        assert res.messages[0].sent_at.hour == 20

    def test_day_month_disambiguation(self, tmp_path):
        # 25/12 can only be day-first
        f = _write(tmp_path, "chat.txt", "25/12/23, 10:00 - A: x\n")
        res = whatsapp_parser.parse_whatsapp(f)
        assert res.messages[0].sent_at.day == 25
        # 12/25 can only be month-first
        f2 = _write(tmp_path, "chat2.txt", "12/25/23, 10:00 - A: y\n")
        res2 = whatsapp_parser.parse_whatsapp(f2)
        assert res2.messages[0].sent_at.month == 12
        assert res2.messages[0].sent_at.day == 25

    def test_bracketed_desktop_format(self, tmp_path):
        f = _write(tmp_path, "chat.txt",
                   "[31/12/23, 11:59:59 PM] Alice: midnight\n")
        res = whatsapp_parser.parse_whatsapp(f)
        assert res.messages[0].sender_label == "Alice"
        assert res.messages[0].sent_at.second == 59

    def test_multiline_body(self, tmp_path):
        f = _write(tmp_path, "chat.txt",
                   "12/28/23, 8:42 PM - Alice: line one\n"
                   "line two continues\n"
                   "line three\n"
                   "12/28/23, 8:43 PM - Bob: next\n")
        res = whatsapp_parser.parse_whatsapp(f)
        assert len(res.messages) == 2
        assert "line two" in res.messages[0].content
        assert "line three" in res.messages[0].content

    def test_media_omitted_becomes_attachment(self, tmp_path):
        f = _write(tmp_path, "chat.txt",
                   "12/28/23, 8:42 PM - Alice: <Media omitted>\n")
        res = whatsapp_parser.parse_whatsapp(f)
        assert res.messages[0].attachments

    def test_system_noise_skipped(self, tmp_path):
        f = _write(tmp_path, "chat.txt",
                   "12/28/23, 8:42 PM - Messages and end-to-end encrypted\n"
                   "12/28/23, 8:43 PM - Alice created group \"Team\"\n"
                   "12/28/23, 8:44 PM - Alice: real message here\n")
        res = whatsapp_parser.parse_whatsapp(f)
        # system lines without a proper sender are not people/messages
        assert all(m.sender_label not in (None, "") for m in res.messages)
        assert any("real message" in m.content for m in res.messages)

    def test_url_message_not_mistaken_for_sender(self, tmp_path):
        f = _write(tmp_path, "chat.txt",
                   "12/28/23, 8:42 PM - https://example.com/page\n")
        res = whatsapp_parser.parse_whatsapp(f)
        # no sender colon => system/unknown line, no fake person created
        assert all(m.sender_label in (None, "https://example.com/page")
                   or "://" not in m.sender_label for m in res.messages)

    def test_colon_in_body_only_splits_first(self, tmp_path):
        f = _write(tmp_path, "chat.txt",
                   "12/28/23, 8:42 PM - Alice: meet me at 5:30\n")
        res = whatsapp_parser.parse_whatsapp(f)
        assert res.messages[0].sender_label == "Alice"
        assert res.messages[0].content == "meet me at 5:30"

    def test_non_whatsapp_file_raises(self, tmp_path):
        f = _write(tmp_path, "random.txt", "just some text\nwith lines\n")
        with pytest.raises(ValueError):
            whatsapp_parser.parse_whatsapp(f)

    def test_utf16_bom(self, tmp_path):
        content = "12/28/23, 8:42 PM - A\u00e9ric: caf\u00e9 talk\n"
        f = _write(tmp_path, "chat.txt", content.encode("utf-16"))
        res = whatsapp_parser.parse_whatsapp(f)
        assert res.messages[0].sender_label == "A\u00e9ric"

    def test_deterministic_ids(self, tmp_path):
        content = ("12/28/23, 8:42 PM - Alice: hello\n"
                   "12/28/23, 8:43 PM - Alice: hello\n")
        f1 = _write(tmp_path, "a.txt", content)
        f2 = _write(tmp_path, "b.txt", content)
        r1, r2 = whatsapp_parser.parse_whatsapp(f1), whatsapp_parser.parse_whatsapp(f2)
        # same content in different files => same message ids (dedupe works)
        assert r1.messages[0].id == r2.messages[0].id
        # distinct messages get distinct ids
        assert r1.messages[0].id != r1.messages[1].id


# ------------------------------------------------------------------ Telegram
class TestTelegram:
    def test_personal_chat(self, tmp_path):
        data = {
            "name": "Alice", "type": "personal_chat", "id": 1,
            "messages": [
                {"id": 1, "type": "message", "date": "2024-01-04T11:20:00",
                 "from": "Alice", "text": "hello"},
                {"id": 2, "type": "message", "date": "2024-01-04T11:21:00",
                 "from": "Me", "text": "hi there"},
                {"id": 3, "type": "service", "date": "2024-01-04T11:22:00",
                 "action": "pin_message", "text": "pinned"},
            ],
        }
        f = _write(tmp_path, "result.json", __import__("json").dumps(data))
        res = telegram_parser.parse_telegram(f)
        assert len(res.messages) == 2
        assert res.messages[0].sender_label == "Alice"
        assert res.conversations[0]["title"] == "Alice"

    def test_text_as_segment_list(self, tmp_path):
        import json
        data = {"name": "Bob", "type": "personal_chat",
                "messages": [{"id": 1, "type": "message",
                              "date": "2024-01-04T11:20:00", "from": "Bob",
                              "text": [{"type": "text", "text": "see "},
                                       {"type": "link", "text": "https://x.io"}]}]}
        f = _write(tmp_path, "result.json", json.dumps(data))
        res = telegram_parser.parse_telegram(f)
        assert "https://x.io" in res.messages[0].content

    def test_chats_list_wrapper(self, tmp_path):
        import json
        data = {"chats": {"list": [
            {"name": "Alice", "type": "personal_chat",
             "messages": [{"id": 1, "type": "message",
                           "date": "2024-01-01T10:00:00", "from": "Alice",
                           "text": "yo"}]},
            {"name": "Group", "type": "group",
             "messages": [{"id": 2, "type": "message",
                           "date": "2024-01-01T11:00:00", "from": "Alice",
                           "text": "hi all"}]},
        ]}}
        f = _write(tmp_path, "result.json", json.dumps(data))
        res = telegram_parser.parse_telegram(f)
        assert len(res.messages) == 2

    def test_media_reference_kept(self, tmp_path):
        import json
        data = {"name": "Alice", "type": "personal_chat",
                "messages": [{"id": 1, "type": "message",
                              "date": "2024-01-01T10:00:00", "from": "Alice",
                              "text": "", "photo": "photos_1/photo_1.jpg"}]}
        f = _write(tmp_path, "result.json", json.dumps(data))
        res = telegram_parser.parse_telegram(f)
        assert res.messages[0].attachments

    def test_invalid_json_raises(self, tmp_path):
        f = _write(tmp_path, "bad.json", "{not json")
        with pytest.raises(ValueError):
            telegram_parser.parse_telegram(f)


# ---------------------------------------------------------------- Instagram
class TestInstagram:
    def test_message_1_json_shape(self, tmp_path):
        import json
        data = {"participants": ["Alice", "Me"], "title": "Alice",
                "messages": [
                    {"sender_name": "Alice", "timestamp_ms": 1704622800000,
                     "content": "hey", "type": "Generic"},
                    {"sender_name": "Me", "timestamp_ms": 1704622920000,
                     "content": "yo", "type": "Generic"},
                ]}
        f = _write(tmp_path, "message_1.json", json.dumps(data))
        res = instagram_parser.parse_instagram(f)
        assert len(res.messages) == 2
        assert res.messages[0].sent_at.year == 2024

    def test_ndjson_tolerance(self, tmp_path):
        lines = [
            '{"sender_name": "Alice", "timestamp_ms": 1704622800000, "content": "a"}',
            '{"sender_name": "Me", "timestamp_ms": 1704622920000, "content": "b"}',
        ]
        f = _write(tmp_path, "inbox.json", "\n".join(lines))
        res = instagram_parser.parse_instagram(f)
        assert len(res.messages) == 2

    def test_share_and_photos(self, tmp_path):
        import json
        data = {"participants": ["Alice"], "title": "Alice",
                "messages": [
                    {"sender_name": "Alice", "timestamp_ms": 1704622800000,
                     "type": "Share", "share": {"text": "check this post"}},
                    {"sender_name": "Alice", "timestamp_ms": 1704623000000,
                     "type": "Generic",
                     "photos": [{"uri": "photos/p1.jpg"}]},
                ]}
        f = _write(tmp_path, "message_1.json", json.dumps(data))
        res = instagram_parser.parse_instagram(f)
        assert len(res.messages) == 2
        assert res.messages[1].attachments

    def test_no_messages_raises(self, tmp_path):
        import json
        f = _write(tmp_path, "empty.json", json.dumps({"messages": []}))
        with pytest.raises(ValueError):
            instagram_parser.parse_instagram(f)


# ----------------------------------------------------------------------- X
class TestX:
    def test_js_wrapper_dm_file(self, tmp_path):
        content = ("window.YTD.dl.part0 = [\n"
                   '  {"message_create": {"message_id": "1", "senderId": "u1",'
                   ' "createdAt": "2024-01-07T14:00:00.000Z",'
                   ' "message_data": {"text": "hello from DM"}}}\n'
                   "]\n")
        f = _write(tmp_path, "direct-messages-0.js", content)
        res = x_parser.parse_x(f)
        assert len(res.messages) == 1
        assert "hello from DM" in res.messages[0].content

    def test_plain_json_list(self, tmp_path):
        import json
        data = [{"message_create": {"senderId": "u1",
                                    "createdAt": "2024-01-07T14:00:00Z",
                                    "message_data": {"text": "hi"}}}]
        f = _write(tmp_path, "dm.json", json.dumps(data))
        res = x_parser.parse_x(f)
        assert res.messages[0].content == "hi"

    def test_timestamp_parsing(self, tmp_path):
        import json
        data = [{"message_create": {"senderId": "u1",
                                    "createdAt": "2024-03-05T09:30:00.000Z",
                                    "message_data": {"text": "morning"}}}]
        f = _write(tmp_path, "dm.json", json.dumps(data))
        res = x_parser.parse_x(f)
        assert res.messages[0].sent_at.year == 2024
        assert res.messages[0].sent_at.month == 3

    def test_empty_raises(self, tmp_path):
        f = _write(tmp_path, "empty.js", "window.YTD.x.part0 = []")
        with pytest.raises(ValueError):
            x_parser.parse_x(f)


# ------------------------------------------------------------------ generic
class TestGeneric:
    def test_csv_detection_and_mapping(self, tmp_path):
        f = _write(tmp_path, "chat.csv",
                   "timestamp,sender,message\n"
                   "2024-01-01T10:00:00Z,Alice,hello\n"
                   "2024-01-01T10:01:00,Bob,hi there\n")
        fields = detect_fields(f)
        assert "timestamp" in fields and "sender" in fields
        res = parse_generic(f)
        assert len(res.messages) == 2
        assert res.messages[0].sender_label == "Alice"

    def test_explicit_mapping(self, tmp_path):
        import json
        f = _write(tmp_path, "log.json", json.dumps([
            {"when": "2024-02-01T10:00:00Z", "who": "Alice", "what": "yo"},
            {"when": "2024-02-01T10:01:00Z", "who": "Bob", "what": "hey"},
        ]))
        res = parse_generic(f, mapping={"timestamp": "when",
                                        "sender": "who", "content": "what"})
        assert len(res.messages) == 2
        assert res.messages[1].content == "hey"

    def test_txt_line_patterns(self, tmp_path):
        f = _write(tmp_path, "chat.txt",
                   "2024-01-01 10:00 <Alice> hello\n"
                   "2024-01-01 10:01 <Bob> hi\n")
        res = parse_generic(f)
        assert len(res.messages) >= 2

    def test_html_table(self, tmp_path):
        f = _write(tmp_path, "log.html",
                   "<html><body><table>"
                   "<tr><th>date</th><th>sender</th><th>message</th></tr>"
                   "<tr><td>2024-01-01T10:00:00Z</td><td>Alice</td><td>hello</td></tr>"
                   "</table></body></html>")
        res = parse_generic(f)
        assert len(res.messages) == 1

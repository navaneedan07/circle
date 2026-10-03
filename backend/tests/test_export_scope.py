"""Instagram/Meta data exports: only the conversation tree is worth reading.

A real export is thousands of files of account settings, ad interests, login
history and posted photos, against a few hundred message threads. Reading all
of it invents people and junk memories out of profile JSON, and stores every
photo you have ever posted as an unlinked media attachment.

The paths below are the real shapes taken from an actual export folder.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from circle.parsers import export_scope
from circle.parsers.instagram import parse_instagram, repair_mojibake

EXPORT = "instagram-i_naveen_31-2026-10-02-ii6xn26L"
THREAD = "17899382594737747"


def _p(rest: str) -> Path:
    return Path(f"G:/My Drive/circle/meta-2026-Oct-02-05-25-53/{EXPORT}"
                f"/your_instagram_activity/{rest}")


class TestIsDataExport:
    def test_export_folder_is_recognised(self):
        assert export_scope.is_data_export(_p("media/0.jpg"))

    def test_meta_prefixed_folder_is_recognised(self):
        assert export_scope.is_data_export(
            Path("G:/My Drive/circle/meta-2026-Oct-02-05-25-53/x.json"))

    def test_whatsapp_export_is_not(self):
        assert not export_scope.is_data_export(
            Path("G:/My Drive/circle/WhatsApp Chat with Hitesh.zip"))

    def test_plain_chat_folder_is_not(self):
        assert not export_scope.is_data_export(
            Path("C:/Circle/imports/whatsapp/chat.txt"))

    def test_a_folder_simply_called_meta_is_not_an_export(self):
        """'meta' alone must not trigger the filter: only instagram-* or a
        known export directory name counts."""
        assert not export_scope.is_data_export(
            Path("G:/My Drive/circle/meta/notes.txt"))


class TestSkipReason:
    @pytest.mark.parametrize("rest", [
        "connections/following.json",
        "connections/followers.json",
        "ads_information/ads_about_meta.json",
        "ads_information/ad_preferences.json",
        "personal_information/personal_information.json",
        "preferences/account_settings.json",
        "security_and_login_information/login_history.json",
        "logged_information/login_activity.json",
        "media/12345.jpg",
        "start_here.html",
        "your_instagram_activity/comments/comments.json",
        "your_instagram_activity/likes/likes.json",
        "your_instagram_activity/posts/posts.json",
        "monetization/payouts.json",
    ])
    def test_export_noise_is_skipped(self, rest):
        assert export_scope.skip_reason(_p(rest)) is not None

    @pytest.mark.parametrize("rest", [
        f"messages/inbox/{THREAD}/message_1.json",
        f"messages/inbox/{THREAD}/photos/242406108400553.jpg",
        f"messages/inbox/{THREAD}/videos/12345.mp4",
    ])
    def test_conversation_tree_is_kept(self, rest):
        assert export_scope.skip_reason(_p(rest)) is None

    def test_files_outside_an_export_are_never_skipped(self):
        for p in (Path("C:/Circle/imports/whatever/connections.json"),
                  Path("G:/My Drive/circle/WhatsApp Chat with X.zip"),
                  Path("C:/Downloads/random.json")):
            assert export_scope.skip_reason(p) is None

    def test_ai_conversations_is_not_a_conversation(self):
        assert export_scope.skip_reason(
            _p("messages/ai_conversations.json")) is not None

    def test_message_html_is_kept_so_it_can_be_reported(self):
        """Newer exports ship message_1.html. We cannot parse it yet, but it
        must not be silently discarded as if it were account noise."""
        assert export_scope.skip_reason(
            _p(f"messages/inbox/{THREAD}/message_1.html")) is None

    def test_thread_level_loose_file_is_skipped(self):
        assert export_scope.skip_reason(
            _p(f"messages/inbox/{THREAD}/profile.json")) is not None


class TestConversationFiles:
    def test_only_conversation_files_are_listed(self, tmp_path):
        root = tmp_path / f"meta-2026" / EXPORT
        keep = root / "your_instagram_activity" / "messages" / "inbox" / THREAD
        keep.mkdir(parents=True)
        (keep / "message_1.json").write_text("{}", encoding="utf-8")
        (keep / "photos").mkdir()
        (keep / "photos" / "a.jpg").write_bytes(b"\xff\xd8\xff")
        (root / "media").mkdir()
        (root / "media" / "b.jpg").write_bytes(b"\xff\xd8\xff")
        (root / "connections").mkdir()
        (root / "connections" / "c.json").write_text("{}", encoding="utf-8")

        files = {p.name for p in export_scope.conversation_files(tmp_path / "meta-2026")}
        assert files == {"message_1.json", "a.jpg"}


class TestInstagramAttachments:
    """Regression: attachments carried only a label, so the media linker never
    matched them and every Instagram photo stayed unlinked."""

    def _export_file(self, tmp_path, message: dict) -> Path:
        return self._export_messages(tmp_path, [message])

    def _export_messages(self, tmp_path, messages: list[dict]) -> Path:
        d = tmp_path / "instagram-abc" / "messages" / "inbox" / THREAD
        d.mkdir(parents=True)
        p = d / "message_1.json"
        p.write_text(json.dumps({
            "participants": [{"name": "Naveen"}, {"name": "Esther"}],
            "title": "Esther", "messages": messages,
        }), encoding="utf-8")
        return p

    def test_photo_uri_becomes_a_linkable_attachment(self, tmp_path):
        p = self._export_file(tmp_path, {
            "sender_name": "Esther", "timestamp_ms": 1728975357343,
            "content": "look",
            "photos": [{"uri": f"your_instagram_activity/messages/inbox/"
                                f"{THREAD}/photos/242406108400553.jpg",
                        "creation_timestamp": 1685174855}],
        })
        res = parse_instagram(p)
        msg = res.messages[0]
        assert msg.attachments, "attachment was dropped"
        att = msg.attachments[0]
        assert att["filename"] == "242406108400553.jpg"
        assert att["filename_key"] == "242406108400553.jpg"
        assert att["kind"] == "image"

    def test_all_media_fields_are_read(self, tmp_path):
        """The live export uses photos/videos/audio/gifs, not audio_files."""
        p = self._export_file(tmp_path, {
            "sender_name": "Esther", "timestamp_ms": 1728975357343, "content": "x",
            "photos": [{"uri": "a/inbox/t/photos/1.jpg"}],
            "videos": [{"uri": "a/inbox/t/videos/2.mp4"}],
            "audio": [{"uri": "a/inbox/t/audio/3.m4a"}],
            "gifs": [{"uri": "a/inbox/t/gifs/4.gif"}],
        })
        names = {a["filename"] for a in parse_instagram(p).messages[0].attachments}
        assert names == {"1.jpg", "2.mp4", "3.m4a", "4.gif"}

    def test_photo_only_messages_get_distinct_ids(self, tmp_path):
        """Two different photos sent at the same instant must not collapse."""
        p = self._export_messages(tmp_path, [
            {"sender_name": "Esther", "timestamp_ms": 1728975357343,
             "photos": [{"uri": "a/inbox/t/photos/1.jpg"}]},
            {"sender_name": "Esther", "timestamp_ms": 1728975357343,
             "photos": [{"uri": "a/inbox/t/photos/2.jpg"}]},
        ])
        msgs = parse_instagram(p).messages
        assert len(msgs) == 2
        assert len({m.id for m in msgs}) == 2, \
            "photo-only messages collapsed into one"

    def test_duplicate_uris_are_deduped(self, tmp_path):
        p = self._export_file(tmp_path, {
            "sender_name": "Esther", "timestamp_ms": 1728975357343, "content": "x",
            "photos": [{"uri": "a/inbox/t/photos/1.jpg"},
                       {"uri": "a/inbox/t/photos/1.jpg"}],
        })
        assert len(parse_instagram(p).messages[0].attachments) == 1


class TestMojibakeRepair:
    """Meta writes parts of an export as UTF-8 decoded as Latin-1.

    Left alone, 'bro \U0001f31d\U0001f31d' is stored as mojibake and becomes
    unsearchable, and the sender name becomes a different person.
    """

    def test_emoji_round_trip_is_repaired(self):
        broken = "Online la bro \u00f0\u009f\u008c\u009d\u00f0\u009f\u008c\u009d"
        assert repair_mojibake(broken) == "Online la bro \U0001f31d\U0001f31d"

    def test_plain_text_is_untouched(self):
        for s in ("Ok bhai", "190 + 25 tax", "hello world", ""):
            assert repair_mojibake(s) == s

    def test_real_latin1_names_are_not_mangled(self):
        # 'ø' alone is not valid UTF-8, so the round trip must fail cleanly.
        assert repair_mojibake("Søren") == "Søren"
        assert repair_mojibake("Renée") == "Renée"

    def test_sender_name_is_repaired_in_a_parsed_export(self, tmp_path):
        d = tmp_path / "instagram-abc" / "messages" / "inbox" / THREAD
        d.mkdir(parents=True)
        p = d / "message_1.json"
        p.write_text(json.dumps({
            "participants": [{"name": "Naveen"}],
            "title": "Naveen",
            "messages": [{"sender_name": "Kavi\u00e2\u0099\u00a1",
                          "timestamp_ms": 1728975357343,
                          "content": "hello \u00f0\u009f\u008c\u009d"}],
        }), encoding="utf-8")
        msg = parse_instagram(p).messages[0]
        # 'â\x99¡' is UTF-8 for U+2661; the export stored it as Latin-1.
        assert msg.sender_label == "Kavi\u2661"
        assert msg.content == "hello \U0001f31d"


class TestPlaceholderSenders:
    """Deleted/anonymous accounts must not become contacts."""

    @pytest.mark.parametrize("label", [
        "Instagram user", "instagram user", "Instagram User",
        "~", "", "   ", "\U0001f600", "\U0001f600\U0001f600",
    ])
    def test_placeholder_labels_are_recognised(self, resolver, label):
        assert resolver.is_placeholder_label(label)

    @pytest.mark.parametrize("label", [
        "Ashwinee", "~Jaishree", "Kaviyasrikanth", "Esther__12", "Naveen",
    ])
    def test_real_names_are_not_placeholders(self, resolver, label):
        assert not resolver.is_placeholder_label(label)

    def test_deleted_account_does_not_create_a_person(self, resolver, clean_store):
        from circle.domain.models import SourceType
        person, created = resolver.resolve_sender("Instagram user",
                                                  SourceType.INSTAGRAM)
        assert person is None
        assert created is False
        assert clean_store.count_people() == 0

    def test_real_name_still_creates_a_person(self, resolver, clean_store):
        from circle.domain.models import SourceType
        person, created = resolver.resolve_sender("Ashwinee", SourceType.INSTAGRAM)
        assert created is True
        assert person.display_name == "Ashwinee"


class TestPipelineSkipsExportNoise:
    def test_noise_file_is_skipped_without_creating_a_job(self, pipeline,
                                                          tmp_path):
        root = tmp_path / "managed"
        export = root / "instagram-abc"
        (export / "connections").mkdir(parents=True)
        noise = export / "connections" / "followers.json"
        noise.write_text(json.dumps({
            "data": [{"username": "stranger", "full_name": "A Stranger"}]}),
            encoding="utf-8")

        result = pipeline.process_path(noise)
        assert result.status == "SKIPPED"
        assert pipeline.store.count_people() == 0
        assert noise.exists(), "noise file must not be moved"

    def test_message_file_in_the_export_is_ingested(self, pipeline, tmp_path):
        pipeline.settings.import_root = str(tmp_path / "managed")
        export = tmp_path / "managed" / "instagram-abc"
        d = export / "your_instagram_activity" / "messages" / "inbox" / THREAD
        d.mkdir(parents=True)
        f = d / "message_1.json"
        f.write_text(json.dumps({
            "participants": [{"name": "Esther"}],
            "title": "Esther",
            "messages": [{"sender_name": "Esther",
                          "timestamp_ms": 1728975357343,
                          "content": "hello from the export"}],
        }), encoding="utf-8")

        result = pipeline.process_path(f)
        assert result.status == "COMPLETED"
        assert result.job.records_imported >= 1
        assert pipeline.store.count_people() == 1
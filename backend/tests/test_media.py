"""WhatsApp media attachment feature tests: filename extraction, ZIP media
ingestion, linking to messages (both orderings), local voice transcription,
media-store path safety and the media API."""
from __future__ import annotations

import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from circle.domain.models import MediaKind
from circle.parsers import whatsapp as whatsapp_parser
from tests.store_probe import find, find_one
from circle.security.files import FileSecurityError
from circle.security.media import (
    media_filename_key, media_root, resolve_served_path,
)

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64 + b"\xff\xd9"
OPUS = b"OggS\x00" + b"\x00" * 64

WA_MEDIA = (
    "12/28/23, 8:42 PM - Aravinth Kumar: look at this IMG-20240101-WA0001.jpg (file attached)\n"
    "12/28/23, 8:43 PM - Aravinth Kumar: <attached: PTT-20240101-WA0002.opus>\n"
    "12/28/23, 8:44 PM - Me: got it, thanks!\n"
)


class FakeSTT:
    def __init__(self, text: str = "hey can you share the SIH docs",
                 lang: str = "en"):
        self.text = text
        self.lang = lang
        self.calls: list[str] = []

    def available(self) -> bool:
        return True

    def transcribe(self, path):
        self.calls.append(str(path))
        return self.text, self.lang

    def status(self) -> dict:
        return {"provider": "fake", "available": True}


def _write(path: Path, data: bytes | str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, bytes):
        path.write_bytes(data)
    else:
        path.write_text(data, encoding="utf-8")
    return path


def _zip(path: Path, entries: dict[str, bytes | str]) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        for name, content in entries.items():
            zf.writestr(name, content)
    return path


def _make_pipeline(clean_store, resolver, fake_embedder, fake_llm, tmp_path):
    from circle.config import get_settings
    from circle.ingestion.pipeline import IngestionPipeline
    settings = get_settings()
    settings.import_root = str(tmp_path / "imports")
    settings.processed_root = str(tmp_path / "processed")
    settings.failed_root = str(tmp_path / "failed")
    settings.quarantine_root = str(tmp_path / "quarantine")
    for d in (settings.root_dir(), settings.processed_dir(),
              settings.failed_dir(), settings.quarantine_dir()):
        d.mkdir(parents=True, exist_ok=True)
    stt = FakeSTT()
    pipe = IngestionPipeline(store=clean_store, resolver=resolver,
                             embedder=fake_embedder, llm=fake_llm,
                             stt=stt, settings=settings)
    return pipe, stt


@pytest.fixture()
def media_pipeline(clean_store, resolver, fake_embedder, fake_llm, tmp_path):
    pipe, _ = _make_pipeline(clean_store, resolver, fake_embedder, fake_llm,
                             tmp_path)
    return pipe


@pytest.fixture()
def media_client(clean_store, resolver, fake_embedder, fake_llm, tmp_path):
    from circle.api.context import AppContext, set_context
    from circle.ingestion.watcher import FolderWatcher
    pipe, _ = _make_pipeline(clean_store, resolver, fake_embedder, fake_llm,
                             tmp_path)
    watcher = FolderWatcher(settings=pipe.settings, process_fn=None)
    ctx = AppContext(settings=pipe.settings, store=clean_store,
                     resolver=resolver, embedder=fake_embedder, llm=fake_llm,
                     pipeline=pipe, watcher=watcher)
    ctx.started = True
    ctx.health = {
        "database": {"connected": True, "engine": "mongodb"},
        "llm": {"available": True, "model": "test", "detail": "test"},
        "embeddings": {"available": True, "model": "test", "detail": "test"},
        "stt": {"available": True, "detail": "fake"},
        "tts": {"enabled": False, "detail": "n/a"},
        "watcher": {"running": False, "detail": "test"},
    }
    set_context(ctx)
    from circle.main import app
    with TestClient(app) as c:
        yield c, pipe
    set_context(None)  # type: ignore[arg-type]


# ------------------------------------------------------------------ parsing
class TestWhatsAppAttachmentParsing:
    def test_file_attached_image_with_caption(self, tmp_path):
        f = _write(tmp_path / "chat.txt",
                   "12/28/23, 8:42 PM - Alice: check this IMG-20240101-WA0001.jpg (file attached)\n")
        msg = whatsapp_parser.parse_whatsapp(f).messages[0]
        assert msg.attachments and len(msg.attachments) == 1
        att = msg.attachments[0]
        assert att["kind"] == "image"
        assert att["filename"] == "IMG-20240101-WA0001.jpg"
        assert att["filename_key"] == "img-20240101-wa0001.jpg"
        assert msg.content == "check this"  # caption without filename noise

    def test_ios_attached_form_is_single_voice_note(self, tmp_path):
        f = _write(tmp_path / "chat.txt",
                   "12/28/23, 8:43 PM - Alice: <attached: 00000012-PTT-2024-01-01-12-01-01.opus>\n")
        msg = whatsapp_parser.parse_whatsapp(f).messages[0]
        assert len(msg.attachments) == 1
        att = msg.attachments[0]
        assert att["kind"] == "voice"
        assert att["filename"] == "00000012-PTT-2024-01-01-12-01-01.opus"

    def test_video_sticker_document_kinds(self, tmp_path):
        f = _write(tmp_path / "chat.txt",
                   "12/28/23, 8:42 PM - Alice: VID-20240102-WA0002.mp4 (file attached)\n"
                   "12/28/23, 8:43 PM - Alice: STK-20240103-WA0003.webp (file attached)\n"
                   "12/28/23, 8:44 PM - Alice: DOC-20240104-WA0004.pdf (file attached)\n")
        kinds = [m.attachments[0]["kind"]
                 for m in whatsapp_parser.parse_whatsapp(f).messages]
        assert kinds == ["video", "sticker", "document"]

    def test_legacy_media_omitted_has_no_filename(self, tmp_path):
        f = _write(tmp_path / "chat.txt",
                   "12/28/23, 8:42 PM - Alice: <Media omitted>\n")
        att = whatsapp_parser.parse_whatsapp(f).messages[0].attachments[0]
        assert att["kind"] == "media"
        assert "filename" not in att

    def test_text_without_media_has_no_attachments(self, tmp_path):
        f = _write(tmp_path / "chat.txt",
                   "12/28/23, 8:42 PM - Alice: just words here\n")
        assert whatsapp_parser.parse_whatsapp(f).messages[0].attachments == []


# --------------------------------------------------------------- ingestion
class TestMediaIngestion:
    def test_zip_media_linked_to_messages(self, media_pipeline, clean_store,
                                          tmp_path):
        z = _zip(tmp_path / "WhatsApp Chat with Aravinth.zip", {
            "_chat.txt": WA_MEDIA,
            "Media/IMG-20240101-WA0001.jpg": JPEG,
            "Media/PTT-20240101-WA0002.opus": OPUS,
        })
        result = media_pipeline.process_path(z)
        assert result.status == "COMPLETED"
        assert clean_store.count_media() == 2

        img = clean_store.find_media_by_filename_key("img-20240101-wa0001.jpg")
        assert img is not None
        assert img.kind == MediaKind.IMAGE
        assert img.message_id, "photo must be linked to its message"
        assert img.person_id, "linked media carries the resolved person"
        assert img.status == "LINKED"
        assert Path(img.stored_path).exists()

        voice = clean_store.find_media_by_filename_key("ptt-20240101-wa0002.opus")
        assert voice is not None
        assert voice.kind == MediaKind.VOICE
        assert voice.transcript, "voice note transcribed locally"
        assert voice.message_id

        # the message itself now points at its attachment
        msgs = clean_store.list_media_for_message(img.message_id)
        assert [m.id for m in msgs] == [img.id]
        owner = find_one(clean_store, "messages", {"_id": img.message_id})
        assert img.id in owner["media_ids"]

    def test_voice_note_becomes_searchable_memory(self, media_pipeline,
                                                  clean_store, tmp_path):
        z = _zip(tmp_path / "WhatsApp Chat with Aravinth.zip", {
            "_chat.txt": WA_MEDIA,
            "Media/PTT-20240101-WA0002.opus": OPUS,
        })
        media_pipeline.process_path(z)
        voice = clean_store.find_media_by_filename_key("ptt-20240101-wa0002.opus")
        mems = find(clean_store, "memories",
                     {"record_id": voice.id, "kind": "voice"})
        assert mems, "voice transcript must be indexed as evidence"
        assert mems[0]["person_id"] == voice.person_id

    def test_media_arriving_after_chat_is_linked(self, media_pipeline,
                                                 clean_store, tmp_path):
        """Chat first (media absent), then the photo is dropped later."""
        chat = _write(tmp_path / "Aravinth_chat.txt",
                      "12/28/23, 8:42 PM - Aravinth Kumar: look IMG-20240101-WA0001.jpg (file attached)\n"
                      "12/28/23, 8:43 PM - Me: ok\n")
        assert media_pipeline.process_path(chat).status == "COMPLETED"
        assert clean_store.count_media() == 0

        photo = _write(tmp_path / "IMG-20240101-WA0001.jpg", JPEG)
        assert media_pipeline.process_path(photo).status == "COMPLETED"
        img = clean_store.find_media_by_filename_key("img-20240101-wa0001.jpg")
        assert img and img.message_id, "late media links to the existing message"

    def test_media_arriving_before_chat_is_linked(self, media_pipeline,
                                                  clean_store, tmp_path):
        """Photo dropped first, chat arrives later (both non-zip)."""
        photo = _write(tmp_path / "IMG-20240101-WA0001.jpg", JPEG)
        assert media_pipeline.process_path(photo).status == "COMPLETED"
        assert clean_store.count_media() == 1

        chat = _write(tmp_path / "Aravinth_chat.txt",
                      "12/28/23, 8:42 PM - Aravinth Kumar: look IMG-20240101-WA0001.jpg (file attached)\n"
                      "12/28/23, 8:43 PM - Me: ok\n")
        assert media_pipeline.process_path(chat).status == "COMPLETED"
        img = clean_store.find_media_by_filename_key("img-20240101-wa0001.jpg")
        assert img and img.message_id

    def test_same_media_content_deduped(self, clean_store, tmp_path,
                                        fake_embedder):
        from circle.ingestion.media import MediaIngest
        from circle.config import get_settings
        settings = get_settings()
        ingest = MediaIngest(clean_store, settings)
        a = _write(tmp_path / "IMG-a.jpg", JPEG)
        b = _write(tmp_path / "IMG-b.jpg", JPEG)  # identical bytes
        m1 = ingest.add(a)
        m2 = ingest.add(b)
        assert m1 and m2
        assert m1.id == m2.id
        assert clean_store.count_media() == 1

    def test_media_only_message_keeps_empty_caption(self, media_pipeline,
                                                    clean_store, tmp_path):
        chat = _write(tmp_path / "Aravinth_chat.txt",
                      "12/28/23, 8:42 PM - Aravinth Kumar: IMG-20240101-WA0001.jpg (file attached)\n")
        assert media_pipeline.process_path(chat).status == "COMPLETED"
        msg = find_one(clean_store, "messages",
                        {"attachments": {"$elemMatch": {"filename_key":
                                                       "img-20240101-wa0001.jpg"}}})
        assert msg is not None
        assert msg["content"] == ""


# ------------------------------------------------------------- path safety
class TestMediaPathSafety:
    def test_resolve_blocks_escape(self, tmp_path):
        root = tmp_path / "media"
        root.mkdir()
        outside = tmp_path / "secret.txt"
        outside.write_text("nope")
        with pytest.raises(FileSecurityError):
            resolve_served_path(str(outside), root)

    def test_resolve_allows_inside(self, tmp_path):
        root = tmp_path / "media"
        root.mkdir()
        inside = root / "a.txt"
        inside.write_text("ok")
        assert resolve_served_path(str(inside), root) == inside.resolve()

    def test_media_root_is_under_processed(self, tmp_path):
        assert media_root(tmp_path / "processed") == tmp_path / "processed" / "media"

    def test_filename_key_strips_bidi_marks(self):
        assert media_filename_key("\u200eIMG-1.JPG") == "img-1.jpg"


# ------------------------------------------------------------------- API
class TestMediaApi:
    def _ingest(self, pipe, tmp_path):
        z = _zip(tmp_path / "WhatsApp Chat with Aravinth.zip", {
            "_chat.txt": WA_MEDIA,
            "Media/IMG-20240101-WA0001.jpg": JPEG,
            "Media/PTT-20240101-WA0002.opus": OPUS,
        })
        assert pipe.process_path(z).status == "COMPLETED"

    def test_serve_and_info(self, media_client, tmp_path):
        client, pipe = media_client
        self._ingest(pipe, tmp_path)
        img = pipe.store.find_media_by_filename_key("img-20240101-wa0001.jpg")

        r = client.get(f"/api/media/{img.id}/info")
        assert r.status_code == 200
        assert r.json()["media"]["kind"] == "image"
        assert r.json()["media"]["url"] == f"/api/media/{img.id}"

        r2 = client.get(f"/api/media/{img.id}")
        assert r2.status_code == 200
        assert r2.headers["content-type"].startswith("image/jpeg")
        assert r2.content == JPEG

    def test_list_and_timeline_expose_media(self, media_client, tmp_path):
        client, pipe = media_client
        self._ingest(pipe, tmp_path)
        img = pipe.store.find_media_by_filename_key("img-20240101-wa0001.jpg")

        r = client.get(f"/api/media?person_id={img.person_id}")
        assert r.status_code == 200
        assert any(m["id"] == img.id for m in r.json()["media"])

        t = client.get(f"/api/people/{img.person_id}/timeline")
        assert t.status_code == 200
        entries = t.json()["timeline"]
        assert any(e.get("media") for e in entries)

    def test_missing_media_404(self, media_client):
        client, _ = media_client
        assert client.get("/api/media/nope").status_code == 404

    def test_person_media_endpoint(self, media_client, tmp_path):
        client, pipe = media_client
        self._ingest(pipe, tmp_path)
        img = pipe.store.find_media_by_filename_key("img-20240101-wa0001.jpg")
        r = client.get(f"/api/people/{img.person_id}/media")
        assert r.status_code == 200
        assert any(m["id"] == img.id for m in r.json()["media"])

    def test_person_media_404(self, media_client):
        client, _ = media_client
        assert client.get("/api/people/nope/media").status_code == 404

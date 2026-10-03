"""Ingestion pipeline (spec §6).

    New file -> detect source -> validate -> checksum -> duplicate check
    -> parse -> normalize -> resolve identities -> store records
    -> embeddings -> relationship metrics/profile -> notify UI
    -> move to processed/

Failures move the file to failed/ (security violations to quarantine/) and
NEVER stop the watcher (spec §41).

Files are only ever moved when they live inside Circle's own managed import
root. A folder the user pointed us at (Google Drive, Downloads, a USB backup)
is read in place and left untouched: moving a file there would delete it from
their Drive, and duplicate suppression is by content checksum anyway.
"""
from __future__ import annotations

import logging
import shutil
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from circle.config import Settings, get_settings
from circle.domain.models import (
    CalendarEvent, Conversation, DataOrigin, Document, Email, ImportJob,
    MediaAttachment, MediaKind, Memory, Message, Note, SourceType,
    VoiceRecording,
)
from circle.identity.resolver import IdentityResolver
from circle.ingestion.media import MediaIngest
from circle.parsers import calendar as calendar_parser
from circle.parsers import contacts as contacts_parser
from circle.parsers import documents as documents_parser
from circle.parsers import email_parser
from circle.parsers import generic as generic_parser
from circle.parsers import instagram as instagram_parser
from circle.parsers import telegram as telegram_parser
from circle.parsers import whatsapp as whatsapp_parser
from circle.parsers import x as x_parser
from circle.parsers.common import ParseResult, clean_text, deterministic_id
from circle.parsers.export_scope import skip_reason as _export_skip_reason
from circle.relationship.metrics import compute_profile, extract_topics, record_event
from circle.repository.base import Store
from circle.security.files import (
    AUDIO_EXTENSIONS, FileSecurityError, move_file, safe_extract_zip,
    sanitize_filename, sha256_file, validate_extension, validate_size,
)
from circle.security.media import MEDIA_EXTENSIONS, media_filename_key

log = logging.getLogger("circle.pipeline")

MAX_ZIP_DEPTH = 3

SOURCE_BY_FOLDER = {
    "whatsapp": SourceType.WHATSAPP, "telegram": SourceType.TELEGRAM,
    "instagram": SourceType.INSTAGRAM, "x": SourceType.X,
    "twitter": SourceType.X, "email": SourceType.EMAIL,
    "calendar": SourceType.CALENDAR, "contacts": SourceType.CONTACTS,
    "voice": SourceType.VOICE, "chats": SourceType.CHAT,
    "chat": SourceType.CHAT, "notes": SourceType.NOTES,
    "documents": SourceType.DOCUMENT,
}


class PipelineResult:
    def __init__(self, status: str, job: Optional[ImportJob] = None,
                 error: str = ""):
        self.status = status
        self.job = job
        self.error = error


class IngestionPipeline:
    def __init__(self, store: Store, resolver: IdentityResolver,
                 embedder, llm=None, stt=None, broker=None,
                 settings: Optional[Settings] = None,
                 watch_roots: Optional[list[Path]] = None):
        self.store = store
        self.resolver = resolver
        self.embedder = embedder
        self.llm = llm
        self.stt = stt
        self.broker = broker
        self.settings = settings or get_settings()
        # Folders whose subfolder names identify the source (whatsapp/, ...).
        self.watch_roots = watch_roots or (
            ([self.settings.root_dir()] if self.settings.root_dir() else [])
            + self.settings.extra_watch_roots())

    # ------------------------------------------------------------------
    # public entry
    # ------------------------------------------------------------------
    def process_path(self, path: Path) -> PipelineResult:
        path = Path(path)
        job: Optional[ImportJob] = None
        try:
            if not path.exists() or path.is_dir():
                return PipelineResult("SKIPPED")

            # Inside a Meta data export almost nothing is a conversation. Skip
            # the noise before opening the file: it keeps account settings and
            # ad interests from being parsed as chats, and stops thousands of
            # photos from being stored as unlinked media.
            reason = _export_skip_reason(path)
            if reason:
                log.debug("skipping %s: %s", path.name, reason)
                return PipelineResult("SKIPPED")

            name = sanitize_filename(path.name)
            source = self._detect_source(path)
            ext = path.suffix.lower()
            if ext in MEDIA_EXTENSIONS and ext != ".zip":
                # media files use their own (larger) ceiling enforced by the
                # media store; they are never parsed as text.
                if path.stat().st_size == 0:
                    raise FileSecurityError("empty file")
            else:
                ext = validate_extension(path)
                validate_size(path, self.settings.file_max_bytes)

            checksum = sha256_file(path)
            if self.store.already_processed(checksum):
                self._emit("job", {"status": "SKIPPED", "filename": name,
                                   "reason": "duplicate checksum"})
                self._archive(path)
                return PipelineResult("SKIPPED")

            job = self.store.insert_job(ImportJob(
                filename=name, path=str(path), source=source,
                status="PROCESSING", checksum=checksum,
                started_at=datetime.now(timezone.utc)))
            self._emit("job", {"id": job.id, "status": "PROCESSING",
                               "filename": name,
                               "source": source.value if source else None})

            media = MediaIngest(self.store, self.settings, stt=self.stt)
            if ext == ".zip":
                imported, skipped = self._process_zip(path, job, source,
                                                      media=media)
            else:
                imported, skipped = self._process_one(path, job, source,
                                                      checksum, media=media)

            job.status = "COMPLETED"
            job.completed_at = datetime.now(timezone.utc)
            job.records_imported = imported
            job.records_skipped = skipped
            self.store.update_job(job)
            self.store.mark_processed(checksum, str(path), job.id or "")
            self._archive(path)
            self._emit("job", {"id": job.id, "status": "COMPLETED",
                               "filename": name, "records": imported})
            log.info("processed %s (%d records)", name, imported)
            return PipelineResult("COMPLETED", job)

        except FileSecurityError as e:
            return self._fail(path, job, f"security: {e}", quarantine=True)
        except ValueError as e:
            return self._fail(path, job, str(e))
        except (OSError, RuntimeError) as e:
            return self._fail(path, job, f"io: {e}")
        except Exception as e:  # never crash the watcher
            log.error("pipeline error on %s: %s\n%s", path, e,
                      traceback.format_exc(limit=6))
            return self._fail(path, job, f"unexpected: {type(e).__name__}: {e}")

    # ------------------------------------------------------------------
    def _archive(self, path: Path) -> None:
        """Move an imported file into processed/, when we are allowed to.

        Watch folders that belong to the user (Google Drive, Downloads) are
        left exactly as they are: the record is already indexed and the
        checksum makes a future rescan a no-op.
        """
        if not self.settings.should_archive(path):
            return
        try:
            move_file(path, self.settings.processed_dir())
        except Exception as e:
            log.warning("could not archive %s: %s", path.name, e)

    def _fail(self, path: Path, job: Optional[ImportJob], error: str,
              quarantine: bool = False) -> PipelineResult:
        status = "QUARANTINED" if quarantine else "FAILED"
        log.warning("%s: %s -> %s", path.name, error, status)
        if job:
            job.status = status
            job.error = error[:500]
            job.completed_at = datetime.now(timezone.utc)
            try:
                self.store.update_job(job)
            except Exception:
                pass
        try:
            if path.exists():
                dest = (self.settings.quarantine_dir() if quarantine
                        else self.settings.failed_dir())
                if self.settings.should_archive(path):
                    move_file(path, dest)
        except Exception as e:
            log.error("could not move failed file: %s", e)
        self._emit("job", {"id": job.id if job else None, "status": status,
                           "filename": path.name, "error": error[:200]})
        return PipelineResult(status, job, error)

    # ------------------------------------------------------------------
    # source detection
    # ------------------------------------------------------------------
    def _detect_source(self, path: Path) -> Optional[SourceType]:
        resolved = path.resolve()
        for root in self.watch_roots:
            try:
                rel = resolved.relative_to(Path(root))
            except (ValueError, OSError):
                continue
            for part in rel.parts[:-1]:
                key = part.lower().strip()
                if key in SOURCE_BY_FOLDER:
                    return SOURCE_BY_FOLDER[key]
            break
        n = path.name.lower()
        if n.endswith("_chat.txt") or "whatsapp" in n:
            return SourceType.WHATSAPP
        if "telegram" in n:
            return SourceType.TELEGRAM
        if "instagram" in n:
            return SourceType.INSTAGRAM
        if n.startswith("direct-message") or "twitter" in n or n.endswith(".js") and "data" in n:
            return SourceType.X
        return None

    def _sniff(self, path: Path, source: Optional[SourceType]) -> Optional[SourceType]:
        """Content sniffing when folder/name gave nothing."""
        if source:
            return source
        ext = path.suffix.lower()
        if ext == ".js":
            return SourceType.X
        if ext in (".eml", ".mbox"):
            return SourceType.EMAIL
        if ext == ".ics":
            return SourceType.CALENDAR
        if ext == ".vcf":
            return SourceType.CONTACTS
        if ext in AUDIO_EXTENSIONS:
            return SourceType.VOICE
        if ext in (".json", ".txt"):
            try:
                head = path.read_bytes()[:4000].decode("utf-8", errors="replace")
            except OSError:
                return None
            if "sender_name" in head and "timestamp_ms" in head:
                return SourceType.INSTAGRAM
            if '"messages"' in head and '"from"' in head:
                return SourceType.TELEGRAM
            if "participants" in head and "title" in head:
                return SourceType.INSTAGRAM
            if "message_create" in head or "YTD" in head:
                return SourceType.X
        return None

    # ------------------------------------------------------------------
    # single file processing
    # ------------------------------------------------------------------
    def _process_zip(self, zip_path: Path, job: ImportJob,
                     source: Optional[SourceType], depth: int = 0,
                     media: Optional[MediaIngest] = None) -> tuple[int, int]:
        """Extract safely, process each entry, then always delete the temp copy.

        Nested archives are handled recursively up to MAX_ZIP_DEPTH — common in
        real downloads (Google Takeout, multi-part social exports). Real media
        attachments are staged FIRST so a photo/voice note is stored and linked
        no matter where it sits relative to the chat file in the archive.
        """
        tmp = Path(tempfile.mkdtemp(prefix="circle-zip-",
                                    dir=str(self.settings.temp_dir().parent)
                                    if self.settings.temp_dir().parent.exists() else None))
        total_ok = total_skip = 0
        media = media or MediaIngest(self.store, self.settings, stt=self.stt)
        try:
            entries = safe_extract_zip(zip_path, tmp)

            # Pass 1: stage media files (photos, voice notes, video, documents).
            staged: set[str] = set()
            for entry in entries:
                if entry.name.startswith("~$") or entry.name.startswith("."):
                    continue
                ext = entry.suffix.lower()
                if ext not in MEDIA_EXTENSIONS or ext == ".zip":
                    continue
                # Only chat attachments are media; the export's own photo dump
                # (media/) is not part of any conversation.
                if _export_skip_reason(entry) is not None:
                    total_skip += 1
                    continue
                sub_source = (source or self._detect_source(entry)
                              or self._detect_source(zip_path)
                              or self._sniff(entry, None))
                if sub_source == SourceType.VOICE and ext in AUDIO_EXTENSIONS:
                    continue  # explicit voice folder -> existing voice workflow
                att = media.add(entry, sub_source or SourceType.CHAT)
                if att is not None:
                    staged.add(str(entry))
                    total_ok += 1
                else:
                    total_skip += 1

            for entry in entries:
                if entry.name.startswith("~$") or entry.name.startswith("."):
                    continue
                if str(entry) in staged:
                    continue
                if _export_skip_reason(entry) is not None:
                    total_skip += 1
                    continue
                try:
                    validate_extension(entry)
                except FileSecurityError:
                    total_skip += 1
                    continue
                # Source hints, most specific first: the entry's own path inside
                # the archive, then the archive name, then content sniffing.
                sub_source = (source or self._detect_source(entry)
                              or self._detect_source(zip_path)
                              or self._sniff(entry, None))
                try:
                    if entry.suffix.lower() == ".zip":
                        if depth + 1 >= MAX_ZIP_DEPTH:
                            log.warning("nested archive too deep, skipping: %s",
                                        entry.name)
                            total_skip += 1
                            continue
                        ok, skip = self._process_zip(entry, job, sub_source,
                                                     depth + 1, media=media)
                        total_ok += ok
                        total_skip += skip
                        continue
                    csum = sha256_file(entry)
                    if self.store.already_processed(csum):
                        total_skip += 1
                        continue
                    ok, skip = self._process_one(entry, job, sub_source, csum,
                                                 media=media)
                    total_ok += ok
                    total_skip += skip
                except Exception as e:
                    log.warning("zip entry failed: %s (%s)", entry.name, e)
                    total_skip += 1
            return total_ok, total_skip
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def _process_one(self, path: Path, job: ImportJob,
                     source: Optional[SourceType], checksum: str,
                     media: Optional[MediaIngest] = None) -> tuple[int, int]:
        source = self._sniff(path, source)
        ext = path.suffix.lower()

        # ---- explicit voice recordings (imports/voice/) -----------
        if ext in AUDIO_EXTENSIONS and source == SourceType.VOICE:
            return self._process_voice(path, job, source, checksum)

        # ---- real media attachment (photo / voice note / video) ---
        if ext in MEDIA_EXTENSIONS and ext != ".zip":
            return self._process_media_file(path, source, checksum, media)

        # ---- legacy: any other loose audio file still transcribes ---
        if ext in AUDIO_EXTENSIONS:
            return self._process_voice(path, job, source or SourceType.VOICE, checksum)

        # ---- parse ------------------------------------------------
        result = self._parse(path, source, ext)
        if result.is_empty():
            raise ValueError("no usable records found in file")

        # ---- persist ----------------------------------------------
        return self._persist(result, source, checksum, path, media=media)

    # ------------------------------------------------------------------
    # media attachments
    # ------------------------------------------------------------------
    def _process_media_file(self, path: Path, source: Optional[SourceType],
                            checksum: str,
                            media: Optional[MediaIngest]) -> tuple[int, int]:
        """Store a standalone media file and link it to messages that name it."""
        media = media or MediaIngest(self.store, self.settings, stt=self.stt)
        att = media.add(path, source or SourceType.CHAT)
        if att is None:
            raise ValueError("unsupported or rejected media file")
        # The chat export may have arrived first: link to matching messages.
        linked = self._link_media_to_existing_messages(att)
        self._emit("media", {"id": att.id, "filename": att.filename,
                             "kind": att.kind.value, "linked": linked})
        return 1, 0

    def _link_media_to_existing_messages(self, media: MediaAttachment) -> int:
        key = media_filename_key(media.filename)
        if not key:
            return 0
        msgs = self.store.find_messages_by_attachment_key(key)
        for m in msgs:
            self._link_media(media, m)
        return len(msgs)

    def _link_media(self, media: MediaAttachment, message: Message) -> None:
        """Attach a stored media file to its message (idempotent)."""
        if not message.id or not media.id:
            return
        changed = False
        if media.message_id != message.id:
            media.message_id = message.id
            media.conversation_id = message.conversation_id or media.conversation_id
            if message.person_id:
                media.person_id = message.person_id
            media.occurred_at = media.occurred_at or message.sent_at
            media.status = "LINKED"
            changed = True
        if media.id not in (message.media_ids or []):
            message.media_ids = list(dict.fromkeys(
                [*(message.media_ids or []), media.id]))
            self.store.update_message_media_ids(message.id, message.media_ids)
        if media.kind == MediaKind.VOICE and not media.transcript:
            self._transcribe_media(media)
            changed = True
        if changed:
            self.store.update_media(media)
        self._store_media_memory(media)

    def _transcribe_media(self, media: MediaAttachment) -> None:
        """Transcribe a voice note locally; failure is recorded, not raised."""
        from circle.voice.stt import get_stt, media_duration_seconds
        stt = self.stt or get_stt()
        path = Path(media.stored_path)
        if not stt.available() or not path.exists():
            media.error = "no local STT engine"
            return
        try:
            media.duration_seconds = media.duration_seconds or \
                media_duration_seconds(path)
            text, lang = stt.transcribe(path)
            media.transcript = text or ""
            media.language = lang or ""
            media.error = ""
        except Exception as e:
            log.warning("voice-note transcription failed: %s", e)
            media.error = str(e)[:200]

    def _store_media_memory(self, media: MediaAttachment) -> None:
        """Index a voice-note transcript / photo caption as searchable evidence."""
        if not media.person_id or not media.id:
            return
        when = media.occurred_at or media.imported_at
        text = kind = cite = ""
        if media.kind == MediaKind.VOICE and media.transcript:
            text = f"[Voice note] {media.transcript}"
            kind = "voice"
            cite = f"🎙️ WhatsApp voice note — {when.strftime('%b %d, %Y')}"
        elif media.kind == MediaKind.IMAGE and media.caption:
            text = f"[Photo] {media.caption}"
            kind = "image"
            cite = f"🖼️ WhatsApp photo — {when.strftime('%b %d, %Y')}"
        if not text:
            return
        mem_id = "mem-vm-" + (media.checksum or media.id)[:24]
        if self.store.get_memory(mem_id):
            return  # already indexed (repeat import)
        mem = Memory(
            id=mem_id, person_id=media.person_id, kind=kind,
            source=media.source, origin=media.origin,
            occurred_at=media.occurred_at or when,
            text=clean_text(text)[:8000], record_id=media.id,
            source_id=media.checksum, citation=cite,
        )
        mem.topics = extract_topics(text)
        self._embed_and_store([mem])

    def _parse(self, path: Path, source: Optional[SourceType],
               ext: str) -> ParseResult:
        if source == SourceType.WHATSAPP and ext == ".txt":
            return whatsapp_parser.parse_whatsapp(path)
        if source == SourceType.TELEGRAM and ext == ".json":
            return telegram_parser.parse_telegram(path)
        if source == SourceType.INSTAGRAM and ext in (".json", ".txt"):
            return instagram_parser.parse_instagram(path)
        if source == SourceType.X and ext in (".json", ".js"):
            return x_parser.parse_x(path)
        if source == SourceType.EMAIL and ext in (".eml", ".mbox", ".csv", ".json"):
            return email_parser.parse_email(path)
        if source == SourceType.CALENDAR and ext in (".ics", ".csv"):
            return calendar_parser.parse_calendar(path)
        if source == SourceType.CONTACTS and ext in (".csv", ".vcf"):
            return contacts_parser.parse_contacts(path)
        if source == SourceType.NOTES and ext in (".txt", ".md"):
            return self._parse_notes_folder(path)
        if source == SourceType.DOCUMENT and ext in (".txt", ".md", ".pdf",
                                                     ".docx", ".html", ".htm",
                                                     ".csv", ".json"):
            return documents_parser.document_result(path)
        if ext in (".pdf", ".docx"):
            return documents_parser.document_result(path)
        if ext in (".html", ".htm") and source in (SourceType.DOCUMENT, None):
            # html without chat structure still extracts as a document
            pass  # handled by fallback chain below

        # Fallback chain: try likely parsers, then generic
        attempts = []
        if ext in (".txt", ".md"):
            # A freeform .txt only becomes a document when it lives in the
            # documents/notes folders; elsewhere it must parse as a chat
            # or fail -- unknown formats are imported explicitly via mapping.
            attempts = [whatsapp_parser.parse_whatsapp,
                        generic_parser.parse_generic]
        elif ext == ".json":
            attempts = [telegram_parser.parse_telegram,
                        instagram_parser.parse_instagram,
                        x_parser.parse_x,
                        generic_parser.parse_generic]
        elif ext == ".js":
            attempts = [x_parser.parse_x]
        elif ext == ".csv":
            attempts = [contacts_parser.parse_contacts,
                        calendar_parser.parse_calendar,
                        email_parser.parse_email,
                        generic_parser.parse_generic]
        elif ext in (".html", ".htm"):
            attempts = [generic_parser.parse_generic,
                        documents_parser.document_result]
        elif ext in (".eml", ".mbox"):
            attempts = [email_parser.parse_email]
        elif ext == ".ics":
            attempts = [calendar_parser.parse_calendar]
        elif ext == ".vcf":
            attempts = [contacts_parser.parse_contacts]
        else:
            attempts = [generic_parser.parse_generic,
                        documents_parser.document_result]

        errors = []
        for fn in attempts:
            try:
                res = fn(path)
                if not res.is_empty():
                    return res
            except Exception as e:
                errors.append(f"{fn.__name__}: {e}")
        raise ValueError("could not parse file (" + "; ".join(errors[-3:]) + ")")

    def _parse_notes_folder(self, path: Path) -> ParseResult:
        text = path.read_bytes().decode("utf-8", errors="replace")
        result = ParseResult()
        if text.strip():
            result.notes.append(Note(
                id="note-" + deterministic_id(path.name, text[:400]),
                title=clean_text(text.splitlines()[0])[:120] or path.stem,
                body=text.strip(),
                noted_at=datetime.fromtimestamp(path.stat().st_mtime,
                                                tz=timezone.utc),
                origin=DataOrigin.IMPORTED,
                external_id="note-" + deterministic_id(path.name, text[:400]),
            ))
        return result

    # ------------------------------------------------------------------
    # persistence
    # ------------------------------------------------------------------
    def _persist(self, result: ParseResult, source: Optional[SourceType],
                 checksum: str, path: Path,
                 media: Optional[MediaIngest] = None) -> tuple[int, int]:
        imported = skipped = 0

        # 0. contacts (identity first so senders resolve against them)
        if result.contacts:
            created = self.resolver.ingest_contacts(result.contacts)
            imported += len(result.contacts)
            self._emit("contacts", {"count": len(result.contacts),
                                    "created": created})

        # 1. conversations
        conv_map: dict[str, str] = {}
        for conv in result.conversations:
            if not conv.get("external_key"):
                continue
            obj = Conversation(
                title=conv.get("title", ""),
                source=conv.get("source") or (source or SourceType.CHAT),
                external_key=conv["external_key"],
                participant_ids=[],
                origin=DataOrigin.IMPORTED,
            )
            c = self.store.insert_conversation(obj)
            if c and c.id:
                conv_map[conv["external_key"]] = c.id

        # 2. messages -> people resolution
        affected: set[str] = set()
        memories: list[Memory] = []
        if result.messages:
            imported_cnt, skipped_cnt, affected_ids = self._store_messages(
                result.messages, conv_map, checksum, path, media=media)
            imported += imported_cnt
            skipped += skipped_cnt
            affected |= affected_ids

        # 3. emails
        for email in result.emails:
            person = None
            if email.from_address:
                person = self.resolver.find_person(email=email.from_address)
            if person is None:
                person, _ = self.resolver.resolve_sender(
                    email.from_label or email.from_address, SourceType.EMAIL,
                    {"email": email.from_address})
            if person and person.id:
                email.person_id = person.id
                affected.add(person.id)
            if self.store.insert_email_if_new(email):
                imported += 1
                if person and person.id and email.sent_at:
                    record_event(self.store, person_id=person.id, kind="email",
                                 source=SourceType.EMAIL.value,
                                 occurred_at=email.sent_at,
                                 summary=email.subject or "(no subject)",
                                 record_id=email.id or email.external_id)
            else:
                skipped += 1

        # 4. calendar events
        for event in result.events:
            person_ids = []
            for p_name in event.participants:
                person = self.resolver.find_person(name=p_name) or \
                    self.resolver.resolve_sender(p_name, SourceType.CALENDAR)[0]
                if person and person.id:
                    person_ids.append(person.id)
                    affected.add(person.id)
            event.person_ids = list(dict.fromkeys(person_ids))
            if self.store.insert_calendar_if_new(event):
                imported += 1
                for pid in event.person_ids:
                    if event.starts_at:
                        record_event(self.store, person_id=pid, kind="meeting",
                                     source=SourceType.CALENDAR.value,
                                     occurred_at=event.starts_at,
                                     summary=event.title,
                                     record_id=event.id or event.external_id)
            else:
                skipped += 1

        # 5. notes
        for note in result.notes:
            if note.person_id is None and result.contacts:
                pass
            if self.store.insert_note_if_new(note):
                imported += 1
            else:
                skipped += 1

        # 6. documents
        for filename, title, text in result.documents:
            doc_id = "doc-" + deterministic_id(filename, text[:400])
            doc = Document(id=doc_id, filename=filename, title=title,
                           text=text[:500000],
                           mime_type=Path(filename).suffix.lower(),
                           origin=DataOrigin.IMPORTED, external_id=doc_id)
            if self.store.insert_document_if_new(doc):
                imported += 1
            else:
                skipped += 1

        # 7. memories (searchable evidence) for every record kind
        memories.extend(self._memories_from_result(result, conv_map, checksum))
        for filename, title, text in result.documents:
            memories.extend(self._chunk_document(filename, title, text, checksum))
        if memories:
            self._embed_and_store(memories)

        # 8. profiles for affected people
        for pid in affected:
            self._refresh_profile(pid)

        # source provenance record
        from circle.domain.models import Source
        self.store.insert_source(Source(
            filename=path.name,
            source_type=source or SourceType.CHAT,
            checksum=checksum,
            record_count=imported,
        ))
        self._emit("sync", {"filename": path.name, "imported": imported,
                            "skipped": skipped,
                            "people_updated": len(affected)})
        return imported, skipped

    # ------------------------------------------------------------------
    def _store_messages(self, messages: list[Message], conv_map: dict[str, str],
                        checksum: str, path: Path,
                        media: Optional[MediaIngest] = None) -> tuple[int, int, set[str]]:
        imported = skipped = 0
        affected: set[str] = set()

        # group by conversation to detect 1:1 vs group
        by_conv: dict[str, list[Message]] = {}
        for m in messages:
            by_conv.setdefault(m.conversation_id or "default", []).append(m)

        for conv_key, msgs in by_conv.items():
            cid = conv_map.get(conv_key)
            # resolve every distinct sender once
            sender_person: dict[str, Optional[object]] = {}
            for m in msgs:
                label = m.sender_label
                if label not in sender_person:
                    sender_person[label] = self.resolver.resolve_sender(
                        label, m.source)
            non_user_labels = [lbl for lbl, p in sender_person.items()
                               if p and p[0] is not None]
            # 1:1 conversation => every message belongs to that person
            one_to_one_person = None
            if len(non_user_labels) == 1:
                one_to_one_person = sender_person[non_user_labels[0]][0]

            if cid:
                conv_obj = self.store.get_conversation(cid)
                if conv_obj:
                    pids = [p[0].id for p in sender_person.values()
                            if p and p[0] and getattr(p[0], "id", None)]
                    conv_obj.participant_ids = list(dict.fromkeys(pids))
                    self.store.update_conversation(conv_obj)

            for m in msgs:
                resolved = sender_person.get(m.sender_label)
                person = resolved[0] if resolved else None
                if person is None and one_to_one_person is not None:
                    person = one_to_one_person
                if person and getattr(person, "id", None):
                    m.person_id = person.id
                    affected.add(person.id)
                if cid:
                    m.conversation_id = cid
                if self.store.insert_message_if_new(m):
                    imported += 1
                    if m.person_id and m.sent_at:
                        is_user_msg = self.resolver.is_user_label(m.sender_label)
                        if not is_user_msg or one_to_one_person is not None:
                            record_event(
                                self.store, person_id=m.person_id,
                                kind="message", source=m.source.value,
                                occurred_at=m.sent_at,
                                summary=f"{m.sender_label}: "
                                        f"{m.content[:80]}",
                                record_id=m.id or "")
                else:
                    skipped += 1
                # Link any real media this message referenced. Idempotent:
                # the media may be staged now or already stored (repeat import).
                for att in (m.attachments or []):
                    if not isinstance(att, dict):
                        continue
                    key = att.get("filename_key")
                    if not key:
                        continue
                    found = (media.find_by_key(key) if media else None) or \
                        self.store.find_media_by_filename_key(key)
                    if found:
                        self._link_media(found, m)
                        if found.person_id:
                            affected.add(found.person_id)
        return imported, skipped, affected

    # ------------------------------------------------------------------
    # memories + embeddings
    # ------------------------------------------------------------------
    def _memories_from_result(self, result: ParseResult, conv_map: dict[str, str],
                              checksum: str) -> list[Memory]:
        out: list[Memory] = []
        for m in result.messages:
            text = clean_text(m.content)
            if not text:
                continue
            mem = Memory(
                id="mem-" + (m.id or deterministic_id(text)),
                person_id=m.person_id,
                kind="message",
                source=m.source,
                origin=m.origin,
                occurred_at=m.sent_at,
                text=f"{m.sender_label}: {text}" if m.sender_label else text,
                record_id=m.id or "",
                source_id=checksum,
                citation=_citation(m.source.value, "message", m.sent_at),
            )
            mem.topics = extract_topics(text)
            out.append(mem)
        for e in result.emails:
            body = clean_text(e.body or e.subject)
            if not body:
                continue
            mem = Memory(
                id="mem-" + (e.id or deterministic_id(body)),
                person_id=e.person_id,
                kind="email",
                source=SourceType.EMAIL,
                origin=e.origin,
                occurred_at=e.sent_at,
                text=f"{e.subject}\n{body}"[:8000],
                record_id=e.id or "",
                source_id=checksum,
                citation=_citation("email", "email", e.sent_at, e.subject),
            )
            mem.topics = extract_topics(f"{e.subject} {body}")
            out.append(mem)
        for ev in result.events:
            pid = ev.person_ids[0] if ev.person_ids else None
            label = " / ".join(ev.participants[:4])
            text = clean_text(
                f"{ev.title}. {label}. {ev.description} {ev.location}".strip())
            mem = Memory(
                id="mem-" + (ev.id or deterministic_id(text)),
                person_id=pid,
                kind="calendar",
                source=SourceType.CALENDAR,
                origin=ev.origin,
                occurred_at=ev.starts_at,
                text=text,
                record_id=ev.id or "",
                source_id=checksum,
                citation=_citation("calendar", "event", ev.starts_at, ev.title),
            )
            mem.topics = extract_topics(text)
            for p_id in ev.person_ids[1:]:
                clone = mem.model_copy(update={"id": mem.id + "-" + p_id[:8],
                                               "person_id": p_id})
                out.append(clone)
            out.append(mem)
        for n in result.notes:
            text = clean_text(f"{n.title}. {n.body}".strip())
            if not text:
                continue
            mem = Memory(
                id="mem-" + (n.id or deterministic_id(text)),
                person_id=n.person_id,
                kind="note",
                source=SourceType.NOTES,
                origin=n.origin,
                occurred_at=n.noted_at,
                text=text,
                record_id=n.id or "",
                source_id=checksum,
                citation=_citation("note", "note", n.noted_at),
            )
            mem.topics = extract_topics(text)
            out.append(mem)
        return out

    def _chunk_document(self, filename: str, title: str, text: str,
                        checksum: str) -> list[Memory]:
        clean = clean_text(text)
        if not clean:
            return []
        chunks: list[str] = []
        size = 1200
        if len(clean) <= size:
            chunks = [clean]
        else:
            start = 0
            while start < len(clean):
                chunks.append(clean[start:start + size])
                start += size
        out = []
        doc_id = "doc-" + deterministic_id(filename, text[:400])
        for i, chunk in enumerate(chunks[:100]):
            mem = Memory(
                id=f"mem-{doc_id}-{i}",
                person_id=None,
                kind="document",
                source=SourceType.DOCUMENT,
                origin=DataOrigin.IMPORTED,
                occurred_at=None,
                text=f"{title}: {chunk}" if i == 0 else chunk,
                record_id=doc_id,
                source_id=checksum,
                citation=f"{filename}",
            )
            mem.topics = extract_topics(chunk)
            out.append(mem)
        return out

    def _embed_and_store(self, memories: list[Memory]) -> int:
        if not memories:
            return 0
        batch = 16
        inserted = 0
        for i in range(0, len(memories), batch):
            chunk = memories[i:i + batch]
            try:
                vectors = self.embedder.embed_batch([m.text for m in chunk])
            except Exception as e:
                log.warning("embedding failed (%s); storing without vectors", e)
                vectors = [None] * len(chunk)   # type: ignore
            for m, vec in zip(chunk, vectors):
                if vec:
                    m.embedding = vec
            inserted += self.store.insert_memories(chunk)
        return inserted

    # ------------------------------------------------------------------
    # voice
    # ------------------------------------------------------------------
    def _process_voice(self, path: Path, job: ImportJob, source: SourceType,
                       checksum: str) -> tuple[int, int]:
        from circle.voice.stt import get_stt, media_duration_seconds
        stt = self.stt or get_stt()
        rec_id = "voice-" + checksum[:24]
        rec = VoiceRecording(
            id=rec_id, filename=sanitize_filename(path.name),
            checksum=checksum, recorded_at=datetime.fromtimestamp(
                path.stat().st_mtime, tz=timezone.utc),
            processing_status="PROCESSING", external_id=rec_id,
        )
        self.store.insert_voice_if_new(rec)
        self._emit("voice", {"filename": rec.filename, "status": "PROCESSING"})
        try:
            if not stt.available():
                raise RuntimeError(stt.status().get("detail",
                                                    "no local STT engine"))
            rec.duration_seconds = media_duration_seconds(path)
            text, lang = stt.transcribe(path)
            if not text:
                raise ValueError("transcription produced no text")
            rec.transcript = text
            rec.language = lang
            rec.processing_status = "COMPLETED"
            self.store.update_voice(rec)

            # person suggestion (never automatic -- user confirms)
            suggestion = None
            for p in self.store.list_people(limit=1000):
                if p.display_name.lower() in text.lower():
                    suggestion = p.id
                    break
            if suggestion:
                self.store.upsert_identity_suggestion(
                    f"sug-voice-{rec_id}-{suggestion}",
                    {"person_a_id": suggestion, "label": rec.filename,
                     "source": "voice", "confidence": 0.5,
                     "reason": "name mentioned in transcript",
                     "status": "pending",
                     "detail": {"person_name": "see profile",
                                "identity": rec.filename,
                                "source": "voice"}})

            mem = Memory(
                id="mem-" + rec_id,
                person_id=None,
                kind="voice",
                source=SourceType.VOICE,
                origin=DataOrigin.IMPORTED,
                occurred_at=rec.recorded_at,
                text=f"[Voice recording {rec.filename}] {clean_text(text)}"[:8000],
                record_id=rec_id,
                source_id=checksum,
                citation=f"🎙️ {rec.filename} — "
                         f"{rec.recorded_at.strftime('%b %d, %Y') if rec.recorded_at else ''}",
            )
            mem.topics = extract_topics(text)
            self._embed_and_store([mem])
            self._emit("voice", {"filename": rec.filename, "status": "COMPLETED"})
            return 1, 0
        except Exception as e:
            rec.processing_status = "FAILED"
            rec.error = str(e)[:400]
            self.store.update_voice(rec)
            self._emit("voice", {"filename": rec.filename, "status": "FAILED",
                                 "error": str(e)[:200]})
            raise ValueError(f"voice transcription failed: {e}")

    # ------------------------------------------------------------------
    def _refresh_profile(self, person_id: str) -> None:
        try:
            profile = compute_profile(self.store, person_id)
            if profile:
                person = self.store.get_person(person_id)
                self._emit("person", {
                    "person_id": person_id,
                    "name": person.display_name if person else "",
                    "status": profile.status.value,
                    "reason": profile.status_reason,
                    "interactions": profile.interaction_count,
                    "recent14": profile.interactions_14d,
                    "last": profile.last_interaction_at.isoformat()
                            if profile.last_interaction_at else None,
                })
        except Exception as e:
            log.error("profile refresh failed for %s: %s", person_id, e)

    def refresh_profiles(self, person_ids: Optional[set[str]] = None) -> None:
        ids = person_ids or {p.id for p in self.store.list_people(limit=2000) if p.id}
        for pid in ids:
            self._refresh_profile(pid)

    # ------------------------------------------------------------------
    def _emit(self, kind: str, payload: dict) -> None:
        if self.broker:
            try:
                self.broker.publish(kind, payload)
            except Exception:
                pass


def _citation(source: str, kind: str, when: Optional[datetime],
              label: str = "") -> str:
    src = {"whatsapp": "WhatsApp", "telegram": "Telegram",
           "instagram": "Instagram", "x": "X", "email": "Email",
           "calendar": "Calendar", "notes": "Note", "chat": "Chat",
           "voice": "🎙️", "document": "Document"}.get(source, source.title())
    when_s = when.strftime("%b %d, %Y %H:%M") if when else "undated"
    extra = f" — {label}" if label else ""
    return f"{src}{extra} — {when_s}"

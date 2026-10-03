"""Core API routes: health, people, timeline, notes, search, ask, identity,
relationship, realtime events (spec §37)."""
from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from circle.ai.rag import RagPipeline, hybrid_retrieve, understand
from circle.api.context import get_context
from circle.config import get_settings
from circle.api.routes_imports import media_dto
from circle.domain.models import DataOrigin, Memory, Note, SourceType
from circle.relationship.metrics import compute_profile, extract_topics, record_event
from circle.security.files import sha256_text

log = logging.getLogger("circle.api")
router = APIRouter(prefix="/api")


# --------------------------------------------------------------------- health
@router.get("/health")
def health() -> dict:
    ctx = get_context()
    return {
        "status": "ok" if ctx.started else "starting",
        "health": ctx.health,
        "started": ctx.started,
        "offline_ready": bool(ctx.health.get("llm", {}).get("available")),
    }


@router.get("/auth/status")
def auth_status(request: Request) -> dict:
    """Whether this backend wants an access key, without leaking anything.

    Public by design: a browser on another machine has to be able to learn
    that it needs a key before it can show the user anything useful. Returns
    only booleans and model names, never archive content.

    `key_accepted` is what the browser actually gates on. This endpoint is
    public, so a 200 here does NOT mean the caller's key was valid: without
    this field the UI cannot tell "no key needed" from "wrong key" and waits
    forever on a real request that will 401.
    """
    from circle.api.auth import check, key_configured
    settings = get_settings()
    allowed, _ = check(request)
    llm = (get_context().health.get("llm") or {})
    installed = llm.get("installed_models") or []
    model = settings.ollama_model
    required = key_configured()
    return {
        "access_key_required": required,
        # True when this caller may proceed to real endpoints.
        "key_accepted": allowed,
        "remote_allowed": bool(settings.allow_remote),
        "llm_available": bool(llm.get("available")),
        "model": model,
        "model_installed": any(
            str(m).split(":")[0] == model.split(":")[0] for m in installed),
        "installed_models": installed,
        "embedding_model": settings.ollama_embedding_model,
        "embedding_installed": any(
            str(m).startswith(settings.ollama_embedding_model.split(":")[0])
            for m in installed),
        "ollama_reachable": bool(llm.get("available")),
    }


@router.get("/setup/pull-model")
def pull_model() -> dict:
    """Download a missing model through Ollama.

    First run cannot answer anything until gemma3:4b is on disk, and the user
    may not know Ollama is even running. Pulling here means the browser does
    not have to be open in a terminal, and the failure is reported in the UI
    instead of a console nobody is watching.
    """
    settings = get_settings()
    model = settings.ollama_model
    try:
        import httpx
        with httpx.Client(timeout=15.0) as client:
            r = client.post(f"{settings.ollama_url.rstrip('/')}/api/pull",
                            json={"model": model, "stream": False})
        if r.status_code >= 400:
            return {"ok": False, "model": model,
                    "error": f"Ollama returned {r.status_code}: {r.text[:200]}"}
    except Exception as e:
        return {"ok": False, "model": model,
                "error": f"cannot reach Ollama at {settings.ollama_url}: {e}"}
    return {"ok": True, "model": model,
            "detail": f"{model} is available. Restart Circle if answers still fail."}


@router.get("/sync/status")
def sync_status() -> dict:
    return get_context().sync_status()


# ------------------------------------------------------------------ realtime
@router.get("/events")
async def events(request: Request) -> StreamingResponse:
    ctx = get_context()
    ctx.broker.bind_loop(asyncio.get_running_loop())

    async def gen():
        yield "retry: 3000\n\n"
        yield json.dumps({"type": "hello", "data": {"started": ctx.started}}) + "\n\n"
        async for event in ctx.broker.subscribe():
            if await request.is_disconnected():
                break
            yield json.dumps(event, default=str) + "\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


# -------------------------------------------------------------------- people
def _person_dto(person, profile=None) -> dict:
    ctx = get_context()
    profile = profile if profile is not None else ctx.store.get_profile(person.id)
    dto = person.model_dump(mode="json")
    if profile:
        dto["status"] = profile.status.value
        dto["status_reason"] = profile.status_reason
        dto["interaction_count"] = profile.interaction_count
        dto["interactions_14d"] = profile.interactions_14d
        dto["last_interaction_at"] = (
            profile.last_interaction_at.isoformat()
            if profile.last_interaction_at else None)
    else:
        dto["status"] = None
        dto["status_reason"] = "No recorded interactions yet."
        dto["interaction_count"] = 0
        dto["interactions_14d"] = 0
        dto["last_interaction_at"] = None
    return dto


# Bounds for the paginated people list.
PEOPLE_PAGE_MAX = 200
PEOPLE_SCAN_MAX = 5000

# Sort orders offered by the people ledger.
PEOPLE_SORTS = ("recent", "name", "interactions")


def _starts_with_digit(name: str) -> bool:
    """True when the first alphanumeric character is a digit.

    Contacts imported from a chat export are often just a saved phone number
    ("+91 62816 83480"). Those should sort after real names, not before them.
    """
    for ch in name.strip():
        if ch.isalnum():
            return ch.isdigit()
    return False


def _natural(text: str) -> tuple[tuple[int, Any], ...]:
    """Compare text so embedded numbers sort numerically ("x2" before "x10")."""
    parts = [p for p in re.split(r"(\d+)", text.lower()) if p]
    return tuple((1, int(p)) if p.isdigit() else (0, p) for p in parts)


def _name_key(name: str, person_id: str) -> tuple:
    """Order people by name: letters first, number-only contacts last."""
    return (1 if _starts_with_digit(name) else 0, _natural(name), person_id)


@router.get("/people")
def list_people(q: Optional[str] = None, status: Optional[str] = None,
                limit: int = 50, offset: int = 0,
                sort: str = "recent") -> dict:
    """Paginated people list.

    Filtering and ordering run across the whole set so `total`, the sort order
    and the "most active" rail stay correct, but only the requested page is
    returned, so the UI never has to render every person at once. Every order
    has a deterministic tie-break so a row cannot move between pages.
    """
    ctx = get_context()
    limit = max(1, min(limit, PEOPLE_PAGE_MAX))
    offset = max(0, offset)
    sort = sort if sort in PEOPLE_SORTS else "recent"
    people = [p for p in ctx.store.list_people(limit=PEOPLE_SCAN_MAX)
              if not p.is_user]
    profiles = {p.person_id: p for p in ctx.store.list_profiles()}
    dtos = [_person_dto(p, profiles.get(p.id)) for p in people]
    if q:
        ql = q.lower()
        dtos = [d for d in dtos
                if ql in d["display_name"].lower()
                or any(ql in a.lower() for a in d.get("aliases", []))]
    if status:
        dtos = [d for d in dtos if d.get("status") == status]
    if sort == "name":
        dtos.sort(key=lambda d: _name_key(d["display_name"], d["id"]))
    elif sort == "interactions":
        dtos.sort(key=lambda d: ((-(d.get("interaction_count") or 0),)
                                  + _name_key(d["display_name"], d["id"])))
    else:
        dtos.sort(key=lambda d: ((d.get("last_interaction_at") or "",)
                                  + _name_key(d["display_name"], d["id"])),
                  reverse=True)
    top = sorted(dtos, key=lambda d: ((-(d.get("interaction_count") or 0),)
                                      + _name_key(d["display_name"], d["id"])))[:6]
    return {
        "people": dtos[offset:offset + limit],
        "total": len(dtos),
        "offset": offset,
        "limit": limit,
        "sort": sort,
        "top": [
            {
                "id": d["id"],
                "display_name": d["display_name"],
                "interaction_count": d.get("interaction_count") or 0,
            }
            for d in top
        ],
    }


@router.get("/people/{person_id}")
def get_person(person_id: str) -> dict:
    ctx = get_context()
    person = ctx.store.get_person(person_id)
    if not person:
        raise HTTPException(404, "person not found")
    profile = compute_profile(ctx.store, person_id)
    return {
        "person": person.model_dump(mode="json"),
        "profile": profile.model_dump(mode="json") if profile else None,
        "conversations": [c.model_dump(mode="json")
                          for c in ctx.store.list_conversations_for_person(person_id)],
        "counts": {
            "messages": len(ctx.store.list_messages_for_person(person_id, limit=10000)),
            "emails": len(ctx.store.list_emails_for_person(person_id, limit=10000)),
            "notes": len(ctx.store.list_notes_for_person(person_id, limit=10000)),
            "events": len(ctx.store.list_calendar_for_person(person_id, limit=10000)),
            "voice": len(ctx.store.list_voice_for_person(person_id)),
        },
    }


@router.get("/people/{person_id}/timeline")
def person_timeline(person_id: str, limit: int = 100) -> dict:
    ctx = get_context()
    if not ctx.store.get_person(person_id):
        raise HTTPException(404, "person not found")
    # Group media by message once (avoids a query per timeline entry).
    media_by_msg: dict[str, list[dict]] = {}
    for md in ctx.store.list_media_for_person(person_id, limit=1000):
        if md.message_id:
            media_by_msg.setdefault(md.message_id, []).append(media_dto(md))

    entries: list[dict[str, Any]] = []
    for m in ctx.store.list_messages_for_person(person_id, limit=limit):
        entries.append({"type": "message", "id": m.id,
                        "at": m.sent_at.isoformat() if m.sent_at else None,
                        "title": m.sender_label, "body": m.content[:500],
                        "source": m.source.value, "origin": m.origin.value,
                        "media": media_by_msg.get(m.id or "", [])})
    for e in ctx.store.list_emails_for_person(person_id, limit=limit):
        entries.append({"type": "email", "id": e.id,
                        "at": e.sent_at.isoformat() if e.sent_at else None,
                        "title": e.subject or "(no subject)",
                        "body": e.body[:500],
                        "source": "email", "origin": e.origin.value})
    for ev in ctx.store.list_calendar_for_person(person_id, limit=limit):
        entries.append({"type": "calendar", "id": ev.id,
                        "at": ev.starts_at.isoformat() if ev.starts_at else None,
                        "title": ev.title,
                        "body": f"{ev.location} {ev.description}".strip()[:500],
                        "source": "calendar", "origin": ev.origin.value})
    for n in ctx.store.list_notes_for_person(person_id, limit=limit):
        entries.append({"type": "note", "id": n.id,
                        "at": n.noted_at.isoformat() if n.noted_at else None,
                        "title": n.title, "body": n.body[:500],
                        "source": "notes", "origin": n.origin.value})
    for v in ctx.store.list_voice_for_person(person_id, limit=20):
        entries.append({"type": "voice", "id": v.id,
                        "at": v.recorded_at.isoformat() if v.recorded_at else None,
                        "title": v.filename,
                        "body": (v.transcript or "")[:500],
                        "source": "voice", "origin": v.origin.value})
    entries.sort(key=lambda e: e.get("at") or "", reverse=True)
    return {"timeline": entries[:limit]}


@router.get("/people/{person_id}/memories")
def person_memories(person_id: str, limit: int = 100) -> dict:
    ctx = get_context()
    mems = ctx.store.memories_for_person(person_id, limit=limit)
    return {"memories": [m.model_dump(mode="json") for m in mems]}


@router.get("/people/{person_id}/suggestions")
def person_suggestions(person_id: str) -> dict:
    return {"prompts": [
        "What did we talk about recently?",
        "What projects are we working on together?",
        "What did I promise this person?",
        "What should I remember about our current project?",
        "Show our recent timeline.",
        "What topics do we discuss most?",
        "Prepare me for my next meeting with this person.",
    ]}


# --------------------------------------------------------------------- notes
class NoteIn(BaseModel):
    person_id: Optional[str] = None
    title: str = ""
    body: str = Field(..., min_length=1, max_length=20000)
    noted_at: Optional[datetime] = None


@router.post("/notes", status_code=201)
def create_note(payload: NoteIn) -> dict:
    ctx = get_context()
    now = datetime.now(timezone.utc)
    ext = "note-" + sha256_text(payload.body + (payload.title or ""))[:24]
    note = Note(
        id=ext, person_id=payload.person_id,
        title=payload.title.strip()[:200],
        body=payload.body.strip(),
        noted_at=payload.noted_at or now,
        origin=DataOrigin.LOCAL, external_id=ext,
    )
    if not ctx.store.insert_note_if_new(note):
        raise HTTPException(409, "identical note already exists")

    text = f"{note.title}. {note.body}".strip()
    mem = Memory(
        id="mem-" + ext, person_id=note.person_id, kind="note",
        source=SourceType.NOTES, origin=DataOrigin.LOCAL,
        occurred_at=note.noted_at, text=text[:8000],
        record_id=ext, source_id=ext,
        citation=f"Note — {note.noted_at.strftime('%b %d, %Y')}",
        topics=extract_topics(text),
    )
    try:
        mem.embedding = ctx.embedder.embed(mem.text)
    except Exception as e:
        log.warning("note embedding failed: %s", e)
    ctx.store.insert_memories([mem])

    if note.person_id and note.noted_at:
        record_event(ctx.store, person_id=note.person_id, kind="note",
                     source=SourceType.NOTES.value, occurred_at=note.noted_at,
                     summary=note.title or note.body[:60], record_id=ext)
        ctx.pipeline.refresh_profiles({note.person_id})
    ctx.broker.publish("note", {"id": note.id, "person_id": note.person_id})
    return {"note": note.model_dump(mode="json")}


@router.get("/notes")
def list_notes(person_id: Optional[str] = None, limit: int = 100) -> dict:
    ctx = get_context()
    if person_id:
        notes = ctx.store.list_notes_for_person(person_id, limit=limit)
    else:
        notes = ctx.store.list_notes_recent(limit=limit)
    return {"notes": [n.model_dump(mode="json") for n in notes]}


# -------------------------------------------------------------------- search
class SearchIn(BaseModel):
    query: str = Field(..., min_length=1, max_length=500)
    person_id: Optional[str] = None
    source: Optional[str] = None
    kind: Optional[str] = None
    limit: int = 20


@router.post("/search")
def search(payload: SearchIn) -> dict:
    ctx = get_context()
    filters: dict[str, Any] = {}
    if payload.person_id:
        filters["person_id"] = payload.person_id
    if payload.source:
        filters["source"] = payload.source
    if payload.kind:
        filters["kind"] = payload.kind
    evidence = hybrid_retrieve(ctx.store, ctx.embedder, payload.query,
                               filters, k=payload.limit)
    results = []
    for e in evidence:
        dto = e.memory.model_dump(mode="json")
        dto.pop("embedding", None)
        dto["score_label"] = e.label
        dto["snippet"] = e.text[:400]
        results.append(dto)
    return {"results": results, "count": len(results)}


# ----------------------------------------------------------------------- ask
class AskIn(BaseModel):
    question: str = Field(..., min_length=1, max_length=4000)
    person_id: Optional[str] = None
    history: Optional[list[dict]] = None


@router.post("/ask")
def ask(payload: AskIn) -> dict:
    ctx = get_context()
    import time
    t0 = time.time()
    pipeline = RagPipeline(ctx.store, ctx.embedder, ctx.llm)
    try:
        result = pipeline.ask(payload.question, person_id=payload.person_id,
                              history=payload.history)
    except Exception as e:
        log.error("ask failed: %s", e)
        raise HTTPException(500, f"ask failed: {type(e).__name__}")
    from circle.observability.sentry import trace_stage
    trace_stage("ask.total", int((time.time() - t0) * 1000),
                {"record_count": result.evidence_count})
    return result.to_dict()


@router.post("/ask/stream")
def ask_stream_route(payload: AskIn):
    """Same as /ask but streams tokens as the local model produces them.

    Perceived latency drops because the user sees the answer forming instead
    of waiting for the full generation on CPU.
    """
    ctx = get_context()
    pipeline = RagPipeline(ctx.store, ctx.embedder, ctx.llm)

    def gen():
        try:
            for event in pipeline.ask_stream(payload.question, payload.person_id):
                yield f"data: {json.dumps(event, default=str)}\n\n"
        except Exception as e:
            log.error("ask stream failed: %s", e)
            yield "data: " + json.dumps(
                {"type": "error", "error": f"{type(e).__name__}: {e}"[:200]}) + "\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


class PrepareIn(BaseModel):
    person_id: str
    meeting_line: str = ""


@router.post("/people/prepare")
def prepare_me(payload: PrepareIn) -> dict:
    ctx = get_context()
    pipeline = RagPipeline(ctx.store, ctx.embedder, ctx.llm)
    try:
        result = pipeline.prepare(payload.person_id, payload.meeting_line)
    except Exception as e:
        log.error("prepare failed: %s", e)
        raise HTTPException(500, f"prepare failed: {type(e).__name__}")
    return result.to_dict()


# --------------------------------------------------------------- relationship
@router.get("/relationship/{person_id}")
def relationship(person_id: str) -> dict:
    ctx = get_context()
    profile = compute_profile(ctx.store, person_id)
    if not profile:
        raise HTTPException(404, "person not found")
    person = ctx.store.get_person(person_id)
    events = ctx.store.list_relationship_events(person_id, limit=50)
    return {
        "profile": profile.model_dump(mode="json"),
        "person_name": person.display_name if person else "",
        "recent_events": events,
    }


# ------------------------------------------------------------------- identity
@router.get("/identity/suggestions")
def identity_suggestions() -> dict:
    """Pending merges, each with the evidence behind the conflict."""
    resolver = get_context().resolver
    out = []
    for doc in resolver.list_suggestions():
        try:
            doc["evidence"] = resolver.suggestion_evidence(doc)
        except Exception as e:  # never fail the whole panel on one bad row
            log.warning("suggestion evidence failed for %s: %s",
                        doc.get("id"), e)
            doc["evidence"] = {}
        out.append(doc)
    return {"suggestions": out}


class MergeIn(BaseModel):
    keep_id: str
    remove_id: str


@router.post("/identity/merge")
def identity_merge(payload: MergeIn) -> dict:
    ctx = get_context()
    out = ctx.resolver.merge_people(payload.keep_id, payload.remove_id)
    if not out.get("ok"):
        raise HTTPException(400, out.get("error", "merge failed"))
    ctx.pipeline.refresh_profiles({payload.keep_id})
    ctx.broker.publish("identity", {"action": "merge", **out})
    return out


class SuggestionResponse(BaseModel):
    suggestion_id: str
    merge: bool


@router.post("/identity/suggestion")
def identity_suggestion(payload: SuggestionResponse) -> dict:
    ctx = get_context()
    out = ctx.resolver.accept_suggestion(payload.suggestion_id, payload.merge)
    if not out.get("ok"):
        raise HTTPException(400, out.get("error", "not found"))
    if payload.merge and out.get("person_id"):
        ctx.pipeline.refresh_profiles({out["person_id"]})
    return out


# ------------------------------------------------------------------- evidence
@router.get("/memories/{memory_id}")
def get_memory(memory_id: str) -> dict:
    """Clicking a source shows the original normalized record."""
    ctx = get_context()
    mem = ctx.store.get_memory(memory_id)
    if not mem:
        raise HTTPException(404, "memory not found")
    dto = mem.model_dump(mode="json")
    dto.pop("embedding", None)
    record: dict[str, Any] = {}
    kind = mem.kind
    rid = mem.record_id
    try:
        if kind == "message" and rid:
            record = ctx.store.get_message(rid) or {}
        elif kind == "email" and rid:
            e = ctx.store.get_email(rid)
            record = e.model_dump(mode="json") if e else {}
        elif kind == "note" and rid:
            n = ctx.store.get_note(rid)
            record = n.model_dump(mode="json") if n else {}
        elif kind == "calendar" and rid:
            ev = ctx.store.get_calendar(rid)
            record = ev.model_dump(mode="json") if ev else {}
        elif kind == "voice" and rid:
            v = ctx.store.get_voice(rid)
            if v:
                record = v.model_dump(mode="json")
            else:
                # voice-note media linked to a message (not a standalone rec)
                mv = ctx.store.get_media(rid)
                record = media_dto(mv) if mv else {}
        elif kind == "image" and rid:
            mi = ctx.store.get_media(rid)
            record = media_dto(mi) if mi else {}
        elif kind == "document" and rid:
            doc = ctx.store.get_document(rid)
            record = doc.model_dump(mode="json") if doc else {}
    except Exception as e:
        log.warning("record lookup failed: %s", e)
    # Attach any real media linked to a message record so the source modal can
    # render the photo/voice note alongside the message.
    media_list: list[dict] = []
    if kind == "message" and rid:
        try:
            media_list = [media_dto(x)
                          for x in ctx.store.list_media_for_message(rid)]
        except Exception:
            media_list = []
    person = ctx.store.get_person(mem.person_id) if mem.person_id else None
    return {"memory": dto, "record": record, "media": media_list,
            "person_name": person.display_name if person else None}


# ------------------------------------------------------------------------ tts
class TtsIn(BaseModel):
    text: str = Field(..., min_length=1, max_length=4000)


@router.post("/tts")
def tts(payload: TtsIn):
    """Optional ElevenLabs voice output -- sends ONLY the answer text."""
    from circle.voice.tts import synthesize, tts_status
    if not tts_status()["enabled"]:
        raise HTTPException(400, "voice output not configured (optional feature)")
    audio = synthesize(payload.text)
    if not audio:
        raise HTTPException(502, "speech synthesis failed")
    from fastapi.responses import Response
    return Response(content=audio, media_type="audio/mpeg")

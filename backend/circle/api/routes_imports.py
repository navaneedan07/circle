"""Import job routes, rescan, settings/bootstrap, demo data, voice playback."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field

from circle.api.context import get_context
from circle.domain.models import DataOrigin, MediaAttachment, SourceType
from circle.integration import cloudfolder
from circle.security.files import FileSecurityError
from circle.security.media import media_root, resolve_served_path

log = logging.getLogger("circle.api.imports")
router = APIRouter(prefix="/api")


def media_dto(m: MediaAttachment) -> dict:
    """Serialize a media attachment for the UI (never the disk path)."""
    return {
        "id": m.id,
        "filename": m.filename,
        "kind": m.kind.value,
        "mime_type": m.mime_type,
        "size_bytes": m.size_bytes,
        "duration_seconds": m.duration_seconds,
        "transcript": m.transcript,
        "caption": m.caption,
        "language": m.language,
        "message_id": m.message_id,
        "person_id": m.person_id,
        "conversation_id": m.conversation_id,
        "occurred_at": m.occurred_at.isoformat() if m.occurred_at else None,
        "status": m.status,
        "source": m.source.value,
        "origin": m.origin.value,
        "url": f"/api/media/{m.id}" if m.id else None,
    }


def _served_path(ctx, m: MediaAttachment):
    return resolve_served_path(m.stored_path, media_root(ctx.settings.processed_dir()))


# ------------------------------------------------------------------- imports
@router.get("/imports")
def list_imports(limit: int = 100) -> dict:
    ctx = get_context()
    jobs = ctx.store.list_jobs(limit=limit)
    return {"jobs": [j.model_dump(mode="json") for j in jobs],
            "counts": ctx.store.count_jobs_by_status()}


@router.post("/imports/rescan")
def rescan() -> dict:
    ctx = get_context()
    n = ctx.watcher.rescan()
    ctx.broker.publish("sync", {"action": "rescan", "queued": n})
    return {"queued": n, "root": str(ctx.watcher.root),
            "roots": [str(p) for p in ctx.watcher.roots]}


# -------------------------------------------------------------- watch folders
class WatchFolderIn(BaseModel):
    path: str = Field(..., min_length=1, max_length=400)


def _folder_dto(ctx, path: Path) -> dict:
    """Describe a watch folder for the UI, including its cloud provider."""
    status = cloudfolder.folder_status(path)
    roots = ctx.watch_folders()
    status.update({
        "primary": str(roots[0]).lower() == str(path).lower(),
        "managed": ctx.settings.should_archive(path),
        "watched": any(str(r).lower() == str(path).lower() for r in roots),
    })
    return status


@router.get("/watch/folders")
def watch_folders() -> dict:
    ctx = get_context()
    folders = [_folder_dto(ctx, p) for p in ctx.watch_folders()]
    return {
        "folders": folders,
        "cloud_drive": cloudfolder.google_drive_install(),
        "managed_root": ctx.settings.should_archive(ctx.watcher.root),
        # Files in a folder the user owns are read in place, never moved.
        "archives_files": ctx.settings.root_is_managed(),
    }


@router.post("/watch/folders", status_code=201)
def add_watch_folder(payload: WatchFolderIn) -> dict:
    ctx = get_context()
    raw = payload.path.strip().strip('"')
    if not raw:
        raise HTTPException(400, "folder path is required")
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise HTTPException(400, "use a full path, for example G:\\My Drive\\ChatBackups")
    try:
        path = path.resolve()
    except OSError as e:
        raise HTTPException(400, f"cannot resolve folder: {e}")
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise HTTPException(400, f"cannot open folder: {e}")
    if not path.is_dir():
        raise HTTPException(400, "that path is not a folder")
    try:
        ctx.add_watch_folder(path)
    except Exception as e:
        raise HTTPException(500, f"could not watch folder: {e}")
    log.info("watch folder added: %s", path)
    return watch_folders()


@router.delete("/watch/folders")
def remove_watch_folder(path: str) -> dict:
    ctx = get_context()
    target = Path(path).expanduser()
    try:
        ctx.remove_watch_folder(target.resolve())
    except ValueError as e:
        raise HTTPException(400, str(e))
    except OSError as e:
        raise HTTPException(400, f"cannot resolve folder: {e}")
    return watch_folders()


@router.get("/watch/detect")
def detect_cloud_folders() -> dict:
    """What Circle can see: cloud clients installed, and their local roots.

    Detection only. Circle has no Google or Microsoft credentials and never
    signs in to anything on the user's behalf.
    """
    return {"cloud_drive": cloudfolder.google_drive_install()}


# ------------------------------------------------------------------ settings
@router.get("/ai/metrics")
def ai_metrics() -> dict:
    """Live AI latency metrics + tuning advice (CPU performance surface)."""
    return get_context().ai_metrics()


@router.post("/ai/warmup")
def ai_warmup() -> dict:
    """Preload models into memory so the next answer is fast."""
    ctx = get_context()
    out: dict = {}
    if hasattr(ctx.llm, "warmup"):
        out["llm"] = ctx.llm.warmup()
    if hasattr(ctx.embedder, "warmup"):
        out["embeddings"] = ctx.embedder.warmup()
    return out


@router.get("/settings")
def get_settings_route() -> dict:
    ctx = get_context()
    s = ctx.settings
    return {
        "import_root": str(ctx.watcher.root),
        "import_roots": [str(p) for p in ctx.watch_folders()],
        "watch_folders": [_folder_dto(ctx, p) for p in ctx.watch_folders()],
        "archives_files": s.root_is_managed(),
        "ollama_url": s.ollama_url,
        "ollama_model": s.ollama_model,
        "embedding_model": s.ollama_embedding_model,
        "watcher_enabled": s.watcher_enabled,
        # Reported by the running store, not the settings default: the
        # engine can differ from what .env says (migrated, or overridden).
        "storage_backend": ctx.store.engine_name(),
        "sqlite_path": str(getattr(ctx.store, "path", "") or ""),
        "sentry_enabled": s.sentry_enabled and bool(s.sentry_dsn),
        "tts_enabled": bool(s.elevenlabs_api_key),
        "user_names": ctx.store.get_setting("user_names") or [],
        "ai": ctx.ai_metrics(),
        "data_origins": {
            "api": "Official API connectors only (none configured)",
            "imported": "Files you imported into the watched folders",
            "local": "Data created inside Circle (notes)",
        },
    }


class SettingsIn(BaseModel):
    user_names: Optional[list[str]] = None
    ollama_model: Optional[str] = None


@router.post("/settings")
def update_settings(payload: SettingsIn) -> dict:
    ctx = get_context()
    if payload.user_names is not None:
        clean = [n.strip() for n in payload.user_names if n.strip()][:20]
        ctx.store.set_setting("user_names", clean)
        ctx.resolver.user_names = {n.lower() for n in clean} | {"me", "you"}
    if payload.ollama_model:
        ctx.store.set_setting("ollama_model", payload.ollama_model)
        # settings object is env-driven; runtime override:
        ctx.settings.ollama_model = payload.ollama_model
        from circle.ai.llm import reset_provider
        # keep the same instance but update model on it
        try:
            ctx.llm.settings.ollama_model = payload.ollama_model  # type: ignore
        except Exception:
            pass
    return get_settings_route()


# ----------------------------------------------------------------- bootstrap
@router.get("/bootstrap")
def bootstrap_info() -> dict:
    ctx = get_context()
    first_run = ctx.store.get_setting("onboarded") is not True
    roots = ctx.watch_folders()
    return {
        "first_run": first_run,
        "layout": [str(roots[0] / s) for s in ctx.settings.SOURCE_SUBFOLDERS],
        "roots": [str(p) for p in roots],
        "health": ctx.health,
        "cloud_drive": cloudfolder.google_drive_install(),
    }


class BootstrapIn(BaseModel):
    import_root: str = Field("", max_length=400)
    watch_folders: list[str] = Field(default_factory=list, max_length=10)
    mark_done: bool = False


@router.post("/bootstrap")
def bootstrap(payload: BootstrapIn) -> dict:
    ctx = get_context()
    if payload.import_root.strip():
        # env var wins for the process; write .env-style override file
        root = Path(payload.import_root.strip()).expanduser()
        try:
            root.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise HTTPException(400, f"cannot create folder: {e}")
        ctx.settings.import_root = str(root.resolve())
        _persist_env("IMPORT_ROOT", str(root.resolve()))
    # Rebuild the root list from scratch, then apply any extra folders the
    # user chose on the setup screen (a Drive backup folder, a USB drive).
    try:
        ctx.watcher.set_roots([ctx.settings.root_dir()], rescan=False)
        ctx.pipeline.watch_roots = list(ctx.watcher.roots)
        ctx.store.set_setting("watch_roots", [])
        for extra in payload.watch_folders:
            candidate = Path(extra.strip().strip('"')).expanduser()
            if not candidate.is_absolute():
                continue
            candidate.mkdir(parents=True, exist_ok=True)
            ctx.add_watch_folder(candidate.resolve())
    except Exception as e:
        log.warning("applying watch folders failed: %s", e)
    ctx.watcher._ensure_layout()
    try:
        ctx.watcher.rescan()
    except Exception as e:
        log.warning("rescan after root change failed: %s", e)
    if payload.mark_done:
        ctx.store.set_setting("onboarded", True)
    return {"ok": True, "root": str(ctx.watcher.root),
            "roots": [str(p) for p in ctx.watch_folders()],
            "first_run": ctx.store.get_setting("onboarded") is not True}


def _persist_env(key: str, value: str) -> None:
    """Persist a setting to the local .env (never committed)."""
    try:
        env_path = Path.cwd() / ".env"
        lines = []
        if env_path.exists():
            lines = env_path.read_text(encoding="utf-8").splitlines()
        prefix = key + "="
        replaced = False
        for i, line in enumerate(lines):
            if line.strip().startswith(prefix):
                lines[i] = f"{key}={value}"
                replaced = True
        if not replaced:
            lines.append(f"{key}={value}")
        env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except Exception as e:
        log.warning("could not persist env setting: %s", e)


# ----------------------------------------------------------------- demo data
@router.post("/demo/generate")
def demo_generate() -> dict:
    from circle.demo.generate import generate_demo_data
    ctx = get_context()
    out = generate_demo_data(ctx)
    return out


@router.get("/demo/status")
def demo_status() -> dict:
    ctx = get_context()
    return {"demo": ctx.store.get_setting("demo_generated") is True}


# --------------------------------------------------------------------- voice
@router.get("/voice/{rec_id}/audio")
def voice_audio(rec_id: str):
    """Serve the explicitly imported recording from the processed folder."""
    ctx = get_context()
    rec = ctx.store.get_voice(rec_id)
    if not rec:
        raise HTTPException(404, "recording not found")
    safe_name = Path(rec.filename).name
    for root in (ctx.settings.processed_dir(), ctx.settings.root_dir()):
        if not root.exists():
            continue
        for p in root.rglob(safe_name):
            if p.is_file():
                media = {".wav": "audio/wav", ".mp3": "audio/mpeg",
                         ".m4a": "audio/mp4", ".ogg": "audio/ogg",
                         ".webm": "audio/webm"}.get(p.suffix.lower(),
                                                    "application/octet-stream")
                return FileResponse(p, media_type=media)
    raise HTTPException(404, "audio file no longer on disk")


@router.get("/voice")
def list_voice(person_id: Optional[str] = None, limit: int = 50) -> dict:
    ctx = get_context()
    if person_id:
        recs = ctx.store.list_voice_for_person(person_id, limit=limit)
    else:
        recs = ctx.store.list_voice_recent(limit=limit)
    return {"recordings": [r.model_dump(mode="json") for r in recs]}


class VoiceAssociateIn(BaseModel):
    recording_id: str
    person_id: Optional[str] = None


# --------------------------------------------------------------------- media
@router.get("/people/{person_id}/media")
def person_media(person_id: str, limit: int = 200) -> dict:
    ctx = get_context()
    if not ctx.store.get_person(person_id):
        raise HTTPException(404, "person not found")
    recs = ctx.store.list_media_for_person(person_id, limit=limit)
    return {"media": [media_dto(m) for m in recs]}


@router.get("/media")
def list_media(person_id: Optional[str] = None, limit: int = 200) -> dict:
    ctx = get_context()
    recs = (ctx.store.list_media_for_person(person_id, limit=limit)
            if person_id else ctx.store.list_media(limit=limit))
    return {"media": [media_dto(m) for m in recs]}


@router.get("/media/{media_id}/info")
def media_info(media_id: str) -> dict:
    ctx = get_context()
    m = ctx.store.get_media(media_id)
    if not m:
        raise HTTPException(404, "media not found")
    return {"media": media_dto(m)}


@router.get("/media/{media_id}")
def media_file(media_id: str):
    """Serve an attachment from the local media store (path is never supplied
    by the client; it is resolved from the stored record and escape-checked)."""
    ctx = get_context()
    m = ctx.store.get_media(media_id)
    if not m:
        raise HTTPException(404, "media not found")
    try:
        path = _served_path(ctx, m)
    except FileSecurityError:
        raise HTTPException(404, "media file is no longer on disk")
    return FileResponse(path, media_type=m.mime_type or "application/octet-stream")


class DescribeIn(BaseModel):
    prompt: str = Field("", max_length=500)


@router.post("/media/{media_id}/describe")
def media_describe(media_id: str, payload: DescribeIn) -> dict:
    """Caption a photo locally (Gemma vision) and index it for retrieval."""
    ctx = get_context()
    m = ctx.store.get_media(media_id)
    if not m:
        raise HTTPException(404, "media not found")
    if m.kind.value != "image":
        raise HTTPException(400, "only images can be described")
    if not hasattr(ctx.llm, "describe_image"):
        raise HTTPException(400, "no local vision model available")
    if m.size_bytes > 12 * 1024 * 1024:
        raise HTTPException(413, "image too large to describe")
    try:
        path = _served_path(ctx, m)
    except FileSecurityError:
        raise HTTPException(404, "media file is no longer on disk")
    prompt = payload.prompt.strip() or (
        "Describe this photo in one or two short factual sentences. "
        "Do not guess identities or emotional states.")
    try:
        caption = ctx.llm.describe_image(path, prompt)
    except Exception as e:
        log.warning("image describe failed: %s", e)
        raise HTTPException(502, "local vision model failed")
    m.caption = (caption or "").strip()[:1000]
    ctx.store.update_media(m)
    ctx.pipeline._store_media_memory(m)
    ctx.broker.publish("media", {"id": m.id, "caption": True})
    return {"ok": True, "media": media_dto(m)}


@router.post("/voice/associate")
def voice_associate(payload: VoiceAssociateIn) -> dict:
    """User confirms/changes the person for a recording (spec §59)."""
    ctx = get_context()
    rec = ctx.store.get_voice(payload.recording_id)
    if not rec:
        raise HTTPException(404, "recording not found")
    rec.person_id = payload.person_id
    ctx.store.update_voice(rec)
    if payload.person_id and rec.recorded_at and rec.transcript:
        from circle.relationship.metrics import record_event
        record_event(ctx.store, person_id=payload.person_id, kind="voice",
                     source=SourceType.VOICE.value, occurred_at=rec.recorded_at,
                     summary=rec.transcript[:80], record_id=rec.id or "")
        # attach memory to person
        ctx.store.update_memories_person(rec.id, payload.person_id)
        ctx.pipeline.refresh_profiles({payload.person_id})
    ctx.broker.publish("voice", {"id": rec.id, "person_id": payload.person_id})
    return {"ok": True}

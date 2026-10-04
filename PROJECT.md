# Circle — project handoff

Written for an engineer or AI picking this up cold. It covers what the project
is, how it is actually put together, what is verified, and the traps that have
already cost real time here.

Read this before changing anything. Several of the notes below describe bugs
that were shipped and then found the hard way — they are cheap to read now and
expensive to rediscover.

---

> ## ⚠️ Current state (read first)
>
> Circle has been **rewritten as an Electron desktop app** under `app/`:
>
> - **Electron + Node backend**, one window. The HTTP API runs in Electron's
>   main process; there is no separate backend binary and no browser shell.
> - **SQLite only.** MongoDB has been removed from the Python backend too
>   (`mongo.py`, `pymongo`, the migration script and the parity tests are
>   gone). There is no second engine and no query-translation layer in the new
>   app.
> - **One folder, chosen by the user.** Circle watches exactly one folder and
>   creates nothing inside it. There are no default or idle folders.
> - **`node:sqlite`** (built into Node 22.5+/Electron) with FTS5, so there is no
>   native module to rebuild.
> - **Distribution changed.** An Electron installer is over GitHub's 100 MB
>   per-file limit, so it is hosted as a **GitHub Release asset**, not committed
>   to the repo. The Render page only links to it.
>
> See `BUILDING.md` for how to build and ship it. Sections 1–13 below document
> the **legacy Python backend** (`backend/`), which is retained for reference
> and is not shipped. Section 4's "two engines" warning no longer applies.
>
> A real bug fixed in the rewrite: the old watcher's only guard against
> re-ingesting Circle's own files was a `startsWith` prefix check, which let
> the archive database be picked up and quarantined (`backend/quarantine/
> circle.db` is the evidence). The new watcher excludes every path Circle owns
> by resolved-path containment.

---

## 1. What Circle is

A **local-first relationship intelligence desktop app**. It reads the exports a
person already has — chat logs, email, calendar, voice notes, documents — works
out who is who, and answers questions about their relationships using a local
LLM.

The hard constraint behind every design decision: **the data never leaves the
machine.** No account, no cloud service, no upload. Answers come from a local
Gemma via Ollama.

It was originally a web app with a hosted interface reached through a tunnel.
That was abandoned. It is now **one desktop application** that serves its own
interface from the same local process that holds the archive. There is no
hosted Circle and no tunnel; if you find yourself adding one, something has gone
wrong.

### The shape of it

```
exported files  ──▶ watcher ──▶ parsers ──▶ identity ��─▶ store
                                (per source)   resolution
                                                         │
                              ┌──────────────────────────┴───────────┐
                              ▼                                      ▼
                    hybrid retrieval (FTS + vectors)      relationship metrics
                              │                                      │
                              └──────────────▶ local Gemma ◀─────────┘
                                                    │
                                                    ▼
                                            answer + evidence
```

---

## 2. Repository layout

```
backend/
  circle/
    ai/           rag.py (retrieval+fusion), llm.py, embeddings.py, prompts.py
    api/          main entry, auth, context wiring, HTTP routes
    connectors/   EMPTY — no code, no references
    demo/         synthetic data generator (dev/demo)
    domain/       models.py — all pydantic models
    identity/     resolver.py — decides which records are the same person
    ingestion/    pipeline.py, watcher.py, media.py
    integration/  cloudfolder.py — Drive/OneDrive placeholder detection
    observability/ sentry.py (off by default)
    parsers/      one module per export source + generic.py
    realtime/     broker.py
    relationship/ metrics.py — profile building from events
    repository/   mongo.py, sqlite.py, sqlite_core.py, serde.py, factory.py, base.py
    search/       EMPTY — no code, no references
    security/     media.py, files.py — zip-slip, path traversal guards
    voice/        stt.py, tts.py
  scripts/        doctor.py, launchers  (legacy Python backend; the shipped
                  application is the Electron build in app/)
  tests/          19 files, 551 tests
frontend/
  src/            React + Vite + TypeScript + Tailwind
  scripts/        visual-check.mjs, contrast-audit.mjs, shot.mjs, overflow-find.mjs
site/             the public download page (committed static HTML)
render.yaml       public site blueprint
```

Sizes: backend 59 files / ~11.8k lines, frontend 19 files / ~4.5k lines.

---

## 3. Stack

| Layer | Choice | Notes |
|---|---|---|
| API | FastAPI + uvicorn | Python 3.11+ |
| Store | SQLite (default) | one file; MongoDB still selectable |
| Retrieval | SQLite FTS5 + numpy cosine | no external vector DB |
| LLM | Ollama / `gemma3:4b` | local; must be running |
| Embeddings | Ollama / `nomic-embed-text` | 768-dim |
| STT | faster-whisper | **excluded from the packaged build** — see §7 |
| Frontend | React 18, Vite, TS, Tailwind, react-router | |
| Packaging | PyInstaller one-file | |
| Hosting | Render static site | download page only |

---

## 4. Storage — the part most likely to surprise you

### Two engines, one interface

`MongoStore` and `SQLiteStore` each have **100 methods and must stay
interchangeable**. `repository/factory.py` picks one from `STORAGE_BACKEND`
(default `sqlite`). The whole test suite runs against both:

```bash
cd backend
.venv/Scripts/python -m pytest -q                 # SQLite (default)
STORAGE_BACKEND=mongo .venv/Scripts/python -m pytest -q
```

**Any change to one store must be made to the other.** A divergence that
shipped once: Mongo's `interaction_totals(since=None)` applies no upper time
bound, while the SQLite port clamped to now — so 83 future-dated events went
missing from "who do I talk to most". There is now a test for it, but that is
the shape of bug this pair of engines produces.

### How SQLite stores documents

`sqlite_core.py` keeps a `doc` JSON column holding the **whole document**, plus
real columns for fields that get filtered, joined or sorted on. This keeps the
pydantic round-trip byte-identical to Mongo while making hot paths indexable.
Fields not listed in `COLUMNS` are filtered with `json_extract` — correct, just
slower.

Embeddings are a BLOB, not JSON text (JSON made them ~10× larger and had to be
parsed on every row read).

### Mongo query translation

Callers still write Mongo-style filters (`{"occurred_at": {"$gte": iso}}`).
`sqlite_core.py` translates the subset Circle actually uses. **Unsupported
operators raise rather than being ignored** — this is deliberate. A query that
returns nothing fails a test; a query that silently returns the wrong rows is a
bug nobody finds.

### The `.db` rule

There must be **no raw collection access outside `repository/`**. 24 call sites
used to reach through the store into `store.db.messages.find_one(...)`. They are
now store methods. Keep it that way: it is what makes the SQLite port possible.

---

## 5. Timestamps — a real trap

Timestamps are stored as **text** and compared as strings. Two problems, both
hit already:

1. **`Z` sorts after `+`.** Pydantic emits `...Z`; `isoformat()` emits
   `...+00:00`. Since `Z` (0x5A) > `+` (0x2B), mixing them silently misorders
   every time window. All writes are now canonicalised on the way in.
2. **`update_many` counts.** Mongo's `modified_count` counts rows that actually
   changed; a naive SQL `UPDATE` returns rows *matched*. Parity required
   handling this explicitly.

If you add a timestamp field, canonicalise it in `sqlite_core.py`, don't write
raw `isoformat()` output.

---

## 6. First run — do not invent directories

`Settings.root_dir()` returns `None` until the user picks a folder. Circle
creates **no** watch folder and **no** subfolders inside the folder it reads.

This is deliberate and recent. The launcher used to create a watched folder
before anyone had been asked, and FirstRun prefilled it — so the app answered a
question the user was never asked, and a dozen empty `whatsapp/`, `telegram/`,
`voice/` directories appeared inside a Google Drive backup, which synced
straight back to Google.

Circle's own working files live under `work_dir()`, which is separate from the
folder being read, and must stay that way: the folder being read belongs to the
user, the other one is ours. One install lives in **one** folder, so backup and
deletion stay single operations (a test pins this — it once shipped split across
two locations, so "delete Circle" left half behind).

---

## 7. The packaged build — 40 MB, and why

`cd backend && .venv/Scripts/python -m PyInstaller circle.spec --noconfirm`
→ `dist/Circle.exe`.

**Do not add dependencies back without checking the size.**

Git hosts reject any single file over **100 MB** with no way to override it.
The build was originally **104 MB** — over the limit — so the release steps
documented at the time *could never work*, and nothing reported it: the download
page simply linked to a file that was not there. This cost real time.

The fix was to exclude the speech-to-text stack (`faster_whisper`,
`ctranslate2`, `onnxruntime`, `av`, `tokenizers`, `hf_xet`) — ~61 MB that exists
only to transcribe audio. Now **40.1 MB**.

Trade-off: **voice-note transcription does not work in the packaged app.**
Circle degrades honestly — health reports `stt: available false` with the reason,
and a voice recording is marked failed rather than crashing the import.
Everything else is unaffected. From source, `requirements.txt` still installs
faster-whisper.

> `faster-whisper` hard-imports PyAV, so PyAV cannot simply be dropped while
> keeping whisper. If you need packaged voice transcription you need a
> fundamentally different distribution (split installer, or an on-demand
> component), not a bigger one-file build.

### Publishing

The application is the Electron build in `app/`, so releases are published from
there with the GitHub CLI:

```bash
cd app
npm run release:check   # verify only; publishes nothing
npm run release         # publish to GitHub Releases
```

This replaces an earlier `backend/scripts/publish_release.py`, which staged the
installer into `site/downloads` and was deleted along with the Python backend.

The installer cannot live in git at all: it is ~277 MB, well past the 100 MB
per-file limit, so the old script's "stage it next to the page" approach could
only ever produce a link that 404s. A GitHub Release asset is the correct home.

`npm run release` refuses to publish when the version disagrees between
`package.json`, the app and the website; when the working tree is dirty or a
commit is unpushed; when the repository is **private**, because a release there
is invisible and the public download link 404s for everyone else; and it fetches
the download URL unauthenticated afterwards to prove the asset is really
reachable. The failure this guards against is silent: the release succeeds, the
button works, and the download still 404s.

Requires `gh auth login`.

---

## 8. Access control

`api/auth.py`. Loopback callers skip the access key. `ACCESS_KEY` is for when
the API is deliberately exposed beyond the machine.

`_is_public()` returns True for **all non-`/api/` paths** — the SPA shell has to
load before the key is known. The exception is `/api/auth/status`, which must
report an honest `key_accepted`. Do not "fix" this without reading the 16
regression tests in `test_access_control.py`; an earlier fix here 401'd every
deep link and served JSON where HTML was expected.

Middleware order in `main.py` matters: `access_key_middleware` is registered
**before** `add_middleware(CORSMiddleware)`, because Starlette puts newly added
middleware at the front, so last-registered runs first. Get this backwards and
`OPTIONS` preflight 401s.

---

## 9. Running it

```bash
# backend (dev)
cd backend
.venv/Scripts/python -m circle.main          # or scripts/serve.sh

# frontend
cd frontend && npm install && npm run dev    # :5173, proxies to :8000

# diagnostics — run this when something misbehaves
cd backend && .venv/Scripts/python scripts/doctor.py
```

Prerequisites: Ollama running with `gemma3:4b` and `nomic-embed-text`. MongoDB
only if `STORAGE_BACKEND=mongo`.

Migrating the old archive: `scripts/migrate_to_sqlite.py` (354.8k documents in
~310s; resumable).

---

## 10. Testing

```bash
cd backend
.venv/Scripts/python -m pytest -q                          # 551 pass
STORAGE_BACKEND=mongo .venv/Scripts/python -m pytest -q    # must also pass

cd frontend
node scripts/contrast-audit.mjs      # colour contrast
node .hosted-check.mjs               # off-machine gate
```

`tests/store_probe.py` is an engine-agnostic accessor — use it in tests instead
of touching `store.db` directly.

`tests/test_annotations.py` exists because `from __future__ import annotations`
makes annotations lazy: a name used in an annotation but never imported passes
every ordinary test and only breaks when something resolves type hints.
`Settings.root_dir` shipped with `Optional` used and never imported.

---

## 11. Verified state

Re-check these before trusting them; they were true at the last commit
(`e9aa2f8`).

- **551 backend tests pass** (1 skipped) against **both** storage engines.
- **100 store methods** each on `MongoStore` and `SQLiteStore` — verified equal.
- Packaged exe is **40.1 MB**.
- 354,800 documents migrated Mongo → SQLite; both engines return identical
  counts, rankings and time-window totals.
- The packaged exe serves the real archive: 337 people, 120,864 messages,
  123,330 memories, and answers *"who do I talk to most"* with correct local
  model output.
- `render.yaml` validates against Render's official schema.
- Visual sweep: no overflow and no console errors across routes × themes ×
  viewports; contrast audit passes all pairs.

`python -m circle.main` and `scripts/serve.sh` both start the server. The
launcher equivalent is `uvicorn circle.main:app --host 127.0.0.1 --port <port>`.

---

## 12. Open items

**The public site has never been redeployed.** `render.yaml` points at `site/`,
but Render is still serving an older configuration. **A Render blueprint is not
re-read on every push** — it must be reapplied in the dashboard. Until then the
site serves the previous config and a committed change appears to have done
nothing. The binary *is* committed (`site/downloads/`, 40.1 MB), so once the
blueprint is reapplied the download works.

Other things worth knowing:

- `circle/search/` and `circle/connectors/` are empty packages with no
  references. Deletable.
- `get_processed_by_path` is defined in the stores and never called.
- The repo is **private**; release assets would need auth. The binary is
  committed to the repo instead, which works only because it is under 100 MB.
- The exe is unsigned, so Windows SmartScreen warns on first run.
- `STT` is unavailable in the packaged build by design (§7).

---

## 13. Things that will waste your time if you don't know them

1. **Git cannot hold a file over 100 MB.** Check the size before writing docs
   that say "commit the binary".
2. **Render blueprints do not apply on push.** Reapply in the dashboard.
3. **`STORAGE_BACKEND` changes which store the tests run against.** A change
   that passes on one engine may silently break the other.
4. **Mongo and SQLite stores must stay in lockstep.** 100 methods each.
5. **Text timestamps sort lexically.** `Z` vs `+00:00` is a live footgun.
6. **`code_search` (ripgrep) is broken in this environment.** Use `read_files`
   or terminal `grep`.
7. **Launching the app blocks and looks like a hang** — it loads models and
   scans folders first. Poll `/api/health` in a separate command.
8. **Chrome is at `~/AppData/Local/Google/Chrome/Application/chrome.exe`,** not
   Program Files. Headless CDP drives the visual scripts.
9. **Annotations are lazy** — missing imports hide until type hints resolve.
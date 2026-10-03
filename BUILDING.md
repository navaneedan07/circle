# Building Circle

Two outputs come out of this repo: the desktop app and the download page.
Neither needs a server.

## The desktop app

One self-contained executable. No installer, no runtime to install, no
database to provision.

```bash
# 1. Build the interface (the app bundles the compiled output)
cd frontend && npm ci && npm run build && cd ..

# 2. Package everything into backend/dist/Circle.exe
cd backend
.venv/Scripts/python -m pip install -r requirements.txt pyinstaller
.venv/Scripts/python -m PyInstaller circle.spec --noconfirm
```

The result is `backend/dist/Circle.exe` (about 105 MB; most of it is the
Python runtime and the local-model libraries).

To try it without packaging:

```bash
cd backend && .venv/Scripts/python desktop_entry.py
```

### What the build needs

- Python 3.11+ with a working `.venv` (see README §9)
- Node 20+ for the frontend build
- A built `frontend/dist` — the spec bundles it, and skips it silently if it
  is missing, which produces an app that starts but serves no interface

### Verifying a build

A packaged build can fail in ways the source tree cannot: imports that only
resolve from a real filesystem, and assets that unpack somewhere other than
expected. So check the binary itself, not the source.

```bash
cd backend
CIRCLE_NO_BROWSER=1 ./dist/Circle.exe
```

Then, in another terminal:

```bash
curl -s http://127.0.0.1:8000/api/health     # {"status":"ok", ... "engine":"sqlite"}
curl -sI http://127.0.0.1:8000/              # 200 text/html
curl -sI http://127.0.0.1:8000/settings      # 200 text/html (SPA deep link)
```

Two failures worth knowing, both found this way:

- `Error loading ASGI app. Could not import module "circle.main"` — uvicorn was
  given an import string, which a frozen build cannot resolve. `desktop.py`
  imports the app object instead.
- A 404 on `/` with a working API — the frontend unpacked somewhere else. A
  one-file build extracts to `sys._MEIPASS`, not next to the `.exe`.

The console window is deliberate: it is where a startup failure is visible.

## The download page

`site/` is committed HTML with no build step. Render publishes it directly
(see `render.yaml`). The application itself is not deployed there: it serves
its own interface, and an off-machine copy of that interface would have no
backend behind it.

To publish a release, run the checker. It refuses to stage anything that would
404, and it is the reason a broken release is caught before it is pushed
rather than after:

```bash
python backend/scripts/publish_release.py --check   # verify only
python backend/scripts/publish_release.py          # stage into site/downloads
```

It verifies that the build is under the host's 100 MB per-file limit, that the
filename carries the version, that `site/index.html` and
`frontend/src/version.ts` agree on version and size, and that the stated size
matches the actual build. Then it prints the `git add -f` to run, because
`site/downloads/` is gitignored.

**The size limit is a wall, not a guideline.** Git hosts reject any single file
over 100 MB outright, so an oversized build cannot be committed and the
download link 404s with no error anywhere to explain why. This is why the
packaged build excludes the speech-to-text libraries (see below): with them it
was 104 MB, and zipping only reached 103 MB because it was already compressed.

### Voice notes in the packaged app

The packaged build deliberately leaves out `faster-whisper`, `ctranslate2`,
`onnxruntime` and PyAV — about 61 MB that only exist to transcribe audio. This
takes the download from 104 MB to 40 MB.

Circle already handles their absence rather than failing: the health check
reports `stt: available false` with the reason, and a voice recording is
marked failed with a clear message instead of crashing the import. Everything
else — messages, email, calendar, documents, and all questions — works exactly
the same. To transcribe voice notes, run the app from source with
`pip install -r requirements.txt`, which includes `faster-whisper`.

**A commit alone does not change the deployed site.** Render blueprints are
not re-read on every push, so after changing `render.yaml` (or switching what
is published) you must open the service in the dashboard and reapply the
blueprint. Until you do, the site keeps serving the configuration it was last
given — which is the usual reason a change appears not to have worked.

## The archive

SQLite, by default: one file at `<data folder>/circle.db`. Copying that file is
a backup; deleting it forgets everything. The data folder is
`Documents/Circle` for a packaged build, or `backend/circle-archive` when
running from source.

Set `STORAGE_BACKEND=mongo` to use MongoDB instead — still supported, and the
migration script moves an existing archive across:

```bash
cd backend
.venv/Scripts/python scripts/migrate_to_sqlite.py --dry-run
.venv/Scripts/python scripts/migrate_to_sqlite.py
```

The migration is idempotent (documents are written by id), so an interrupted
run picks up where it stopped rather than starting over.
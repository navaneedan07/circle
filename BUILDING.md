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
(see `render.yaml`).

To publish a release, copy the built executable in and update the link:

```bash
mkdir -p site/downloads
cp backend/dist/Circle.exe site/downloads/Circle-0.1.0-windows-x64.exe
```

Then bump the version in two places in `site/index.html` — the download
filename and the stated size. Both are written by hand on purpose: a version
that only appears in one of them produces a page offering a stale binary under
a fresh name, or a size that no longer matches what people download.

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
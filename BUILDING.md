# Building Circle

Two outputs come out of this repo: the desktop app and the download page.
Neither needs a server.

> **Note on history.** An earlier version of Circle was a Python/FastAPI app
> packaged with PyInstaller into a ~40 MB one-file executable that opened in
> a browser. It has been replaced by an **Electron desktop app** with a Node
> backend and a single SQLite file. MongoDB has been removed entirely. The
> Python tree under `backend/` is legacy and is not part of the shipped app.

## The desktop app

One Electron application. No installer wizard required, no separate backend
process to run, no database to provision. The backend runs inside Electron's
main process (which is Node), so there is no second binary to ship.

```bash
# 1. Install dependencies
cd app && npm install

# 2. Build the interface (the app bundles the compiled output)
npm run build:renderer

# 3. Typecheck and bundle the main process
npm run build

# 4. Run it
npm start
```

To make the distributable:

```bash
cd app
CSC_KEY_PASSWORD=<cert-password> npm run dist
# -> app/release/Circle-<version>-windows-x64.exe   (signed NSIS installer)
# -> app/release/Circle-<version>-windows-x64.zip   (portable)
```

The `.exe` is the signed installer; the `.zip` is the same app as a portable
folder (no install step). Both are large (~280 MB / ~360 MB) because they carry
the Chromium runtime.

## Code signing

Signing is configured in `package.json` under `win.signtoolOptions`, using
`certs/circle-dev.pfx`. `certs/` is **gitignored** — a signing key must never be
committed. The password is read from `CSC_KEY_PASSWORD`, not stored in the repo.

```bash
CSC_KEY_PASSWORD=<password> npm run dist
```

### The certificate in this repo is self-signed

`certs/circle-dev.pfx` was generated with `New-SelfSignedCertificate`. It
produces a **valid signature** (verify with `Get-AuthenticodeSignature`; the
signer and an RFC3161 timestamp are present) but the chain ends in an untrusted
root, so Windows shows the publisher as unknown and SmartScreen still warns.
To remove that warning you must replace it with a **commercial code-signing
certificate** (OV/EV) — a credential only you can buy. See `certs/README.md`.

### Windows symlink privilege (fresh machines)

`electron-builder` fetches the `winCodeSign` package, which contains macOS
dylib *symlinks*. Windows cannot create symlinks without **Settings → System →
For developers → Developer Mode** (or an elevated shell), so a first build can
fail with:

```
Cannot create symbolic link : A required privilege is not held by the client
  ...\winCodeSign\...\darwin\10.12\lib\libcrypto.dylib
```

Fix it by enabling Developer Mode, then delete
`%LOCALAPPDATA%\electron-builder\Cache\winCodeSign` so it re-extracts cleanly.
The needed Windows tools (`rcedit`, `signtool`) can also be extracted manually
with the `darwin` folder excluded if you would rather not enable Developer Mode.

### What the build needs

- Node 22.5+ (the app uses the built-in `node:sqlite`; no native module to
  compile)
- A built `frontend/dist` — step 2 produces it. Without it the app starts but
  serves no interface. `npm run dist` runs `build:renderer` itself, so the
  installer can never pick up a stale interface from an earlier build.

### Verifying a build

A packaged build can fail in ways the source tree cannot, so check the binary
itself, not the source.

```bash
cd app
npm run build
npm start
```

Then confirm the window opens, the prerequisite screen reports the local model
as ready, and `Documents/Circle/circle.db` is created after choosing a folder.

## Distribution — read this before cutting a release

**Do not commit the installer to the repo.** Git hosts reject any single file
over **100 MB**, and an Electron installer is comfortably past that (it carries
the Chromium runtime). The old build fit because it was a 40 MB PyInstaller
one-file executable; that is no longer true.

The current flow, automated:

```bash
cd app
npm run release:check     # verify only; publishes nothing
npm run release           # create/update the GitHub Release and upload the assets
```

One-time setup, using the [GitHub CLI](https://cli.github.com):

```bash
winget install --id GitHub.cli -e      # Windows
gh auth login
```

A new terminal is needed after installing, because the `PATH` is read at
startup.

`npm run release` publishes the installer as a **GitHub Release asset** —
Releases allow large files and are not subject to the 100 MB per-file git limit
that makes committing it impossible. It then verifies the result, because the
failure mode here is silent:

- the version must agree between `package.json`, `frontend/src/version.ts` and
  the download link on the site
- the working tree must be clean and the commit pushed, or the tag would point
  at code nobody has
- **the repository must be public.** A release on a private repository is
  invisible to visitors, so the download button 404s for anyone not signed in.
  The script refuses to publish, and says why, unless you pass
  `--allow-private`.
- afterwards it fetches the download URL without signing in and requires a 200

That last check is the point. A release can succeed, the button can be live,
and the download can still 404 — with no error anywhere. Adding the zip as well
is `node scripts/publish-release.mjs --publish --zip`.

`site/` is committed static HTML with no build step; Render publishes it
directly (see `render.yaml`). The application is not deployed there — it serves
its own interface from the local process. The page only hands over the file.
Check that Render's **Publish Path** is `./site`: serving `frontend/dist`
deploys the app's own interface, which on a public host has no backend behind
it and can only render its "no Circle behind it" page.

**A commit alone does not change the deployed site.** Render blueprints are not
re-read on every push, so after changing `render.yaml` you must open the service
in the dashboard and reapply the blueprint.

## The archive

SQLite, and only SQLite: one file at `<data folder>/circle.db`. On Windows the
data folder is `Documents\Circle`. Copying that file is a backup; deleting it
forgets everything.

Circle's own files (the archive, `media/`, `processed/`, `failed/`, `tmp/`) live
in that one folder. **The folder you ask Circle to read is separate and belongs
to you**: Circle reads it in place and creates nothing inside it. One install
lives in one folder, so backing up or deleting Circle stays a single operation.

## Local model

Answering needs a local model. On first run the app checks for Ollama and the
two models (`gemma3:4b`, `nomic-embed-text`) and offers one-click setup: it
downloads Ollama, runs its installer, starts it, and pulls both models. It only
fetches public installer and model artifacts — no user data is sent anywhere.

To install by hand instead:

```bash
ollama pull gemma3:4b
ollama pull nomic-embed-text
```

## Legacy: the Python backend

`backend/` still contains the original Python/FastAPI implementation and its
test suite (549 tests, SQLite-only). It is retained for reference and is not
shipped. If you work in it:

```bash
cd backend
.venv/Scripts/python -m pytest -q
.venv/Scripts/python scripts/doctor.py
```

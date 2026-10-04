/**
 * Publish a Circle release to GitHub Releases.
 *
 * This replaces an older script that staged the installer into the website's
 * own folder. That approach could not work: the installer is ~290 MB, which is
 * past git hosts' 100 MB per-file limit, so the download link pointed at a
 * file that could never be committed and would 404 forever. A release asset is
 * the correct home for a binary this size -- and the one thing that can still
 * silently fail is publishing to a repository nobody else can see.
 *
 * So the checks are the point:
 *
 *   - the artifact exists, and its filename carries the version
 *   - package.json, the app's own version file and the website all agree
 *   - git is authenticated, the tree is clean, and the commit is pushed
 *   - the repository is PUBLIC, because a release on a private repository
 *     404s for every visitor to the download page
 *   - after uploading, the public URL is fetched unauthenticated and must
 *     answer 200. A release that cannot be downloaded is not a release.
 *
 * Usage:
 *   node scripts/publish-release.mjs                  # check only (default)
 *   node scripts/publish-release.mjs --publish        # actually publish
 *   node scripts/publish-release.mjs --publish --allow-private
 *
 * Requires the GitHub CLI (https://cli.github.com) and `gh auth login`.
 */
import { execFileSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const appDir = path.resolve(here, "..");
const repoRoot = path.resolve(appDir, "..");
const releaseDir = path.join(appDir, "release");

const args = process.argv.slice(2);
const has = (flag) => args.includes(flag);
const PUBLISH = has("--publish");
const ALLOW_PRIVATE = has("--allow-private");
const INCLUDE_ZIP = has("--zip");

const problems = [];
const warnings = [];

const ok = (msg) => console.log(`  ok    ${msg}`);
const warn = (msg) => {
  warnings.push(msg);
  console.log(`  warn  ${msg}`);
};
const bad = (msg) => {
  problems.push(msg);
  console.log(`  FAIL  ${msg}`);
};
const step = (msg) => console.log(`\n${msg}`);

function sh(command, commandArgs, { allowFail = false } = {}) {
  try {
    return execFileSync(command, commandArgs, { encoding: "utf-8", stdio: ["ignore", "pipe", "pipe"] }).trim();
  } catch (err) {
    if (allowFail) return null;
    const detail = err.stderr ? String(err.stderr).trim().split("\n").slice(-3).join(" ") : err.message;
    throw new Error(`${command} failed: ${detail}`);
  }
}

const readJson = (file) => JSON.parse(fs.readFileSync(file, "utf-8"));
const read = (file) => fs.readFileSync(file, "utf-8");

// ---------------------------------------------------------------- version

step("Version agreement");
const pkg = readJson(path.join(appDir, "package.json"));
const version = pkg.version;
const tag = `v${version}`;
const assetName = `Circle-${version}-windows-x64.exe`;
ok(`package.json version is ${version}`);

const versionTs = read(path.join(repoRoot, "frontend", "src", "version.ts"));
const appVersion = versionTs.match(/APP_VERSION\s*=\s*"([^"]+)"/)?.[1];
if (appVersion !== version) {
  bad(`frontend/src/version.ts says ${appVersion}, package.json says ${version}. The in-app download link would disagree with the release.`);
} else {
  ok("the application agrees on the version");
}

// The filename carries the version so a cached page cannot serve an old binary
// under a new name. This is the same reason the name is versioned.
if (!assetName.includes(version)) {
  bad(`asset name ${assetName} does not carry the version`);
} else {
  ok(`asset name is versioned: ${assetName}`);
}

// ---------------------------------------------------------------- artifact

step("Artifact");
const exePath = path.join(releaseDir, assetName);
if (!fs.existsSync(exePath)) {
  bad(`no build at ${exePath}\n        Build it first:  cd app && CSC_KEY_PASSWORD=<pass> npm run dist`);
} else {
  // `.size`, not `.st_size`: Node 25 dropped the `st_*` aliases from Stats, so
  // the old spelling silently yields undefined here -- and `undefined / n` is
  // NaN, which would sail through every size comparison below.
  const sizeMb = fs.statSync(exePath).size / 1048576;
  if (!Number.isFinite(sizeMb)) {
    bad(`could not read the size of ${assetName}`);
  }
  ok(`found ${assetName} (${sizeMb.toFixed(1)} MB)`);
  // Past this point GitHub stores it as a release asset, not in git, so the
  // per-file git limit does not apply -- but a 2 GB asset cap does.
  if (sizeMb > 2000) {
    bad(`${sizeMb.toFixed(1)} MB exceeds GitHub's 2 GB release-asset limit`);
  }
}

const zipPath = path.join(releaseDir, `Circle-${version}-windows-x64.zip`);
const haveZip = fs.existsSync(zipPath);
if (INCLUDE_ZIP && !haveZip) {
  warn(`--zip requested but ${path.basename(zipPath)} is not present`);
}

// ---------------------------------------------------------------- website

step("Website");
const siteHtml = read(path.join(repoRoot, "site", "index.html"));
const releaseUrl = siteHtml.match(/https:\/\/github\.com\/[^"']+\/releases\/download\/[^"']+/)?.[0];
if (!releaseUrl) {
  bad("site/index.html has no GitHub Releases download link to check");
} else if (!releaseUrl.endsWith(`/${tag}/${assetName}`)) {
  bad(`site/index.html links to\n        ${releaseUrl}\n        but this release is ${tag}/${assetName}.\n        The download button would 404.`);
} else {
  ok("the download button points at this exact release");
}

// ------------------------------------------------------------ git hygiene

step("Repository state");
let owner = "";
let repoName = "";
let visibility = "";
let branch = "";

if (PUBLISH || ALLOW_PRIVATE) {
  try {
    const view = JSON.parse(sh("gh", ["repo", "view", "--json", "visibility,nameWithOwner,defaultBranchRef"]));
    visibility = view.visibility;
    branch = view.defaultBranchRef?.name ?? "main";
    [owner = "", repoName = ""] = view.nameWithOwner.split("/");
    ok(`repository ${view.nameWithOwner} (${visibility.toLowerCase()})`);
  } catch (err) {
    bad(`${err.message}\n        Install the GitHub CLI and run:  gh auth login`);
  }
}

if (PUBLISH) {
  const status = sh("git", ["status", "--porcelain"], { allowFail: true });
  if (status === null) {
    bad("not a git repository");
  } else if (status !== "") {
    bad(`the working tree has uncommitted changes:\n${status.split("\n").slice(0, 10).map((l) => `        ${l}`).join("\n")}\n        A release should point at committed code.`);
  } else {
    ok("working tree is clean");
  }

  try {
    const remote = sh("git", ["remote", "get-url", "origin"]);
    if (remote.includes("github.com")) {
      const [, maybeOwner, maybeRepo] = remote.match(/github\.com[/:]([^/]+)\/([^/.]+)/) ?? [];
      if (maybeOwner && maybeRepo) {
        owner = maybeOwner;
        repoName = maybeRepo;
      }
    }
  } catch {
    /* reported by the visibility check above */
  }

  const unpushed = sh("git", ["log", `@{u}..HEAD`, "--oneline"], { allowFail: true });
  if (unpushed === null) {
    warn("no upstream configured; cannot confirm the commit is pushed");
  } else if (unpushed !== "") {
    bad(`${unpushed.split("\n").length} commit(s) are not pushed:\n${unpushed.split("\n").map((l) => `        ${l}`).join("\n")}\n        Push before releasing, or the tag will point at code nobody has.`);
  } else {
    ok("everything committed is pushed");
  }
}

// -------------------------------------------------------------- visibility

step("Visibility");
if (visibility === "PRIVATE" || visibility === "INTERNAL") {
  const message =
    `the repository is ${visibility.toLowerCase()}. A release there is invisible to ` +
    `visitors, so the download button on the public site will 404 for anyone not signed in.`;
  if (PUBLISH && !ALLOW_PRIVATE) {
    bad(`${message}\n        Make the repository public first, or pass --allow-private if that is genuinely intended.`);
  } else {
    warn(message);
  }
} else if (visibility === "PUBLIC") {
  ok("the repository is public, so a release asset is downloadable by anyone");
} else if (!PUBLISH) {
  ok("skipped (check-only mode; run with --publish to verify)");
} else {
  // Could not be determined (usually: gh is not authenticated). Say so, rather
  // than printing an empty section that reads like it passed.
  warn("could not determine whether the repository is public; run `gh auth login` to have this checked");
}

// ---------------------------------------------------------------- publish

if (!PUBLISH) {
  step("Result");
  if (problems.length > 0) {
    console.log(`${problems.length} problem(s). Nothing was published.\n`);
    process.exit(1);
  }
  if (warnings.length > 0) console.log(`\n${warnings.length} warning(s).`);
  console.log("\nAll checks pass. Re-run with --publish to create the release.\n");
  process.exit(0);
}

step("Publishing");
if (problems.length > 0) {
  console.log(`${problems.length} problem(s) found above. Nothing was published.`);
  console.log("Fix them and re-run; the checks exist to stop a broken release.\n");
  process.exit(1);
}

const assets = [exePath, ...(INCLUDE_ZIP && haveZip ? [zipPath] : [])];

const existing = sh("gh", ["release", "view", tag, "--json", "tagName"], { allowFail: true });
if (existing) {
  console.log(`  release ${tag} already exists; replacing its assets`);
  sh("gh", ["release", "upload", tag, ...assets, "--clobber"]);
  sh("gh", ["release", "edit", tag, "--title", `Circle ${version}`]);
} else {
  const notes = [
    `Circle ${version} for Windows (64-bit).`,
    "",
    "- Reads the exports already in one folder you choose.",
    "- Answers using a local model through Ollama; nothing is uploaded.",
    "",
    "See the project site for what it does and how it handles your data.",
  ].join("\n");
  sh("gh", ["release", "create", tag, ...assets, "--title", `Circle ${version}`, "--notes", notes, "--target", branch || "main"]);
}

console.log(`\n  published ${tag} with ${assets.length} asset(s)`);

// ---------------------------------------------------------------- verify

step("Verifying the public download");
const url = `https://github.com/${owner}/${repoName}/releases/download/${tag}/${assetName}`;
console.log(`  ${url}`);
const status = sh(
  "powershell",
  [
    "-NoProfile",
    "-Command",
    `(Invoke-WebRequest -Uri '${url}' -Method Head -MaximumRedirection 5 -UseBasicParsing -ErrorAction SilentlyContinue).StatusCode`,
  ],
  { allowFail: true }
);

if (status === "200") {
  ok("the download URL answers 200 without signing in");
  console.log("\nRelease published and publicly reachable.\n");
  process.exit(0);
}

if (status === "404" || status === null || status === "") {
  bad("the public download URL does not resolve.");
  console.log(
    "\n  This is the failure that looks like success: the release exists, the\n" +
      "  button is live, and clicking it 404s. Usual causes:\n" +
      "    - the repository is private (check with: gh repo view --json visibility)\n" +
      "    - the upload is still propagating; wait a minute and re-run --check\n\n"
  );
} else {
  warn(`unexpected status ${status} from the download URL`);
}

process.exit(1);

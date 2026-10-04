/* Release facts, in one place.

The download link and the size/version line are both shown on the hosted page.
Keeping them here means a release bump is one edit rather than two that can
quietly disagree -- a cached page offering a stale binary under a fresh name
is exactly the failure worth designing out.

site/index.html carries the same values as static HTML. If you bump this, bump
it there too (grep for Circle-0.).
*/

export const APP_VERSION = "0.2.0";

/**
 * Where the built installer is published.
 *
 * The Electron installer is larger than a git host's 100 MB per-file limit, so
 * it is hosted as a GitHub Release asset rather than committed to the repo. The
 * Render page only links to it.
 */
export const DOWNLOAD_PATH =
  "https://github.com/navaneedan07/circle/releases/download/v0.2.0/Circle-0.2.0-windows-x64.exe";
/* Release facts, in one place.

The download link and the size/version line are both shown on the hosted page.
Keeping them here means a release bump is one edit rather than two that can
quietly disagree -- a cached page offering a stale binary under a fresh name
is exactly the failure worth designing out.

site/index.html carries the same values as static HTML. If you bump this, bump
it there too (grep for Circle-0.).
*/

export const APP_VERSION = "0.1.0";

/** Where the built executable is published, relative to the site root. */
export const DOWNLOAD_PATH = "downloads/Circle-0.1.0-windows-x64.exe";

export const DOWNLOAD_SIZE_MB = 40;
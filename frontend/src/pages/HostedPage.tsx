/* Shown when this interface is NOT running on the reader's own machine.

Circle is a desktop application: it serves this interface from the same local
process that holds the archive. A copy of the interface opened anywhere else
has no backend behind it, and showing the connection screen there would ask the
reader to set up the very thing the desktop app exists to avoid.

This is deliberately short. The landing page is site/index.html, published as
static HTML, and duplicating its copy here would give the project two
descriptions of itself that drift apart.
*/
import { APP_VERSION, DOWNLOAD_PATH } from "../version";

export default function HostedPage() {
  return (
    <div className="flex min-h-screen items-start justify-center bg-paper px-5 py-16">
      <div className="w-full max-w-xl">
        <div className="eyebrow">Circle</div>
        <h1 className="serif mt-3 text-3xl leading-tight text-ink">
          This page has no Circle behind it
        </h1>

        <p className="mt-3 text-sm leading-6 text-muted">
          Circle is a desktop application. It runs on your computer, reads the
          folders you point it at, and keeps your archive in one local file.
          This copy of the interface is not connected to any of that, so there
          is nothing here to connect to.
        </p>

        <a
          href={DOWNLOAD_PATH}
          className="mt-8 inline-block bg-ink px-6 py-3 text-xs font-medium uppercase tracking-[0.16em] text-paper hover:bg-accent"
        >
          Download Circle for Windows
        </a>
        <p className="mt-3 text-xs leading-5 text-muted">
          Version {APP_VERSION}. One installer, one window, no account.
        </p>

        <p className="mt-8 border-l-2 border-line-strong pl-3 text-sm leading-6 text-muted">
          Already installed? Open the Circle app itself &mdash; it has its own
          window.
        </p>
      </div>
    </div>
  );
}
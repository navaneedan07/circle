import { useState } from "react";
import { api } from "../api";
import { PRIVACY_URL, TERMS_URL } from "../version";

/**
 * Consent gate.
 *
 * Circle's entire job is to point a local model at someone's private
 * messages. That makes the agreement a precondition rather than a formality:
 * this screen comes before the folder picker, before the model installer, and
 * before a single byte of the archive is read. Accepting afterwards would be
 * agreeing to something already done.
 *
 * The summary is here so the decision can be made without leaving the app; the
 * full documents open in the reader's real browser.
 */
export default function ConsentPage({ onAccepted }: { onAccepted: () => void }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const accept = async () => {
    setBusy(true);
    setError(null);
    try {
      await api.acceptTerms();
      onAccepted();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="flex min-h-screen items-center justify-center bg-paper px-5 py-12">
      <div className="w-full max-w-2xl">
        <header className="flex flex-wrap items-end justify-between gap-x-6 gap-y-3 border-b border-line pb-4">
          <div>
            <div className="serif text-lg tracking-[0.34em] text-ink">CIRCLE</div>
            <p className="mt-3 text-sm leading-6 text-muted">
              Before Circle reads anything, one agreement.
            </p>
          </div>
          <div className="text-right">
            <div className="eyebrow">First run</div>
            <div className="eyebrow-ink mt-1.5">Terms &amp; privacy</div>
          </div>
        </header>

        <section className="mt-6 border border-line">
          <h2 className="eyebrow-ink border-b border-line px-5 py-3">
            What happens to your messages
          </h2>
          <div className="space-y-4 px-5 py-5 text-sm leading-6 text-muted">
            <p>
              Circle reads the exports in the one folder you choose and builds a
              local index of the people in them. That index is a single SQLite
              file on this machine.
            </p>
            <ul className="space-y-2">
              <li>
                <span className="text-ink">Nothing is uploaded.</span> Circle
                has no account and no server, so there is nowhere for your
                messages to go.
              </li>
              <li>
                <span className="text-ink">Your files are not modified.</span>{" "}
                Exports are read where they already are and left exactly as they
                were.
              </li>
              <li>
                <span className="text-ink">Answers come from your own
                computer.</span> Questions are handled by a local model through
                Ollama. Circle collects no analytics and no telemetry.
              </li>
              <li>
                <span className="text-ink">You are responsible for the
                messages.</span> Point Circle only at exports you have the right
                to read.
              </li>
            </ul>
            <p className="text-xs leading-5">
              Deleting the <code>Documents/Circle</code> folder removes
              everything Circle has stored. There is no backup anywhere else.
            </p>
          </div>
        </section>

        <section className="mt-4 border border-line bg-surface px-5 py-5">
          <p className="text-sm leading-6 text-muted">
            Read the{" "}
            <a
              href={TERMS_URL}
              target="_blank"
              rel="noreferrer"
              className="text-ink underline underline-offset-2"
            >
              terms of use
            </a>{" "}
            and the{" "}
            <a
              href={PRIVACY_URL}
              target="_blank"
              rel="noreferrer"
              className="text-ink underline underline-offset-2"
            >
              privacy notice
            </a>{" "}
            in full. They open in your browser.
          </p>

          <button
            onClick={accept}
            disabled={busy}
            className="mt-4 w-full bg-ink py-3 text-xs font-medium uppercase tracking-[0.16em] text-paper hover:bg-accent disabled:opacity-50"
          >
            {busy ? "Saving" : "I agree, continue"}
          </button>

          {error && (
            <div className="mt-4 border border-accent px-3.5 py-2.5 text-sm text-accent">
              {error}
            </div>
          )}
        </section>

        <p className="mt-4 text-center text-xs text-muted">
          Local first. Your conversations never leave this machine.
        </p>
      </div>
    </div>
  );
}

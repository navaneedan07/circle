/* First run, and the recovery path.

Circle can be hosted two ways:
  1. The backend serves the UI (normal local use) -- nothing to configure.
  2. The UI is hosted separately and the backend runs on this machine behind
     a tunnel -- the user pastes that address and the access key once.

This screen covers both the "cannot reach it" and "needs the key" cases, and
also guides a new user through the one thing that cannot be automated: getting
MongoDB and the Gemma model onto their machine.
*/
import { useEffect, useState } from "react";
import { api, AuthError, loadConnection, saveConnection } from "../api";

type Status = "checking" | "ok" | "needs_key" | "unreachable" | "no_model";

/**
 * True when this page is NOT served by the user's own Circle.
 *
 * On the hosted UI (the Render site) same-origin is a static host with no API
 * behind it, so "leave the address blank" would never work: a tunnel address
 * is required. Only trust a loopback hostname, since that is the one case
 * where same-origin really is the backend.
 */
const SAME_ORIGIN_IS_BACKEND = ["localhost", "127.0.0.1", "[::1]", ""].includes(
  window.location.hostname
);

export default function ConnectPage({
  onConnected,
}: {
  onConnected: () => void;
}) {
  const [baseUrl, setBaseUrl] = useState(loadConnection().baseUrl);
  const [accessKey, setAccessKey] = useState(loadConnection().accessKey);
  const [status, setStatus] = useState<Status>("checking");
  const [detail, setDetail] = useState("");
  const [busy, setBusy] = useState(false);
  const [pulling, setPulling] = useState(false);
  const [pullNote, setPullNote] = useState("");
  const [model, setModel] = useState("");

  const probe = async () => {
    setStatus("checking");
    setDetail("");
    try {
      const s = await api.authStatus();
      setModel(s.model);
      // key_accepted, not access_key_required: this endpoint is public, so a
      // 200 proves nothing about whether the supplied key is correct.
      if (!s.key_accepted) {
        setStatus("needs_key");
        setDetail(
          loadConnection().accessKey
            ? "That access key was not accepted. Check it against the one Circle printed at startup."
            : "This Circle is shared and needs its access key."
        );
        return;
      }
      if (!s.model_installed) {
        setStatus("no_model");
        setDetail(
          s.ollama_reachable
            ? `Ollama is running, but ${s.model} has not been downloaded yet.`
            : `Ollama is not running. Start it, then download ${s.model}.`
        );
        return;
      }
      setStatus("ok");
      onConnected();
    } catch (e) {
      if (e instanceof AuthError && e.needsKey) {
        setStatus("needs_key");
        setDetail(e.message);
      } else {
        setStatus("unreachable");
        setDetail(e instanceof Error ? e.message : "Cannot reach the backend.");
      }
    }
  };

  useEffect(() => {
    void probe();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const save = async () => {
    setBusy(true);
    saveConnection({ baseUrl: baseUrl.trim(), accessKey: accessKey.trim() });
    // A wrong key or a mistyped address should not be saved silently.
    await probe();
    setBusy(false);
  };

  const pull = async () => {
    setPulling(true);
    setPullNote("Downloading. This is a few gigabytes and can take a while.");
    try {
      const r = await api.pullModel();
      setPullNote(
        r.ok
          ? `${r.model} is ready.`
          : `Could not download: ${r.error ?? "unknown error"}`
      );
      if (r.ok) await probe();
    } catch (e) {
      setPullNote(e instanceof Error ? e.message : "Download failed.");
    } finally {
      setPulling(false);
    }
  };

  return (
    <div className="flex min-h-screen items-start justify-center bg-paper px-5 py-16">
      <div className="w-full max-w-2xl">
        <div className="eyebrow">Circle</div>
        <h1 className="serif mt-3 text-3xl leading-tight text-ink">
          {status === "needs_key"
            ? "This Circle needs its access key"
            : status === "no_model"
              ? "One more step: download the model"
              : "Connect to your Circle"}
        </h1>

        <p className="mt-3 text-sm leading-6 text-muted">
          {status === "needs_key"
            ? "Your archive stays on your own machine. This key only proves the browser is allowed to ask it questions."
            : status === "no_model"
              ? "Circle answers questions with a local model. It runs on your machine and nothing is sent anywhere."
              : SAME_ORIGIN_IS_BACKEND
                ? "If Circle is running on this computer, leave the address blank. If you opened this page from somewhere else, paste the address your Circle printed when it started."
                : "This page is only the interface. Your archive stays on your own machine, so paste the address your Circle printed when it started (it looks like a tunnel address), then its access key."}
        </p>

        {(status === "unreachable" || status === "needs_key") && (
          <div className="mt-6 space-y-4">
            <div>
              <label className="eyebrow block" htmlFor="base-url">
                Circle address
              </label>
              <input
                id="base-url"
                value={baseUrl}
                onChange={(e) => setBaseUrl(e.target.value)}
                placeholder="https://your-tunnel-address"
                autoComplete="off"
                className="mono mt-2 w-full border border-line bg-surface px-3.5 py-2.5 text-sm text-ink outline-none placeholder:text-muted focus:border-line-strong"
              />
              <p className="mt-2 text-xs leading-5 text-muted">
                {SAME_ORIGIN_IS_BACKEND
                  ? "Leave empty when Circle is running on this computer."
                  : "Where Circle is running on your own machine, not this page."}
              </p>
            </div>

            <div>
              <label className="eyebrow block" htmlFor="access-key">
                Access key
              </label>
              <input
                id="access-key"
                type="password"
                value={accessKey}
                onChange={(e) => setAccessKey(e.target.value)}
                placeholder="Only if your Circle was shared"
                autoComplete="off"
                className="mono mt-2 w-full border border-line bg-surface px-3.5 py-2.5 text-sm text-ink outline-none placeholder:text-muted focus:border-line-strong"
              />
              <p className="mt-2 text-xs leading-5 text-muted">
                The same key your Circle printed at startup. It is stored only
                in this browser.
              </p>
            </div>

            <div className="flex flex-wrap items-center gap-3">
              <button
                onClick={save}
                disabled={busy}
                className="bg-ink px-5 py-2.5 text-xs font-medium text-paper hover:bg-accent disabled:opacity-50"
              >
                {busy ? "Checking" : "Connect"}
              </button>
              {SAME_ORIGIN_IS_BACKEND && (
                <button
                  onClick={() => {
                    setBaseUrl("");
                    setAccessKey("");
                    saveConnection({ baseUrl: "", accessKey: "" });
                    void probe();
                  }}
                  className="border border-ink px-5 py-2.5 text-xs font-medium text-ink hover:bg-ink hover:text-paper"
                >
                  Use this computer
                </button>
              )}
            </div>
          </div>
        )}

        {status === "no_model" && (
          <div className="mt-6 space-y-4">
            <div className="border border-line bg-surface p-4">
              <div className="eyebrow">In a terminal, on this computer</div>
              <code className="mono mt-2 block break-words text-sm text-ink">
                ollama pull {model}
              </code>
            </div>
            <div className="flex flex-wrap items-center gap-3">
              <button
                onClick={pull}
                disabled={pulling}
                className="bg-ink px-5 py-2.5 text-xs font-medium text-paper hover:bg-accent disabled:opacity-50"
              >
                {pulling ? "Downloading" : "Download it for me"}
              </button>
              <button
                onClick={() => void probe()}
                className="border border-ink px-5 py-2.5 text-xs font-medium text-ink hover:bg-ink hover:text-paper"
              >
                I have already done this
              </button>
            </div>
            {pullNote && (
              <p aria-live="polite" className="text-sm leading-6 text-muted">
                {pullNote}
              </p>
            )}
          </div>
        )}

        {status === "unreachable" && (
          <div className="mt-6 border border-line p-4">
            <div className="eyebrow">If Circle is not starting at all</div>
            <ul className="mt-2 space-y-2 text-sm leading-6 text-muted">
              <li>
                <span className="text-ink">MongoDB</span> must be running.
                Circle stores everything in a local database and cannot start
                without one.
              </li>
              <li>
                <span className="text-ink">Ollama</span> must be running for
                answers. Start the app and leave it open.
              </li>
              {!SAME_ORIGIN_IS_BACKEND && (
                <li>
                  <span className="text-ink">Tunnel</span> must be running so
                  this page can reach your machine:
                  <code className="mono"> cloudflared tunnel --url http://127.0.0.1:8000</code>.
                  Copy the address it prints into the field above.
                </li>
              )}
              <li>
                Both are local services. Nothing is uploaded to run them.
              </li>
            </ul>
          </div>
        )}

        {status === "checking" && (
          <p className="mt-6 text-sm text-muted">Checking the backend.</p>
        )}

        {detail && status !== "checking" && (
          <p
            aria-live="polite"
            className="mt-4 border-l-2 border-line-strong pl-3 text-sm leading-6 text-muted"
          >
            {detail}
          </p>
        )}
      </div>
    </div>
  );
}
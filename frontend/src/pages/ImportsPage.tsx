import { useState } from "react";
import { api, fmtDate, ImportJob } from "../api";
import { usePoll } from "../hooks";
import WatchFolders from "../components/WatchFolders";

const STATUS_TEXT: Record<string, string> = {
  COMPLETED: "text-moss",
  PROCESSING: "text-ochre",
  QUEUED: "text-ochre",
  FAILED: "text-accent",
  QUARANTINED: "text-accent",
  SKIPPED: "text-muted",
};

const SOURCE_FOLDERS = [
  "whatsapp",
  "telegram",
  "instagram",
  "x",
  "chats",
  "voice",
  "email",
  "calendar",
  "contacts",
  "documents",
  "notes",
];

export default function ImportsPage() {
  const { data, refresh } = usePoll(() => api.imports(), 3000);
  const { data: sync } = usePoll(() => api.syncStatus(), 4000);
  const [scanning, setScanning] = useState(false);
  const [busy, setBusy] = useState(false);

  const rescan = async () => {
    setScanning(true);
    try {
      await api.rescan();
      setTimeout(refresh, 1200);
    } finally {
      setScanning(false);
    }
  };

  const generateDemo = async () => {
    setBusy(true);
    try {
      await api.generateDemo();
      setTimeout(refresh, 2500);
    } finally {
      setBusy(false);
    }
  };

  const jobs = data?.jobs ?? [];
  const counts = Object.entries(data?.counts ?? {});

  return (
    <div className="space-y-10">
      <header className="flex flex-wrap items-end justify-between gap-x-8 gap-y-5">
        <div>
          <div className="eyebrow">Intake</div>
          <h1 className="serif mt-3 text-3xl leading-none text-ink">Imports</h1>
          <p className="mt-3 max-w-xl text-sm leading-6 text-muted">
            Files placed in the watched folders are processed automatically. No
            button is needed.
          </p>
        </div>
        <div className="flex gap-2">
          <button
            onClick={rescan}
            disabled={scanning}
            className="border border-ink px-4 py-2 text-xs font-medium text-ink hover:bg-ink hover:text-paper disabled:opacity-50"
          >
            {scanning ? "Scanning" : "Rescan now"}
          </button>
          <button
            onClick={generateDemo}
            disabled={busy}
            className="bg-ink px-4 py-2 text-xs font-medium text-paper hover:bg-accent disabled:opacity-50"
          >
            {busy ? "Generating" : "Generate demo data"}
          </button>
        </div>
      </header>

      <dl className="grid grid-cols-2 gap-px border border-line bg-line sm:grid-cols-4">
        {(
          [
            [
              "Watch folders",
              String(sync?.folder_count ?? (sync?.roots?.length ?? 1)),
            ],
            ["Processed", String(sync?.files.processed ?? 0)],
            ["Failed", String(sync?.files.failed ?? 0)],
            ["Watcher", sync?.watcher_running ? "Running" : "Stopped"],
          ] as [string, string][]
        ).map(([label, value]) => (
          <div key={label} className="bg-paper px-4 py-3">
            <dt className="eyebrow">{label}</dt>
            <dd className="mono mt-2 truncate text-lg leading-none tabular-nums text-ink">
              {value}
            </dd>
          </div>
        ))}
      </dl>

      <WatchFolders compact />

      <section className="border border-line">
        <h2 className="eyebrow border-b border-line px-4 py-2.5">
          What Circle reads
        </h2>
        <div className="px-4 py-4">
          <div className="flex flex-wrap gap-2">
            {SOURCE_FOLDERS.map((f) => (
              <span
                key={f}
                className="border border-line px-2.5 py-1 text-xs text-ink"
              >
                {f}/
              </span>
            ))}
          </div>
          <p className="eyebrow mt-4 leading-5">
            Formats: txt json csv html pdf docx eml mbox ics vcf wav mp3 m4a ogg
            webm zip, plus photos and voice notes inside WhatsApp exports
          </p>
        </div>
      </section>

      <section className="border border-line">
        <div className="flex flex-wrap items-baseline justify-between gap-x-6 gap-y-1 border-b border-line px-4 py-2.5">
          <h2 className="eyebrow">Import queue</h2>
          {counts.length > 0 && (
            <div className="eyebrow flex flex-wrap gap-x-4 gap-y-1">
              {counts.map(([status, n]) => (
                <span key={status} className={STATUS_TEXT[status] ?? "text-muted"}>
                  {status} {n}
                </span>
              ))}
            </div>
          )}
        </div>

        {jobs.length === 0 ? (
          <div className="px-6 py-14 text-center text-sm text-muted">
            No imports yet. Drop files into the import folder above, or generate
            demo data, and they appear here as they are processed.
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full border-collapse text-left">
              <thead>
                <tr className="eyebrow border-b border-line">
                  <th className="py-2.5 pl-4 pr-3 font-normal">File</th>
                  <th className="hidden py-2.5 pr-3 font-normal sm:table-cell">
                    Source
                  </th>
                  <th className="py-2.5 pr-3 font-normal">Status</th>
                  <th className="py-2.5 pr-3 text-right font-normal">Records</th>
                  <th className="hidden py-2.5 pr-4 text-right font-normal sm:table-cell">
                    When
                  </th>
                </tr>
              </thead>
              <tbody>
                {jobs.map((j: ImportJob) => (
                  <tr
                    key={j.id ?? j.filename}
                    className="border-b border-line last:border-b-0 hover:bg-surface"
                  >
                    <td className="py-3 pl-4 pr-3 align-top text-ink">
                      <div className="break-words">{j.filename}</div>
                      {j.error && (
                        <div
                          className="mt-1 truncate text-xs text-accent"
                          title={j.error}
                        >
                          {j.error}
                        </div>
                      )}
                    </td>
                    <td className="hidden py-3 pr-3 align-top text-sm text-muted sm:table-cell">
                      {j.source ?? "unknown"}
                    </td>
                    <td className="py-3 pr-3 align-top">
                      <span
                        className={`text-[10px] uppercase tracking-[0.08em] sm:text-[11px] sm:tracking-[0.16em] ${STATUS_TEXT[j.status] ?? "text-muted"}`}
                      >
                        {j.status}
                      </span>
                    </td>
                    <td className="mono py-3 pr-3 text-right align-top text-xs tabular-nums text-ink">
                      {j.records_imported}
                      {j.records_skipped > 0 && (
                        <span className="text-muted">
                          {" "}
                          +{j.records_skipped} dup
                        </span>
                      )}
                    </td>
                    <td className="mono hidden py-3 pr-4 text-right align-top text-[11px] leading-5 text-muted sm:table-cell">
                      {fmtDate(j.completed_at ?? j.created_at ?? null)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </div>
  );
}

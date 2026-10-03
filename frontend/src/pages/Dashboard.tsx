import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { api, fmtWhen, Person, STATUS_DOTS } from "../api";
import { usePoll } from "../hooks";
import AskPanel from "../components/AskPanel";

const PAGE_SIZES = [25, 50, 100, 200];
const SORTS = [
  { key: "recent", label: "Recent" },
  { key: "name", label: "Name" },
  { key: "interactions", label: "Interactions" },
] as const;
const VIEW_KEY = "circle-people-view";

/** The ledger view (filter, sort, page size, page) survives a reload. */
type PeopleView = {
  q: string;
  status: string;
  sort: string;
  pageSize: number;
  page: number;
};

function readView(): PeopleView {
  const fallback: PeopleView = {
    q: "",
    status: "",
    sort: "recent",
    pageSize: 50,
    page: 0,
  };
  try {
    const raw = localStorage.getItem(VIEW_KEY);
    if (!raw) return fallback;
    const p = JSON.parse(raw) as Partial<PeopleView>;
    const size = Number(p.pageSize);
    return {
      q: typeof p.q === "string" ? p.q : "",
      status: typeof p.status === "string" ? p.status : "",
      sort: SORTS.some((s) => s.key === p.sort) ? String(p.sort) : "recent",
      pageSize: PAGE_SIZES.includes(size) ? size : 50,
      page: Math.max(0, Number(p.page) || 0),
    };
  } catch {
    return fallback;
  }
}

const STATUSES = [
  "Very Active",
  "Active",
  "Occasional",
  "Low Activity",
  "No Recent Activity",
];

function StatLedger({ stats }: { stats: [string, string][] }) {
  return (
    <dl className="grid grid-cols-2 gap-px border border-line bg-line sm:grid-cols-4">
      {stats.map(([label, value]) => (
        <div key={label} className="bg-paper px-4 py-3">
          <dt className="eyebrow">{label}</dt>
          <dd className="mono mt-2 text-lg leading-none tabular-nums text-ink">
            {value}
          </dd>
        </div>
      ))}
    </dl>
  );
}

function PeopleLedger({ people }: { people: Person[] }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full border-collapse text-left">
        <thead>
          <tr className="eyebrow border-y border-line">
            <th className="py-2.5 pl-3 pr-3 font-normal">Name</th>
            <th className="hidden py-2.5 pr-3 font-normal sm:table-cell">
              Status
            </th>
            <th className="py-2.5 pr-3 text-right font-normal">Last seen</th>
            <th className="py-2.5 pr-3 text-right font-normal">Interactions</th>
          </tr>
        </thead>
        <tbody>
          {people.map((p) => {
            const dot = STATUS_DOTS[p.status ?? ""] ?? "bg-muted";
            return (
              <tr key={p.id} className="border-b border-line hover:bg-surface">
                <td className="max-w-0 py-3 pl-3 pr-3 align-top">
                  <Link
                    to={`/person/${p.id}`}
                    className="flex min-h-6 min-w-0 items-center gap-2"
                  >
                    <span
                      className={`h-2 w-2 shrink-0 ${dot}`}
                      aria-hidden="true"
                    />
                    <span className="truncate text-sm font-medium text-ink">
                      {p.display_name}
                    </span>
                  </Link>
                  {p.status_reason && (
                    <div className="mt-1 truncate pl-4 text-xs text-muted">
                      {p.status_reason}
                    </div>
                  )}
                </td>
                <td className="hidden py-3 pr-3 align-top sm:table-cell">
                  <span className="eyebrow">{p.status ?? "No data yet"}</span>
                </td>
                <td className="mono py-3 pr-3 text-right align-top text-[11px] leading-5 text-muted">
                  {fmtWhen(p.last_interaction_at)}
                </td>
                <td className="mono py-3 pr-3 text-right align-top text-xs tabular-nums text-ink">
                  {p.interaction_count ?? 0}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

/** Seed questions. The first group is answered by MongoDB, not the model, so
 *  showing them teaches which questions are instant. */
const ASK_EXAMPLES = [
  "Who do I talk to the most",
  "Who did I talk to in the last 7 days",
  "How many messages did I send last week",
  "What are my top topics",
  "What have we been talking about recently?",
];

function AskExamples() {
  return (
    <div className="flex flex-wrap items-center gap-2">
      <span className="eyebrow">Try</span>
      {ASK_EXAMPLES.map((q) => (
        <span key={q} className="text-xs text-muted">
          <AskSeedButton question={q} />
        </span>
      ))}
    </div>
  );
}

function AskSeedButton({ question }: { question: string }) {
  // Routed through a custom event so AskPanel owns the asking state rather
  // than this row lifting it, which would put the query state in the page.
  return (
    <button
      onClick={() =>
        window.dispatchEvent(
          new CustomEvent("circle:ask", { detail: question })
        )
      }
      className="border border-line px-2.5 py-1 text-xs text-muted hover:border-line-strong hover:text-ink"
    >
      {question}
    </button>
  );
}

export default function Dashboard() {
  const initial = useMemo(readView, []);
  const [query, setQuery] = useState(initial.q);
  const [search, setSearch] = useState(initial.q);
  const [statusFilter, setStatusFilter] = useState(initial.status);
  const [pageSize, setPageSize] = useState(initial.pageSize);
  const [page, setPage] = useState(initial.page);
  const [sort, setSort] = useState(initial.sort);
  const offset = page * pageSize;

  const { data, refresh, error } = usePoll(
    () =>
      api.people({
        q: search || undefined,
        status: statusFilter || undefined,
        sort,
        limit: pageSize,
        offset,
      }),
    6000,
    [search, statusFilter, offset, pageSize, sort]
  );
  const { data: sync } = usePoll(() => api.syncStatus(), 8000);

  // Debounce typing so each keystroke does not fire a request.
  useEffect(() => {
    const t = window.setTimeout(() => setSearch(query.trim()), 250);
    return () => window.clearTimeout(t);
  }, [query]);

  // Remember the view so a reload lands on the same page and filter.
  useEffect(() => {
    try {
      localStorage.setItem(
        VIEW_KEY,
        JSON.stringify({ q: query, status: statusFilter, sort, pageSize, page })
      );
    } catch {
      /* private mode */
    }
  }, [query, statusFilter, sort, pageSize, page]);

  const total = data?.total ?? 0;

  // A page can fall out of range when the archive shrinks or the filter
  // changes underneath it; fall back to the last page that exists.
  useEffect(() => {
    if (!data) return;
    const last = Math.max(0, Math.ceil(data.total / pageSize) - 1);
    if (page > last) setPage(last);
  }, [data, pageSize, page]);

  const people = data?.people ?? [];
  const mostActive = data?.top ?? [];
  const filtered = !!statusFilter || !!search;
  const from = total === 0 ? 0 : offset + 1;
  const to = Math.min(offset + pageSize, total);

  const setFilter = (next: string) => {
    setStatusFilter(next);
    setPage(0);
  };

  const setRows = (next: number) => {
    setPageSize(next);
    setPage(0);
  };

  return (
    <div className="space-y-10">
      <header>
        <div className="flex flex-wrap items-end justify-between gap-x-8 gap-y-4">
          <div>
            <div className="eyebrow">The archive</div>
            <h1 className="serif mt-3 text-4xl leading-none text-ink">People</h1>
          </div>
          <p className="max-w-md text-sm leading-6 text-muted">
            Context from every export you have imported, brought together under
            one person.
          </p>
        </div>

        <div className="mt-6">
          <StatLedger
            stats={[
              ["People", String(sync?.counts.people ?? total)],
              ["Messages", String(sync?.counts.messages ?? 0)],
              ["Indexed", String(sync?.counts.memories ?? 0)],
              ["Files read", String(sync?.files.processed ?? 0)],
            ]}
          />
        </div>
      </header>

      <div className="grid gap-10 lg:grid-cols-[minmax(0,1fr)_20rem]">
        <div className="min-w-0 space-y-10">
          <AskPanel
            title="Ask your archive"
            placeholder="What have we been talking about recently?"
          />

          <AskExamples />

          <section className="space-y-4">
            <div className="flex flex-wrap items-end justify-between gap-4 border-b border-line pb-3">
              <div className="flex flex-wrap items-center gap-4">
                <h2 className="eyebrow">People</h2>
                <span className="eyebrow-ink tabular-nums">{total}</span>
              </div>
              <input
                value={query}
                onChange={(e) => {
                  setQuery(e.target.value);
                  setPage(0);
                }}
                placeholder="Search names"
                aria-label="Search people by name"
                className="w-full border border-line bg-surface px-3 py-2 text-sm text-ink outline-none placeholder:text-muted focus:border-line-strong sm:w-64"
              />
            </div>

            <div className="flex flex-wrap items-center gap-x-6 gap-y-2">
              <div className="flex flex-wrap gap-x-5 gap-y-2">
                <button
                  onClick={() => setFilter("")}
                  className={`eyebrow inline-flex min-h-6 items-center border-b ${!statusFilter ? "border-ink text-ink" : "border-transparent text-muted hover:text-ink"}`}
                >
                  All
                </button>
                {STATUSES.map((s) => (
                  <button
                    key={s}
                    onClick={() => setFilter(statusFilter === s ? "" : s)}
                    className={`eyebrow inline-flex min-h-6 items-center border-b ${statusFilter === s ? "border-ink text-ink" : "border-transparent text-muted hover:text-ink"}`}
                  >
                    {s}
                  </button>
                ))}
              </div>

              <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
                <span className="eyebrow">Sort</span>
                {SORTS.map((s) => (
                  <button
                    key={s.key}
                    onClick={() => {
                      setSort(s.key);
                      setPage(0);
                    }}
                    aria-pressed={sort === s.key}
                    className={`eyebrow inline-flex min-h-6 items-center border-b ${sort === s.key ? "border-ink text-ink" : "border-transparent text-muted hover:text-ink"}`}
                  >
                    {s.label}
                  </button>
                ))}
              </div>
            </div>

            {error && (
              <div className="border border-accent px-3 py-2 text-sm text-accent">
                Cannot reach the local API: {error}
              </div>
            )}

            {total === 0 && !filtered ? (
              <div className="border border-line bg-surface px-6 py-16 text-center">
                <h3 className="serif text-lg text-ink">No people yet</h3>
                <p className="mx-auto mt-2 max-w-md text-sm text-muted">
                  Drop an export into the watched import folder. WhatsApp,
                  Telegram, calendar, contacts, email and voice recordings all
                  produce people automatically.
                </p>
                <div className="mt-6 flex justify-center gap-3">
                  <Link
                    to="/imports"
                    className="border border-ink px-4 py-2 text-xs font-medium text-ink hover:bg-ink hover:text-paper"
                  >
                    View imports
                  </Link>
                  <button
                    onClick={() => api.generateDemo().then(refresh)}
                    className="bg-ink px-4 py-2 text-xs font-medium text-paper hover:bg-accent"
                  >
                    Generate demo data
                  </button>
                </div>
              </div>
            ) : people.length === 0 ? (
              <div className="border border-line px-6 py-12 text-center">
                <p className="text-sm text-ink">
                  No people match this view.
                </p>
                <p className="mx-auto mt-2 max-w-md text-sm text-muted">
                  {search
                    ? `Nothing in the archive is named "${search.trim()}".`
                    : `No one is currently marked ${statusFilter}.`}
                </p>
                <button
                  onClick={() => {
                    setQuery("");
                    setStatusFilter("");
                    setPage(0);
                  }}
                  className="mt-5 border border-ink px-4 py-2 text-xs font-medium text-ink hover:bg-ink hover:text-paper"
                >
                  Clear filters
                </button>
              </div>
            ) : (
              <>
                <PeopleLedger people={people} />

                <div className="flex flex-wrap items-center justify-between gap-x-6 gap-y-3 pt-3">
                  <span className="eyebrow tabular-nums">
                    Showing {from} to {to} of {total}
                    {filtered && (
                      <span className="text-ink">
                        {" "}
                        / {search ? `matching "${search.trim()}"` : statusFilter}
                      </span>
                    )}
                  </span>

                  <div className="flex flex-wrap items-center gap-x-6 gap-y-2">
                    <div className="flex items-center gap-3">
                      <span className="eyebrow">Rows</span>
                      {PAGE_SIZES.map((n) => (
                        <button
                          key={n}
                          onClick={() => setRows(n)}
                          aria-pressed={pageSize === n}
                          className={`eyebrow inline-flex min-h-6 items-center border-b tabular-nums ${pageSize === n ? "border-ink text-ink" : "border-transparent text-muted hover:text-ink"}`}
                        >
                          {n}
                        </button>
                      ))}
                    </div>
                    <div className="flex gap-2">
                      <button
                        onClick={() => setPage((p) => Math.max(0, p - 1))}
                        disabled={page === 0}
                        className="border border-line px-3 py-1.5 text-xs text-ink hover:border-line-strong disabled:opacity-40"
                      >
                        Previous
                      </button>
                      <button
                        onClick={() => setPage((p) => p + 1)}
                        disabled={to >= total}
                        className="border border-line px-3 py-1.5 text-xs text-ink hover:border-line-strong disabled:opacity-40"
                      >
                        Next
                      </button>
                    </div>
                  </div>
                </div>
              </>
            )}
          </section>
        </div>

        <aside className="min-w-0 space-y-6 self-start lg:sticky lg:top-8">
          {mostActive.length > 0 && (
            <section className="border border-line">
              <h2 className="eyebrow border-b border-line px-4 py-2.5">
                Most active
              </h2>
              <ol>
                {mostActive.map((p, i) => (
                  <li key={p.id} className="border-b border-line last:border-b-0">
                    <Link
                      to={`/person/${p.id}`}
                      className="flex items-baseline justify-between gap-3 px-4 py-2.5 hover:bg-surface"
                    >
                      <span className="flex min-w-0 items-baseline gap-3">
                        <span className="mono shrink-0 text-[11px] text-muted">
                          {String(i + 1).padStart(2, "0")}
                        </span>
                        <span className="truncate text-sm text-ink">
                          {p.display_name}
                        </span>
                      </span>
                      <span className="mono shrink-0 text-[11px] tabular-nums text-muted">
                        {p.interaction_count}
                      </span>
                    </Link>
                  </li>
                ))}
              </ol>
            </section>
          )}

          <section className="border border-line">
            <h2 className="eyebrow border-b border-line px-4 py-2.5">
              Reading from
            </h2>
            <div className="space-y-2 px-4 py-3">
              <div className="eyebrow-ink">
                {sync?.folder_count ?? sync?.roots?.length ?? 1} folder
                {(sync?.folder_count ?? 1) === 1 ? "" : "s"}
              </div>
              {(sync?.roots ?? [sync?.root ?? "not set"]).map((r) => (
                <div
                  key={String(r)}
                  className="mono truncate text-[11px] text-muted"
                  title={String(r)}
                >
                  {String(r)}
                </div>
              ))}
              <p className="pt-1 text-xs text-muted">
                Files are processed automatically as they appear.
              </p>
              <div className="flex flex-wrap gap-x-4 gap-y-1 pt-1">
                <Link
                  to="/imports"
                  className="eyebrow inline-flex min-h-6 items-center text-ink underline decoration-line-strong underline-offset-4"
                >
                  Imports
                </Link>
                <Link
                  to="/settings"
                  className="eyebrow inline-flex min-h-6 items-center text-ink underline decoration-line-strong underline-offset-4"
                >
                  Manage folders
                </Link>
              </div>
            </div>
          </section>
        </aside>
      </div>
    </div>
  );
}

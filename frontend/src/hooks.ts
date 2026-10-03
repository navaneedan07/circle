import { useEffect, useRef, useState, useCallback } from "react";
import { apiUrl, getConnection } from "./api";

/** Subscribe to backend SSE events; calls `onChange` for relevant events. */
export function useEvents(
  onChange: (type: string, data: Record<string, unknown>) => void
): { connected: boolean; lastEvent: { type: string; at: number } | null } {
  const [connected, setConnected] = useState(false);
  const [lastEvent, setLastEvent] = useState<{ type: string; at: number } | null>(null);
  const handler = useRef(onChange);
  handler.current = onChange;

  useEffect(() => {
    // EventSource cannot send headers, so the access key travels in the query
    // string. The backend accepts that for /api/events only.
    const { accessKey } = getConnection();
    const url = apiUrl("/api/events") + (accessKey ? `?key=${encodeURIComponent(accessKey)}` : "");
    const es = new EventSource(url);
    es.onopen = () => setConnected(true);
    es.onerror = () => setConnected(false);
    es.onmessage = (ev) => {
      try {
        const parsed = JSON.parse(ev.data);
        const type = parsed.type as string;
        if (type && type !== "ping" && type !== "hello") {
          setLastEvent({ type, at: Date.now() });
          handler.current(type, parsed.data || {});
        }
      } catch {
        /* ignore malformed events */
      }
    };
    const onConn = () => {
      es.close();
      void 0;
    };
    window.addEventListener("circle:connection", onConn);
    return () => {
      window.removeEventListener("circle:connection", onConn);
      es.close();
    };
  }, []);

  return { connected, lastEvent };
}

/** Simple polling hook with manual refresh support. */
export function usePoll<T>(
  fetcher: () => Promise<T>,
  intervalMs: number,
  deps: unknown[] = []
): { data: T | null; error: string | null; refresh: () => void; loading: boolean } {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const alive = useRef(true);

  // Keep the fetcher in a ref so a changing inline closure does not
  // invalidate `refresh`. Without this the effect re-runs on every render,
  // its cleanup clears `alive` before the in-flight request resolves, and
  // `setData` is never applied (the page hangs on its loading state).
  const fetcherRef = useRef(fetcher);
  fetcherRef.current = fetcher;

  const refresh = useCallback(() => {
    fetcherRef
      .current()
      .then((d) => {
        if (alive.current) {
          setData(d);
          setError(null);
        }
      })
      .catch((e: Error) => {
        if (alive.current) setError(e.message);
      })
      .finally(() => {
        if (alive.current) setLoading(false);
      });
  }, []);

  useEffect(() => {
    alive.current = true;
    refresh();
    const t = setInterval(refresh, intervalMs);
    return () => {
      alive.current = false;
      clearInterval(t);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [refresh, intervalMs, ...deps]);

  return { data, error, refresh, loading };
}

/** Dark/light theme persisted in localStorage. */
export function useTheme(): [string, () => void] {
  const [theme, setTheme] = useState<string>(
    () => localStorage.getItem("circle-theme") || "dark"
  );
  useEffect(() => {
    document.documentElement.classList.toggle("dark", theme === "dark");
    localStorage.setItem("circle-theme", theme);
  }, [theme]);
  const toggle = () => setTheme((t) => (t === "dark" ? "light" : "dark"));
  return [theme, toggle];
}

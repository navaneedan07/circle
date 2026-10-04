import { Route, Routes, useLocation, useParams } from "react-router-dom";
import { useCallback, useEffect, useState } from "react";
import Layout from "./components/Layout";
import Dashboard from "./pages/Dashboard";
import PersonPage from "./pages/PersonPage";
import ImportsPage from "./pages/ImportsPage";
import SettingsPage from "./pages/SettingsPage";
import FirstRun from "./pages/FirstRun";
import ConnectPage from "./pages/ConnectPage";
import HostedPage from "./pages/HostedPage";
import SetupPage from "./pages/SetupPage";
import ConsentPage from "./pages/ConsentPage";
import { api, getConnection } from "./api";

/**
 * True when this page is being served by the reader's own Circle.
 *
 * Circle is one desktop application: the same local process serves the
 * interface and holds the archive. Only a loopback origin can be that process.
 * Anywhere else -- a static host, a shared link -- is a copy of the interface
 * with no backend behind it, so it shows the download page instead of a
 * connection error the reader cannot act on.
 */
const RUNS_ON_OWN_MACHINE = [
  "localhost",
  "127.0.0.1",
  "[::1]",
  "::1",
  "",
].includes(window.location.hostname);

/** Reports the open person upward so search can be scoped to them. */
function PersonRoute() {
  const { id = "" } = useParams();
  const [name, setName] = useState<string>("");
  useEffect(() => {
    let alive = true;
    if (!id) return;
    api
      .person(id)
      .then((d) => {
        if (alive) setName(d.person.display_name);
      })
      .catch(() => {
        if (alive) setName("");
      });
    return () => {
      alive = false;
    };
  }, [id]);
  return <PersonPage personName={name} />;
}

export default function App() {
  // Before any network call: off-machine, there is nothing to talk to.
  if (!RUNS_ON_OWN_MACHINE) return <HostedPage />;

  return <CircleApp />;
}

function CircleApp() {
  const [firstRun, setFirstRun] = useState<boolean | null>(null);
  const location = useLocation();
  const personId = location.pathname.startsWith("/person/")
    ? location.pathname.split("/")[2] ?? ""
    : "";

  const [connected, setConnected] = useState<boolean | null>(null);
  // Consent is checked before anything else: before the folder is chosen,
  // before the model installer can run, before a single export is read.
  const [termsAccepted, setTermsAccepted] = useState<boolean | null>(null);
  // The model gate is separate from the connection gate: the backend can be
  // reachable while Ollama is absent, and then the only honest screen is the
  // prerequisite installer.
  const [needsSetup, setNeedsSetup] = useState(false);
  const retry = useCallback(() => {
    setConnected(null);
    setTermsAccepted(null);
    // /api/auth/status is public and reports both the access-key requirement
    // and whether the model is installed, so it is the right gate: calling
    // bootstrap first would 401 or time out before the user has seen why.
    api
      .terms()
      .then(async (t) => {
        if (!t.accepted) {
          // Stop here. Everything after this point reads private data.
          setTermsAccepted(false);
          setConnected(true);
          return;
        }
        setTermsAccepted(true);
        const s = await api.authStatus();
        // authStatus is public, so a 200 does not mean the key was right.
        // key_accepted is what says whether real calls will succeed.
        if (!s.key_accepted) {
          setFirstRun(false);
          setConnected(false);
          return;
        }
        if (!s.model_installed) {
          setFirstRun(false);
          setNeedsSetup(true);
          setConnected(true);
          return;
        }
        setNeedsSetup(false);
        const b = await api.bootstrap();
        setFirstRun(b.first_run);
        setConnected(true);
      })
      .catch(() => {
        setFirstRun(false);
        setConnected(false);
      });
  }, []);

  useEffect(retry, [retry]);

  if (connected === null) {
    return (
      <div className="flex h-screen items-center justify-center bg-paper">
        <div className="mono text-xs uppercase tracking-[0.18em] text-muted">
          Loading Circle
        </div>
      </div>
    );
  }

  if (!connected) {
    return <ConnectPage onConnected={retry} />;
  }

  if (termsAccepted === false) {
    return <ConsentPage onAccepted={retry} />;
  }

  if (needsSetup) {
    return <SetupPage onReady={retry} />;
  }

  if (firstRun) {
    return <FirstRun onDone={() => setFirstRun(false)} />;
  }

  return (
    <Layout
      key={location.pathname.split("/")[1] || "root"}
      searchScope={
        personId ? { personId, personName: "" } : undefined
      }
    >
      <Routes>
        <Route path="/" element={<Dashboard />} />
        <Route path="/person/:id" element={<PersonRoute />} />
        <Route path="/imports" element={<ImportsPage />} />
        <Route path="/settings" element={<SettingsPage />} />
        <Route path="*" element={<Dashboard />} />
      </Routes>
    </Layout>
  );
}

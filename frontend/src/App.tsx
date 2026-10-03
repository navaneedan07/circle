import { Route, Routes, useLocation, useParams } from "react-router-dom";
import { useCallback, useEffect, useState } from "react";
import Layout from "./components/Layout";
import Dashboard from "./pages/Dashboard";
import PersonPage from "./pages/PersonPage";
import ImportsPage from "./pages/ImportsPage";
import SettingsPage from "./pages/SettingsPage";
import FirstRun from "./pages/FirstRun";
import ConnectPage from "./pages/ConnectPage";
import { api, getConnection } from "./api";

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
  const [firstRun, setFirstRun] = useState<boolean | null>(null);
  const location = useLocation();
  const personId = location.pathname.startsWith("/person/")
    ? location.pathname.split("/")[2] ?? ""
    : "";

  const [connected, setConnected] = useState<boolean | null>(null);
  const retry = useCallback(() => {
    setConnected(null);
    // /api/auth/status is public and reports both the access-key requirement
    // and whether the model is installed, so it is the right gate: calling
    // bootstrap first would 401 or time out before the user has seen why.
    api
      .authStatus()
      .then(async (s) => {
        // authStatus is public, so a 200 does not mean the key was right.
        // key_accepted is what says whether real calls will succeed.
        if (!s.key_accepted) {
          setFirstRun(false);
          setConnected(false);
          return;
        }
        if (!s.model_installed) {
          setFirstRun(false);
          setConnected(false);
          return;
        }
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

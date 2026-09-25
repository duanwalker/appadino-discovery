import { useEffect, useState } from "react";
import { fetchClients } from "./api";
import type { Client } from "./types";
import { RunHealthBanner } from "./components/RunHealthBanner";
import { ExportButton } from "./components/ExportButton";
import { ProspectTable } from "./components/ProspectTable";
import { SuppressionManager } from "./components/SuppressionManager";

type Tab = "prospects" | "suppression";

// Internal dev tool: which client's data the dashboard is showing. No auth
// exists anywhere in this app — this is a bare operator convenience, not
// access control. Selection lives in component state only (session-scoped,
// resets on reload) per the current scope; not persisted to localStorage/URL.
function useClientSelector(): {
  clients: Client[];
  clientId: number | null;
  setClientId: (id: number) => void;
  loading: boolean;
  error: string | null;
} {
  const [clients, setClients] = useState<Client[]>([]);
  const [clientId, setClientId] = useState<number | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    fetchClients()
      .then((rows) => {
        setClients(rows);
        if (rows.length > 0) setClientId(rows[0].id);
      })
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false));
  }, []);

  return { clients, clientId, setClientId, loading, error };
}

function useReviewerName(): [string, (name: string) => void] {
  const [name, setName] = useState(() => {
    try {
      return localStorage.getItem("discovery.reviewerName") ?? "";
    } catch {
      return "";
    }
  });
  function update(next: string) {
    setName(next);
    try {
      localStorage.setItem("discovery.reviewerName", next);
    } catch {
      // per-viewer convenience only — fine if storage is unavailable
    }
  }
  return [name, update];
}

export default function App() {
  const [tab, setTab] = useState<Tab>("prospects");
  const [reviewerName, setReviewerName] = useReviewerName();
  const { clients, clientId, setClientId, loading, error } = useClientSelector();

  return (
    <div className="app">
      <header className="app-header">
        <h1>Discovery — Prospect Review</h1>
        <div className="app-header__controls">
          <div className="app-header__client">
            <label htmlFor="client-select">Client</label>
            <select
              id="client-select"
              value={clientId ?? ""}
              onChange={(e) => setClientId(Number(e.target.value))}
              disabled={clients.length === 0}
            >
              {clients.map((c) => (
                <option key={c.id} value={c.id}>
                  {c.name}
                </option>
              ))}
            </select>
          </div>
          <div className="app-header__reviewer">
            <label htmlFor="reviewer-name">Your name</label>
            <input
              id="reviewer-name"
              type="text"
              value={reviewerName}
              onChange={(e) => setReviewerName(e.target.value)}
              placeholder="e.g. Lauren"
            />
          </div>
        </div>
      </header>

      {loading ? (
        <p>Loading clients…</p>
      ) : error ? (
        <p role="alert">Failed to load clients: {error}</p>
      ) : clientId === null ? (
        <p role="alert">No clients found.</p>
      ) : (
        <>
          <RunHealthBanner clientId={clientId} />

          <nav className="tabs">
            <button
              type="button"
              className={`tab ${tab === "prospects" ? "tab--active" : ""}`}
              onClick={() => setTab("prospects")}
            >
              Prospects
            </button>
            <button
              type="button"
              className={`tab ${tab === "suppression" ? "tab--active" : ""}`}
              onClick={() => setTab("suppression")}
            >
              Suppression list
            </button>
            <div className="tabs__spacer" />
            {tab === "prospects" && <ExportButton clientId={clientId} />}
          </nav>

          <main>
            {tab === "prospects" ? (
              <ProspectTable clientId={clientId} reviewerName={reviewerName} />
            ) : (
              <SuppressionManager clientId={clientId} />
            )}
          </main>
        </>
      )}
    </div>
  );
}

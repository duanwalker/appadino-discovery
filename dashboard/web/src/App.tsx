import { useState } from "react";
import { RunHealthBanner } from "./components/RunHealthBanner";
import { ExportButton } from "./components/ExportButton";
import { ProspectTable } from "./components/ProspectTable";
import { SuppressionManager } from "./components/SuppressionManager";

// §5 "Multi-tenant from day one: client switcher hidden behind a config flag;
// single-tenant deployment." G2.x scopes to the existing client_id=2 dev dataset
// only — a real switcher (reading `clients`) is future work, not built this pass.
const CLIENT_ID = 2;

type Tab = "prospects" | "suppression";

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

  return (
    <div className="app">
      <header className="app-header">
        <h1>Discovery — Prospect Review</h1>
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
      </header>

      <RunHealthBanner clientId={CLIENT_ID} />

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
        {tab === "prospects" && <ExportButton clientId={CLIENT_ID} />}
      </nav>

      <main>
        {tab === "prospects" ? (
          <ProspectTable clientId={CLIENT_ID} reviewerName={reviewerName} />
        ) : (
          <SuppressionManager clientId={CLIENT_ID} />
        )}
      </main>
    </div>
  );
}

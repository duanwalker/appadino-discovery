import { useEffect, useState } from "react";
import { createSuppression, deleteSuppression, fetchSuppression } from "../api";
import type { SuppressionEntry } from "../types";

const KINDS: SuppressionEntry["kind"][] = ["client", "active_prospect", "partner_attribution"];

export function SuppressionManager({ clientId }: { clientId: number }) {
  const [entries, setEntries] = useState<SuppressionEntry[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [orgName, setOrgName] = useState("");
  const [ein, setEin] = useState("");
  const [kind, setKind] = useState<SuppressionEntry["kind"]>("client");
  const [submitting, setSubmitting] = useState(false);

  function load() {
    fetchSuppression(clientId)
      .then(setEntries)
      .catch((err) => setError(err.message));
  }

  useEffect(load, [clientId]);

  async function handleAdd(e: React.FormEvent) {
    e.preventDefault();
    if (!orgName.trim()) return;
    setSubmitting(true);
    setError(null);
    try {
      await createSuppression({
        client_id: clientId,
        org_name: orgName.trim(),
        ein: ein.trim() || undefined,
        kind,
        source: "dashboard manual entry",
      });
      setOrgName("");
      setEin("");
      load();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSubmitting(false);
    }
  }

  async function handleRemove(id: number) {
    setError(null);
    try {
      await deleteSuppression(id);
      load();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  return (
    <div className="suppression-manager">
      <form className="suppression-form" onSubmit={handleAdd}>
        <input
          type="text"
          placeholder="Organization name"
          value={orgName}
          onChange={(e) => setOrgName(e.target.value)}
          required
        />
        <input type="text" placeholder="EIN (optional)" value={ein} onChange={(e) => setEin(e.target.value)} />
        <select value={kind} onChange={(e) => setKind(e.target.value as SuppressionEntry["kind"])}>
          {KINDS.map((k) => (
            <option key={k} value={k}>
              {k.replace(/_/g, " ")}
            </option>
          ))}
        </select>
        <button type="submit" className="button button--primary" disabled={submitting}>
          Add entry
        </button>
      </form>

      {error && <div className="banner banner--error">{error}</div>}
      {entries === null ? (
        <div className="banner">Loading suppression list…</div>
      ) : entries.length === 0 ? (
        <div className="banner">No suppression entries for this client.</div>
      ) : (
        <table className="prospect-table">
          <thead>
            <tr>
              <th>Organization</th>
              <th>EIN</th>
              <th>Kind</th>
              <th>Source</th>
              <th>Added</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {entries.map((entry) => (
              <tr key={entry.id}>
                <td>{entry.org_name}</td>
                <td>{entry.ein ?? "—"}</td>
                <td>{entry.kind.replace(/_/g, " ")}</td>
                <td>{entry.source ?? "—"}</td>
                <td>{new Date(entry.added_at).toLocaleDateString()}</td>
                <td>
                  <button type="button" className="button button--link" onClick={() => handleRemove(entry.id)}>
                    Remove
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

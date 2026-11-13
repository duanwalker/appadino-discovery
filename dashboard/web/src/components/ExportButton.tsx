import { useState } from "react";
import { fetchExportCsv } from "../api";

export function ExportButton({ clientId }: { clientId: number }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function download() {
    setBusy(true);
    setError(null);
    try {
      const { blob, filename } = await fetchExportCsv(clientId);
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = filename;
      document.body.appendChild(link);
      link.click();
      link.remove();
      // Deferred: revoking in the same tick as click() can cancel the download.
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <button type="button" className="button button--primary" onClick={download} disabled={busy}>
        {busy ? "Exporting…" : "Export CSV"}
      </button>
      {error && <span role="alert">Export failed: {error}</span>}
    </>
  );
}

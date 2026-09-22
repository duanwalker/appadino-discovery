import { useEffect, useState } from "react";
import { fetchRuns } from "../api";
import type { Run } from "../types";

// §5 "Run history + last-run health banner" — sourced from the last publish-stage
// run (that's the stage that writes qa_mismatch_rate/published into `counts`, per
// stages/run_publish.py). QA_MISMATCH_PAGE_THRESHOLD there is 0.10 — mirrored here
// for the warning color, not re-derived from anywhere authoritative.
const QA_MISMATCH_WARN_THRESHOLD = 0.1;

function formatTimestamp(iso: string | null): string {
  if (!iso) return "—";
  return new Date(iso).toLocaleString();
}

export function RunHealthBanner({ clientId }: { clientId: number }) {
  const [run, setRun] = useState<Run | null | undefined>(undefined);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    fetchRuns(clientId, "publish", 1)
      .then((runs) => setRun(runs[0] ?? null))
      .catch((err) => setError(err.message));
  }, [clientId]);

  if (error) {
    return <div className="run-banner run-banner--failed">Could not load run history: {error}</div>;
  }
  if (run === undefined) {
    return <div className="run-banner">Loading run history…</div>;
  }
  if (run === null) {
    return <div className="run-banner">No publish runs recorded yet for this client.</div>;
  }

  const counts = run.counts || {};
  const published = counts.published as number | undefined;
  const mismatchRate = counts.qa_mismatch_rate as number | null | undefined;
  const isFailed = run.status !== "success";
  const isMismatchHigh = typeof mismatchRate === "number" && mismatchRate > QA_MISMATCH_WARN_THRESHOLD;

  return (
    <div className={`run-banner ${isFailed ? "run-banner--failed" : ""}`}>
      <span className="run-banner__item">
        <strong>Last run:</strong> {formatTimestamp(run.finished_at)}
      </span>
      <span className="run-banner__item">
        <strong>Status:</strong>{" "}
        <span className={isFailed ? "badge badge--danger" : "badge badge--success"}>{run.status}</span>
      </span>
      {published !== undefined && (
        <span className="run-banner__item">
          <strong>Published:</strong> {published}
        </span>
      )}
      {mismatchRate !== undefined && mismatchRate !== null && (
        <span className="run-banner__item">
          <strong>QA mismatch rate:</strong>{" "}
          <span className={isMismatchHigh ? "badge badge--danger" : "badge badge--success"}>
            {(mismatchRate * 100).toFixed(1)}%
          </span>
        </span>
      )}
      {isFailed && run.error && <span className="run-banner__error">{run.error}</span>}
    </div>
  );
}

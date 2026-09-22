import { useState } from "react";
import { reviewSuppressionFlag } from "../api";

// §4 Stage 4 / §9 rule 7: fuzzy suppression matches are flagged, never silently
// dropped — this surfaces prospects.suppression_flag for a human confirm/dismiss.
// "Confirm" adds a real `suppression` entry (this org will be excluded outright on
// future runs); "dismiss" clears the flag as a false positive.
export function SuppressionFlagBanner({
  prospectId,
  flag,
  reviewerName,
  onResolved,
}: {
  prospectId: number;
  flag: string;
  reviewerName: string;
  onResolved: () => void;
}) {
  const [busy, setBusy] = useState<"confirm" | "dismiss" | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function handle(action: "confirm" | "dismiss") {
    setBusy(action);
    setError(null);
    try {
      await reviewSuppressionFlag(prospectId, action, reviewerName);
      onResolved();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="suppression-flag">
      <span>⚠ {flag}</span>
      <div className="suppression-flag__actions">
        <button
          type="button"
          className="button button--danger"
          disabled={busy !== null || !reviewerName}
          onClick={() => handle("confirm")}
        >
          {busy === "confirm" ? "Suppressing…" : "Confirm (suppress)"}
        </button>
        <button
          type="button"
          className="button"
          disabled={busy !== null || !reviewerName}
          onClick={() => handle("dismiss")}
        >
          {busy === "dismiss" ? "Dismissing…" : "Dismiss (false match)"}
        </button>
      </div>
      {!reviewerName && <div className="suppression-flag__hint">Enter your name above to review this flag.</div>}
      {error && <div className="suppression-flag__error">{error}</div>}
    </div>
  );
}

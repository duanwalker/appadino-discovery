import { useCallback, useEffect, useRef, useState } from "react";
import { checkEnrichStatus, enrichProspect } from "../api";

const POLL_INTERVAL_MS = 3000;
// ~2 minutes of client-side polling before telling the user to check back later.
// enrich_prospect already waits a bounded window server-side before returning
// "pending" at all, so reaching this cap means FullEnrich itself is slow, not that
// something's stuck — see function_app.py's ENRICH_POLL_ATTEMPTS comment for why
// there's no measured latency number this is tuned against.
const MAX_CLIENT_POLLS = 40;

export function useEnrich(prospectId: number, onEnriched: () => void) {
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const cancelled = useRef(false);

  useEffect(
    () => () => {
      cancelled.current = true;
    },
    []
  );

  const pollUntilDone = useCallback(
    async (jobId: string, attempt: number) => {
      if (cancelled.current) return;
      if (attempt >= MAX_CLIENT_POLLS) {
        setPending(false);
        setError("Still processing at FullEnrich — check back in a bit.");
        return;
      }
      try {
        const result = await checkEnrichStatus(prospectId, jobId);
        if (cancelled.current) return;
        if (result.status === "done") {
          setPending(false);
          onEnriched();
          return;
        }
        setTimeout(() => pollUntilDone(jobId, attempt + 1), POLL_INTERVAL_MS);
      } catch (err) {
        if (cancelled.current) return;
        setPending(false);
        setError(err instanceof Error ? err.message : String(err));
      }
    },
    [prospectId, onEnriched]
  );

  const trigger = useCallback(async () => {
    setPending(true);
    setError(null);
    try {
      const result = await enrichProspect(prospectId);
      if (cancelled.current) return;
      if (result.status === "done") {
        setPending(false);
        onEnriched();
      } else {
        pollUntilDone(result.job_id, 0);
      }
    } catch (err) {
      if (cancelled.current) return;
      setPending(false);
      setError(err instanceof Error ? err.message : String(err));
    }
  }, [prospectId, onEnriched, pollUntilDone]);

  return { pending, error, trigger };
}

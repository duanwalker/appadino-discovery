import type { EnrichResponse } from "../api";

export const POLL_INTERVAL_MS = 3000;
// ~2 minutes of client-side polling before telling the user to check back later.
// enrich_prospect already waits a bounded window server-side before returning
// "pending" at all, so reaching this cap means FullEnrich itself is slow, not that
// something's stuck — see function_app.py's ENRICH_POLL_ATTEMPTS comment for why
// there's no measured latency number this is tuned against. Wall-clock, not a poll
// count: a hidden tab's throttled timers can stretch a fixed count far past 2 min.
export const MAX_POLL_MS = 2 * 60 * 1000;

export const STILL_PROCESSING_MESSAGE = "Still processing at FullEnrich — check back in a bit.";

export interface EnrichFlowDeps {
  enrich: () => Promise<EnrichResponse>;
  checkStatus: (jobId: string) => Promise<EnrichResponse>;
  setPending: (pending: boolean) => void;
  setError: (message: string | null) => void;
  onDone: () => void;
  isCancelled: () => boolean;
  now?: () => number;
  sleep?: (ms: number) => Promise<void>;
}

/** Kept free of React so it runs under `node --test` (tests/enrichFlow.test.ts).
 * Every exit path clears pending: done, error, malformed response, or the cap. */
export async function runEnrich(deps: EnrichFlowDeps): Promise<void> {
  const now = deps.now ?? Date.now;
  const sleep = deps.sleep ?? ((ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms)));
  const fail = (message: string) => {
    deps.setPending(false);
    deps.setError(message);
  };

  deps.setPending(true);
  deps.setError(null);
  const deadline = now() + MAX_POLL_MS;
  try {
    let result = await deps.enrich();
    while (!deps.isCancelled()) {
      if (result?.status === "done") {
        deps.setPending(false);
        deps.onDone();
        return;
      }
      if (result?.status !== "pending") {
        fail("Unexpected response from the server.");
        return;
      }
      const jobId = result.job_id;
      if (!jobId) {
        // Another request holds the claim but hasn't heard back from FullEnrich yet.
        fail("An enrichment for this org is already in progress — refresh in a moment.");
        return;
      }
      await sleep(POLL_INTERVAL_MS);
      if (deps.isCancelled()) return;
      if (now() >= deadline) {
        fail(STILL_PROCESSING_MESSAGE);
        return;
      }
      result = await deps.checkStatus(jobId);
    }
  } catch (err) {
    if (deps.isCancelled()) return;
    fail(err instanceof Error ? err.message : String(err));
  }
}

import { useCallback, useEffect, useState } from "react";
import { checkEnrichStatus, enrichProspect } from "../api";
import { runEnrich } from "./enrichFlow";
import { createMountedFlag } from "./mountedFlag";

export function useEnrich(prospectId: number, onEnriched: () => void) {
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // One flag per component instance (useState keeps it across StrictMode's
  // simulated remount); its effect resets it on mount — see mountedFlag.ts.
  const [mounted] = useState(createMountedFlag);

  useEffect(mounted.effect, [mounted]);

  const trigger = useCallback(
    () =>
      runEnrich({
        enrich: () => enrichProspect(prospectId),
        checkStatus: (jobId) => checkEnrichStatus(prospectId, jobId),
        setPending,
        setError,
        onDone: onEnriched,
        isCancelled: mounted.isCancelled,
      }),
    [prospectId, onEnriched, mounted]
  );

  return { pending, error, trigger };
}

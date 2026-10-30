import { useCallback, useEffect, useRef, useState } from "react";
import { checkEnrichStatus, enrichProspect } from "../api";
import { runEnrich } from "./enrichFlow";

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

  const trigger = useCallback(
    () =>
      runEnrich({
        enrich: () => enrichProspect(prospectId),
        checkStatus: (jobId) => checkEnrichStatus(prospectId, jobId),
        setPending,
        setError,
        onDone: onEnriched,
        isCancelled: () => cancelled.current,
      }),
    [prospectId, onEnriched]
  );

  return { pending, error, trigger };
}

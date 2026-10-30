/** A cancellation flag tied to a component's mount lifecycle, kept free of React so
 * it runs under `node --test` (tests/mountedFlag.test.ts).
 *
 * React StrictMode (main.tsx, development) runs mount -> cleanup -> mount on every
 * component. Setting the flag only in the cleanup leaves it "cancelled" for the
 * component's whole real life, so every async flow checking it bails silently (the
 * Enrich spinner that never cleared). `effect` resets it on each mount instead. */
export interface MountedFlag {
  isCancelled: () => boolean;
  /** Pass straight to useEffect: marks mounted, returns the cleanup that cancels. */
  effect: () => () => void;
}

export function createMountedFlag(): MountedFlag {
  let cancelled = false;
  return {
    isCancelled: () => cancelled,
    effect: () => {
      cancelled = false;
      return () => {
        cancelled = true;
      };
    },
  };
}

// LOCAL DEV ONLY: which customer the Functions API's local operator mode should show.
//
// Under `func start` with DISCOVERY_LOCAL_OPERATOR_MODE on (see
// dashboard/functions_api/README.md), the API skips login and takes client_id from
// this header. The deployed API never reads it (operator mode refuses to start in
// Azure), and a production build never sends it: main.tsx enables this only when
// Vite's dev server is running (import.meta.env.DEV), and it starts disabled.
//
// The selected client is recorded synchronously when the switcher changes, before
// React re-renders, because child components fetch in effects, and React runs child
// effects before parent effects.

export const LOCAL_OPERATOR_CLIENT_HEADER = "x-local-operator-client-id";

let enabled = false;
let selectedClientId: number | null = null;

export function enableOperatorHeader(isViteDevServer: boolean): void {
  enabled = isViteDevServer;
}

export function setOperatorClientId(clientId: number | null): void {
  selectedClientId = clientId;
}

export function operatorHeaders(): Record<string, string> {
  if (!enabled || selectedClientId === null) return {};
  return { [LOCAL_OPERATOR_CLIENT_HEADER]: String(selectedClientId) };
}

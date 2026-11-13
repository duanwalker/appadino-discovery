import { operatorHeaders } from "./operatorHeader.ts";
import type { Client, Prospect, ProspectStatus, Run, SuppressionEntry } from "./types";

async function errorMessage(res: Response): Promise<string> {
  const text = await res.text();
  try {
    const message = (JSON.parse(text) as { error?: unknown } | null)?.error;
    if (typeof message === "string" && message) return message;
  } catch {
    // fall through to the generic message
  }
  return `Request failed (HTTP ${res.status})`;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    ...init,
    // operatorHeaders() is {} except under the Vite dev server (see operatorHeader.ts).
    headers: { "Content-Type": "application/json", ...operatorHeaders(), ...init?.headers },
  });
  // Read as text first: an unhandled server error can arrive with an empty body, and
  // the message shown on the row should be short either way, not a JSON parse error.
  const text = await res.text();
  let body: unknown = null;
  try {
    body = text ? JSON.parse(text) : null;
  } catch {
    body = null;
  }
  if (!res.ok) {
    const message = (body as { error?: unknown } | null)?.error;
    throw new Error(typeof message === "string" && message ? message : `Request failed (HTTP ${res.status})`);
  }
  if (body === null) {
    throw new Error(`Empty or unreadable response from the server (HTTP ${res.status})`);
  }
  return body as T;
}

// LOCAL OPERATOR MODE ONLY: /api/clients exists only when the Functions API runs
// under `func start` with DISCOVERY_LOCAL_OPERATOR_MODE on (function_app.py's
// list_clients_local_operator). The deployed API has no such route.
export function fetchClients(): Promise<Client[]> {
  return request(`/api/clients`);
}

export function fetchProspects(clientId: number): Promise<Prospect[]> {
  return request(`/api/prospects?client_id=${clientId}`);
}

export function updateProspect(
  id: number,
  updates: { status?: ProspectStatus; notes?: string; updated_by: string }
): Promise<{ id: number; status: string }> {
  return request(`/api/prospects/${id}`, { method: "PATCH", body: JSON.stringify(updates) });
}

export function reviewSuppressionFlag(
  id: number,
  action: "confirm" | "dismiss",
  updatedBy: string
): Promise<{ id: number; action: string }> {
  return request(`/api/prospects/${id}/suppression-review`, {
    method: "POST",
    body: JSON.stringify({ action, updated_by: updatedBy }),
  });
}

export function fetchSuppression(clientId: number): Promise<SuppressionEntry[]> {
  return request(`/api/suppression?client_id=${clientId}`);
}

export function createSuppression(entry: {
  client_id: number;
  ein?: string;
  org_name: string;
  kind: "client" | "active_prospect" | "partner_attribution";
  source?: string;
}): Promise<{ id: number }> {
  return request(`/api/suppression`, { method: "POST", body: JSON.stringify(entry) });
}

export function deleteSuppression(id: number): Promise<{ id: number; status: string }> {
  return request(`/api/suppression/${id}`, { method: "DELETE" });
}

export function fetchRuns(clientId: number, stage?: string, limit = 10): Promise<Run[]> {
  const params = new URLSearchParams({ client_id: String(clientId), limit: String(limit) });
  if (stage) params.set("stage", stage);
  return request(`/api/runs?${params}`);
}

// A fetch rather than a plain <a href> download: a link click can't carry the
// local operator header. The filename comes from the server's Content-Disposition.
export async function fetchExportCsv(clientId: number): Promise<{ blob: Blob; filename: string }> {
  const res = await fetch(`/api/export.csv?client_id=${clientId}`, { headers: operatorHeaders() });
  if (!res.ok) throw new Error(await errorMessage(res));
  const disposition = res.headers.get("Content-Disposition") ?? "";
  const filename = /filename="?([^";]+)"?/.exec(disposition)?.[1] ?? `prospects_client${clientId}.csv`;
  return { blob: await res.blob(), filename };
}

export interface EnrichContactResult {
  contact_name: string | null;
  contact_title: string | null;
  email: string | null;
  email_status: string | null;
  stale_detail: string | null;
  phone: string | null;
  provider_confidence: number;
  credits_spent: number;
}

export type EnrichResponse =
  // job_id is absent while another request holds the claim but has no job id yet.
  | { status: "pending"; job_id?: string }
  | { status: "done"; enrichment_id: number; contact: EnrichContactResult };

// §5.5/E2: submit-then-poll, not synchronous (FullEnrich's real API, confirmed by
// E1) — this call itself already waits a short bounded window server-side before
// returning "pending", so a "pending" response here means the caller should start
// polling checkEnrichStatus, not that nothing happened yet.
export function enrichProspect(id: number): Promise<EnrichResponse> {
  return request(`/api/prospects/${id}/enrich`, { method: "POST" });
}

export function checkEnrichStatus(id: number, jobId: string): Promise<EnrichResponse> {
  return request(`/api/prospects/${id}/enrich-status?job_id=${encodeURIComponent(jobId)}`);
}

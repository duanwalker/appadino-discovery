import type { Prospect, ProspectStatus, Run, SuppressionEntry } from "./types";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    ...init,
    headers: { "Content-Type": "application/json", ...init?.headers },
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.error || `${res.status} ${res.statusText}`);
  }
  return res.json() as Promise<T>;
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

export function exportCsvUrl(clientId: number): string {
  return `/api/export.csv?client_id=${clientId}`;
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
  | { status: "pending"; job_id: string }
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

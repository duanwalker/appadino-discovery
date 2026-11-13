// Offline: the local operator header (x-local-operator-client-id) is sent on every
// /api call when enabled (Vite dev server) and never when disabled (production build).
// fetch is stubbed; nothing leaves the process.
import assert from "node:assert/strict";
import { afterEach, beforeEach, test } from "node:test";
import * as api from "../src/api.ts";
import {
  LOCAL_OPERATOR_CLIENT_HEADER,
  enableOperatorHeader,
  operatorHeaders,
  setOperatorClientId,
} from "../src/operatorHeader.ts";

const realFetch = globalThis.fetch;

interface Seen {
  url: string;
  headers: Headers;
}

let seen: Seen[] = [];

beforeEach(() => {
  seen = [];
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    seen.push({ url: String(input), headers: new Headers(init?.headers) });
    return new Response(JSON.stringify({ ok: true }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  }) as typeof fetch;
});

afterEach(() => {
  globalThis.fetch = realFetch;
  enableOperatorHeader(false);
  setOperatorClientId(null);
});

// One call per exported API function. Pinned against the module's exports below, so a
// new /api function that skips the shared headers can't be added without a test.
const CALLS: Record<string, () => Promise<unknown>> = {
  fetchClients: () => api.fetchClients(),
  fetchProspects: () => api.fetchProspects(2),
  updateProspect: () => api.updateProspect(1, { status: "reviewed", updated_by: "x" }),
  reviewSuppressionFlag: () => api.reviewSuppressionFlag(1, "dismiss", "x"),
  fetchSuppression: () => api.fetchSuppression(2),
  createSuppression: () => api.createSuppression({ client_id: 2, org_name: "Org", kind: "client" }),
  deleteSuppression: () => api.deleteSuppression(1),
  fetchRuns: () => api.fetchRuns(2, "publish"),
  fetchExportCsv: () => api.fetchExportCsv(2),
  enrichProspect: () => api.enrichProspect(1),
  checkEnrichStatus: () => api.checkEnrichStatus(1, "job-1"),
};

test("the call table covers every exported API function", () => {
  const exported = Object.entries(api)
    .filter(([, value]) => typeof value === "function")
    .map(([name]) => name)
    .sort();
  assert.deepEqual(Object.keys(CALLS).sort(), exported);
});

test("disabled by default: no header even with a client selected", async () => {
  setOperatorClientId(2);
  for (const call of Object.values(CALLS)) await call();
  assert.equal(seen.length, Object.keys(CALLS).length);
  for (const { url, headers } of seen) {
    assert.equal(headers.get(LOCAL_OPERATOR_CLIENT_HEADER), null, url);
  }
});

test("production build (disabled): no header on any call", async () => {
  enableOperatorHeader(false);
  setOperatorClientId(3);
  for (const call of Object.values(CALLS)) await call();
  for (const { url, headers } of seen) {
    assert.equal(headers.get(LOCAL_OPERATOR_CLIENT_HEADER), null, url);
  }
});

test("dev server (enabled): every /api call carries the selected client", async () => {
  enableOperatorHeader(true);
  setOperatorClientId(2);
  for (const call of Object.values(CALLS)) await call();
  assert.equal(seen.length, Object.keys(CALLS).length);
  for (const { url, headers } of seen) {
    assert.ok(url.startsWith("/api/"), url);
    assert.equal(headers.get(LOCAL_OPERATOR_CLIENT_HEADER), "2", url);
  }
});

test("switching client changes the header on the very next call", async () => {
  enableOperatorHeader(true);
  setOperatorClientId(2);
  await api.fetchProspects(2);
  setOperatorClientId(3);
  await api.fetchProspects(3);
  assert.deepEqual(
    seen.map(({ headers }) => headers.get(LOCAL_OPERATOR_CLIENT_HEADER)),
    ["2", "3"],
  );
});

test("enabled but no client chosen yet: no header (e.g. the /api/clients call itself)", async () => {
  enableOperatorHeader(true);
  await api.fetchClients();
  assert.equal(seen[0].headers.get(LOCAL_OPERATOR_CLIENT_HEADER), null);
  assert.deepEqual(operatorHeaders(), {});
});

test("the header does not replace the JSON content type or caller headers", async () => {
  enableOperatorHeader(true);
  setOperatorClientId(2);
  await api.updateProspect(1, { notes: "n", updated_by: "x" });
  assert.equal(seen[0].headers.get("Content-Type"), "application/json");
  assert.equal(seen[0].headers.get(LOCAL_OPERATOR_CLIENT_HEADER), "2");
});

test("CSV export uses the server's filename and surfaces API errors", async () => {
  globalThis.fetch = (async () =>
    new Response("EIN\n123\n", {
      status: 200,
      headers: { "Content-Disposition": "attachment; filename=prospects_client2.csv" },
    })) as typeof fetch;
  const { blob, filename } = await api.fetchExportCsv(2);
  assert.equal(filename, "prospects_client2.csv");
  assert.equal(await blob.text(), "EIN\n123\n");

  globalThis.fetch = (async () =>
    new Response(JSON.stringify({ error: "local operator mode: send the header" }), {
      status: 400,
    })) as typeof fetch;
  await assert.rejects(api.fetchExportCsv(2), /local operator mode: send the header/);
});

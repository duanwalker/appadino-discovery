// Offline: runs under Node's built-in runner with native TypeScript stripping
// (`npm test`, Node >= 23.6). fetch is stubbed; nothing leaves the process.
import assert from "node:assert/strict";
import { afterEach, test } from "node:test";
import { enrichProspect } from "../src/api.ts";
import { MAX_POLL_MS, STILL_PROCESSING_MESSAGE, runEnrich } from "../src/hooks/enrichFlow.ts";

const realFetch = globalThis.fetch;
afterEach(() => {
  globalThis.fetch = realFetch;
});

function recorder() {
  const pending: boolean[] = [];
  const errors: (string | null)[] = [];
  let done = 0;
  return {
    pending,
    errors,
    doneCount: () => done,
    deps: {
      setPending: (p: boolean) => pending.push(p),
      setError: (e: string | null) => errors.push(e),
      onDone: () => {
        done += 1;
      },
      isCancelled: () => false,
      sleep: async () => {},
    },
  };
}

test("enrich POST answering 500 with an empty body clears pending and shows a short error", async () => {
  globalThis.fetch = async () => new Response("", { status: 500 });
  const r = recorder();

  await runEnrich({
    ...r.deps,
    enrich: () => enrichProspect(779),
    checkStatus: () => assert.fail("must not poll after a failed submit"),
  });

  assert.deepEqual(r.pending, [true, false]);
  assert.equal(r.errors.at(-1), "Request failed (HTTP 500)");
  assert.equal(r.doneCount(), 0);
});

test("enrich POST answering 200 with an empty body clears pending and shows a short error", async () => {
  globalThis.fetch = async () => new Response("", { status: 200 });
  const r = recorder();

  await runEnrich({ ...r.deps, enrich: () => enrichProspect(779), checkStatus: () => assert.fail("no poll") });

  assert.deepEqual(r.pending, [true, false]);
  assert.match(r.errors.at(-1) ?? "", /^Empty or unreadable response/);
});

test("provider rejection (502 JSON) surfaces the server's message", async () => {
  const body = { error: "FullEnrich rejected the enrichment request (HTTP 400, error.enrichment.first_name.empty)" };
  globalThis.fetch = async () => new Response(JSON.stringify(body), { status: 502 });
  const r = recorder();

  await runEnrich({ ...r.deps, enrich: () => enrichProspect(779), checkStatus: () => assert.fail("no poll") });

  assert.deepEqual(r.pending, [true, false]);
  assert.equal(r.errors.at(-1), body.error);
});

test("polling stops with a message once the wall-clock cap passes (hidden tab)", async () => {
  // A hidden tab's throttled timer resumes long after it was scheduled: the clock
  // jumps past the cap during the first sleep, so no further poll may be sent.
  let clock = 0;
  let checks = 0;
  const r = recorder();

  await runEnrich({
    ...r.deps,
    now: () => clock,
    sleep: async () => {
      clock += MAX_POLL_MS + 1;
    },
    enrich: async () => ({ status: "pending", job_id: "job-1" }),
    checkStatus: async () => {
      checks += 1;
      return { status: "pending", job_id: "job-1" };
    },
  });

  assert.equal(checks, 0);
  assert.deepEqual(r.pending, [true, false]);
  assert.equal(r.errors.at(-1), STILL_PROCESSING_MESSAGE);
});

test("pending without a job id stops instead of polling job_id=undefined", async () => {
  const r = recorder();

  await runEnrich({
    ...r.deps,
    enrich: async () => ({ status: "pending" }),
    checkStatus: () => assert.fail("must not poll without a job id"),
  });

  assert.deepEqual(r.pending, [true, false]);
  assert.match(r.errors.at(-1) ?? "", /already in progress/);
});

test("resolves through a status poll and calls onDone", async () => {
  const r = recorder();
  const done = { status: "done" as const, enrichment_id: 1, contact: {} as never };

  await runEnrich({
    ...r.deps,
    enrich: async () => ({ status: "pending", job_id: "job-1" }),
    checkStatus: async (jobId) => {
      assert.equal(jobId, "job-1");
      return done;
    },
  });

  assert.deepEqual(r.pending, [true, false]);
  assert.equal(r.doneCount(), 1);
});

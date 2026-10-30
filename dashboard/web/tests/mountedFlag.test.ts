// Offline regression test for the StrictMode cancellation bug: POST /enrich returned
// 202 {status: "pending", job_id}, no enrich-status request followed, and the spinner
// never cleared, because the hook's cancel flag stayed true after StrictMode's
// mount -> cleanup -> mount.
import assert from "node:assert/strict";
import { test } from "node:test";
import { runEnrich } from "../src/hooks/enrichFlow.ts";
import { createMountedFlag } from "../src/hooks/mountedFlag.ts";

/** StrictMode's development sequence, driving the flag exactly as useEffect would. */
function strictModeMount(effect: () => () => void): () => void {
  effect()();
  return effect();
}

test("flag is not cancelled after StrictMode's mount -> cleanup -> mount", () => {
  const flag = createMountedFlag();

  strictModeMount(flag.effect);

  assert.equal(flag.isCancelled(), false);
});

test("a real unmount still cancels", () => {
  const flag = createMountedFlag();
  const unmount = strictModeMount(flag.effect);

  unmount();

  assert.equal(flag.isCancelled(), true);
});

test("after a StrictMode remount, a pending enrich polls and clears the spinner", async () => {
  const flag = createMountedFlag();
  strictModeMount(flag.effect);
  const pending: boolean[] = [];
  const polled: string[] = [];
  let done = 0;

  await runEnrich({
    enrich: async () => ({ status: "pending", job_id: "job-1" }),
    checkStatus: async (jobId) => {
      polled.push(jobId);
      return { status: "done", enrichment_id: 1, contact: {} as never };
    },
    setPending: (p) => pending.push(p),
    setError: () => {},
    onDone: () => {
      done += 1;
    },
    isCancelled: flag.isCancelled,
    sleep: async () => {},
  });

  assert.deepEqual(polled, ["job-1"]);
  assert.deepEqual(pending, [true, false]);
  assert.equal(done, 1);
});

test("after a StrictMode remount, a failed enrich still surfaces its error", async () => {
  const flag = createMountedFlag();
  strictModeMount(flag.effect);
  const pending: boolean[] = [];
  const errors: (string | null)[] = [];

  await runEnrich({
    enrich: async () => {
      throw new Error("Request failed (HTTP 500)");
    },
    checkStatus: () => assert.fail("must not poll after a failed submit"),
    setPending: (p) => pending.push(p),
    setError: (e) => errors.push(e),
    onDone: () => assert.fail("not done"),
    isCancelled: flag.isCancelled,
  });

  assert.deepEqual(pending, [true, false]);
  assert.equal(errors.at(-1), "Request failed (HTTP 500)");
});

test("unmounting mid-flight stops polling without touching state", async () => {
  const flag = createMountedFlag();
  const unmount = strictModeMount(flag.effect);
  const pending: boolean[] = [];
  const errors: (string | null)[] = [];

  await runEnrich({
    enrich: async () => {
      unmount();
      return { status: "pending", job_id: "job-1" };
    },
    checkStatus: () => assert.fail("must not poll after unmount"),
    setPending: (p) => pending.push(p),
    setError: (e) => errors.push(e),
    onDone: () => assert.fail("no state updates after unmount"),
    isCancelled: flag.isCancelled,
    sleep: async () => {},
  });

  // Only the pre-request updates; nothing after the unmount.
  assert.deepEqual(pending, [true]);
  assert.deepEqual(errors, [null]);
});

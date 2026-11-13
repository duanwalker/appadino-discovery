# Gate report: local dashboard against the operator-mode API (v1 item 5, part of gate 2)

Date: 2026-11-12. **Not committed, not deployed. Azure and `infra/` were not
touched.** The change is in `gate.patch` against HEAD `1db105d`. The API, the login
logic and the tests that guard them are unchanged.

## What changed

**Dashboard (`dashboard/web`)**

- New `src/operatorHeader.ts` holds the local operator header state. It is
  **disabled by default**. `main.tsx` calls `enableOperatorHeader(import.meta.env.DEV)`,
  so it is on only under the Vite dev server.
  - Checked in the production bundle: the call is compiled to
    `enableOperatorHeader(!1)`, i.e. `false`, so a production build never sends
    the header.
  - Checked on the dev server: `import.meta.env.DEV` is `true` there.
- `api.ts`: the shared `request()` adds `operatorHeaders()` to every `/api` call. When
  disabled, or before a client is chosen, that adds nothing.
- `App.tsx`: the client switcher records the chosen client synchronously, **before**
  the React state update. Child components fetch in effects, and React runs child
  effects before parent effects, so recording it in an effect would send the
  previous client on the first fetches after a switch.
- **CSV export was a plain `<a href download>` link**, and a link click can't carry
  a custom header. In operator mode it would always have returned 400.
  - `ExportButton` now fetches `fetchExportCsv()` (with headers) and saves the
    returned file as a download.
  - The filename comes from the server's `Content-Disposition`, and API errors
    show next to the button.
  - The same code path runs in production; there it relies on the SWA login
    cookie, which a same-origin fetch sends.
- New `src/vite-env.d.ts` (`/// <reference types="vite/client" />`) so TypeScript
  knows `import.meta.env`.
- Not changed: the dashboard still sends `?client_id=` (the API ignores it), and
  the "Your name" box remains (the API records the operator email instead). Both
  go in the rest of gate 2.

**Functions API files (docs and config only)**

- `local.settings.json.example`: added `DISCOVERY_LOCAL_OPERATOR_MODE: ""` (off)
  and `DISCOVERY_LOCAL_OPERATOR_EMAIL: "<you@example.com>"`. The mode placeholder is
  an empty string on purpose: any non-empty value other than the exact one makes
  the app refuse to start.
- `README.md`: how to turn the mode on in your own `local.settings.json`, and a
  note that the dashboard now sends the header in dev.

## Tests and checks

**`npm test`: 21 passed, 0 failed.** 9 of these are new in
`tests/operatorHeader.test.ts`. They call every exported API function (pinned to
the module's exports, so a new function can't skip the header untested) with
`fetch` stubbed, and check:

- no header when disabled (the production case), even with a client selected;
- every call carries the selected client when enabled;
- a switch takes effect on the very next call;
- no header before a client is chosen (the `/api/clients` call itself);
- `Content-Type` is preserved;
- the CSV filename is parsed and API errors are shown.

| Check | Result |
|---|---|
| `npx tsc --noEmit` | clean |
| `npm run build` | succeeded; the bundle check above passed |
| Functions API `pytest`, Docker on, with `TEST_DATABASE_URL` set to a throwaway container | **701 passed, 29 xfailed** in each of 3 full runs. See "Unexpected" for the first run. |
| Committed suite alone (`--ignore` on the unexpected file) | 572 passed, 4 xfailed, the same as at gate 1 |
| `ruff` | no Python file touched by this change. `dashboard/functions_api` shows the same 9 findings as at HEAD, plus 16 in the unexpected file. |
| `local.settings.json.example` | valid JSON |

**Live end-to-end check** against a throwaway Docker Postgres, never your real
database:

- Setup: migrated with `alembic upgrade head` and seeded with synthetic prospects
  (client 1: 4, client 2: 7). `func start` ran in operator mode from a scratch copy
  of the API with its own settings file, so your real `local.settings.json` was
  never read. Vite's dev server ran from the real `dashboard/web`.
- Results, all through Vite's proxy at `http://localhost:5173`:
  - `/api/clients` listed both clients;
  - `/api/prospects?client_id=99` with header 1 returned exactly client 1's 4
    rows, and with header 2 exactly client 2's 7;
  - no header returned a clear 400;
  - `/api/export.csv` returned `prospects_client2.csv` with 7 rows;
  - `/` returned 200.
- Everything was stopped and the container removed afterwards.

## Unexpected

1. **An untracked file appeared during this session that I did not write:
   `dashboard/functions_api/tests/test_access_control_extra.py`** (15.7 KB,
   20:09). Its docstring calls it "Independent gate-1 attacks", with 25 strict
   xfails for unfixed gaps. I left it alone and it is **not in the patch**. It
   adds 129 passing tests and 25 xfails to the run, and 16 ruff findings.
2. **The first full API run had 1 failure** (700 passed, 29 xfailed). I didn't
   capture the test name (only the summary line was kept). It didn't come back in
   3 more full runs, and each half passes alone. It's probably order- or
   timing-dependent, and probably involves the new file, which imports the
   module-scoped Postgres fixtures from `test_access_control_postgres.py`. Worth
   watching.
3. **`func start` needs the API's venv activated.** With the venv's `Scripts` only
   on `PATH`, the worker used another Python and failed with
   `ModuleNotFoundError: httpx`, then registered no functions. The steps below
   activate it.
4. **Vite listens on `localhost` only.** `http://127.0.0.1:5173` doesn't answer;
   use `http://localhost:5173`.
5. **Client ids differ between a fresh database and yours.** A fresh migration
   seeds ARCHITECT as client 1 and QA as client 2, while STATUS.md says ARCHITECT
   is `client_id=2` in the dev database. The switcher shows names, so pick
   ARCHITECT by name. I didn't query your database, so the 3,136 count is
   unverified.

## Files in the patch

New:

- `dashboard/web/src/operatorHeader.ts`
- `dashboard/web/src/vite-env.d.ts`
- `dashboard/web/tests/operatorHeader.test.ts`
- `gate-report-local-operator-dashboard.md` (this file)

Modified:

- `dashboard/web/src/api.ts`
- `dashboard/web/src/App.tsx`
- `dashboard/web/src/main.tsx`
- `dashboard/web/src/components/ExportButton.tsx`
- `dashboard/functions_api/local.settings.json.example`
- `dashboard/functions_api/README.md`

Not included: `test_access_control_extra.py`, `.gitignore`, the JSON backup, and
the other reports.

# National run, client_id=2 (STATUS.md v1 item 2)

Run from the repo root in PowerShell, on `main`, in one session. There are three phases (A filter, B score, C publish) with a STOP between each. Every phase runs inside `adisc-dev-pipeline-job`: a temporary `args` edit to `infra/modules/containerAppsJob.bicep`, deployed, started with a plain `job start`, then reverted at the end.

Why not `az containerapp job start --args/--command`: the CLI does offer them, but in this az CLI version each override either dropped the job's secrets, volumes or resources, or silently ignored the command (STATUS.md, connection-drop validation). Losing the archive-cache volume or `DATABASE_URL` would break the run.

## Setup

**S1.** Set variables and helper functions. Paste the whole block.
```powershell
$rg = 'rg-appadino-discovery-dev'; $jobName = 'adisc-dev-pipeline-job'; $py = 'pipeline\.venv\Scripts\python.exe'
$bicep = (Resolve-Path infra/modules/containerAppsJob.bicep).Path
$tag = 'latest'   # Phase A; B0 switches this to an immutable tag built from main
$env:DATABASE_URL = az keyvault secret show --vault-name adisc-dev-kv --name db-connection-string --query value -o tsv
$env:PG_ADMIN_PW  = az keyvault secret show --vault-name adisc-dev-kv --name postgres-admin-password --query value -o tsv
$ws = az monitor log-analytics workspace show -g $rg -n adisc-dev-law --query customerId -o tsv
function Set-JobArgs([string[]]$a, [int]$retry = 1) {
  git checkout -- infra/modules/containerAppsJob.bicep
  $t = [IO.File]::ReadAllText($bicep)
  $block = "image: image`n          args: [`n" + (($a | % { "            '$_'" }) -join "`n") + "`n          ]`n"
  $t = $t -replace "image: image\r?\n", $block -replace 'replicaRetryLimit: 1', "replicaRetryLimit: $retry"
  [IO.File]::WriteAllText($bicep, $t); git diff --stat infra
}
function Deploy-Job { az deployment group create -g $rg -f infra/main.bicep -p infra/main.bicepparam -p pipelineImageTag=$tag --query properties.provisioningState -o tsv }
function Start-Run { $script:exec = az containerapp job start -n $jobName -g $rg --query name -o tsv; $script:exec }
function Wait-Run { do { Start-Sleep 300; $s = az containerapp job execution show -n $jobName -g $rg --job-execution-name $exec --query properties.status -o tsv; "$(Get-Date -f HH:mm) $s" } while ($s -in 'Running','Processing'); $s }
function Get-RunLogs([string]$has) {
  $q = "ContainerAppConsoleLogs_CL | where ContainerGroupName_s startswith '$exec' | where $has | project TimeGenerated, Log_s | order by TimeGenerated asc"
  (az monitor log-analytics query -w $ws --analytics-query $q -o json | ConvertFrom-Json) | % { "$($_.TimeGenerated) $($_.Log_s)" }
}
git status --short; $tag
```
**STOP IF** you're not on `main`, `git status` shows a modified `containerAppsJob.bicep`, or either secret came back empty.

**S2.** Phase A runs on the `:latest` image already pushed, which contains the Deflate64 fix. No build is needed here. Phase B needs a newer image, built in step B0.

## Phase A: Stage 1-2, `filter 2` (no `--eins`, no `--limit-eins`)

Expect about 119k survivors (119,218 the last time this was recorded) and about 243k filings. At the measured 26 filings/sec, that's roughly 2.6 h of the 4 h timeout. EINs whose signal is already current (such as the 5,000 from the validation run) are skipped.

**A1.** Point the job at `filter 2`.
```powershell
Set-JobArgs @('filter', '2')
```
**STOP IF** the diff stat isn't one file with a few lines added.

**A2.** Preview the deploy. Besides the args, the job also picks up the 2 vCPU / 4 GiB resources from `07d41e2` here.
```powershell
az deployment group what-if -g $rg -f infra/main.bicep -p infra/main.bicepparam -p pipelineImageTag=$tag
```
**STOP IF** anything other than `adisc-dev-pipeline-job` shows as Modify, Create or Delete.

**A3.** Deploy and start.
```powershell
Deploy-Job; Start-Run
```
**STOP IF** the deploy doesn't print `Succeeded` or the execution name is empty.

**A4.** Wait. This checks every 5 minutes.
```powershell
Wait-Run
```

**A5.** Read the results. Logs reach Log Analytics a few minutes after the run ends.
```powershell
Get-RunLogs "Log_s has 'Stage 1:' or Log_s has 'Suppression (pre-Stage2)' or Log_s has 'Stage 2 progress' or Log_s has 'Stage 2:' or Log_s has 'filter complete' or Log_s has 'RUN_FAILED' or Log_s has 'compression method'" | Select -Last 12
```
Read `survivors`, `filings_parsed`, `filings_failed`, `signals_computed`, `coverage_pct` and `peak_rss_mib` from the `filter complete:` line.

**If A4 ended `Failed` and there's no `filter complete:` line:**
- If the last log lines are progress lines with no `RUN_FAILED` traceback, it hit the 4 h timeout. Run `Start-Run; Wait-Run`, then repeat A5. It resumes per EIN and skips everything already committed. Repeat until a `filter complete:` line appears.
- If a `RUN_FAILED` traceback appears, stop and paste it.

**STOP IF:**
- `RUN_FAILED` or `compression method` appears;
- `filings_failed` is more than a few percent of `filings_parsed + filings_failed`;
- `peak_rss_mib` is above 3,200 (of 4,096);
- or `survivors` is far from about 119k.

Paste the `filter complete:` line either way.

## Phase B: Stage 3, `score 2`. Costs money: needs your approval

Scoring submits Haiku requests in chunks of at most 20,000 requests or about 100 MB (about 6 batches for 119k orgs). It records each batch ID in the run's `runs.counts` as soon as the batch is submitted. A run that dies can be resumed with `score 2 --resume-run <id>`, which collects the recorded batches instead of paying for them again. While an unfinished run with recorded batches exists, a plain `score 2` refuses to start and exits with code 2.

**B0.** Rebuild the image from `main` so it contains the chunked scoring code (`7aae80d`). Tag it with an immutable `national-<commit>` tag for Phases B and C, and also as `:latest` for the monthly scheduled run.
```powershell
git pull --ff-only; git log --oneline -1
$tag = "national-$(git rev-parse --short HEAD)"
az acr build --registry adiscdevacr -t "discovery-pipeline:$tag" -t discovery-pipeline:latest pipeline
```
**STOP IF** the build fails or `git log` doesn't show a commit at or after `7aae80d`.

**B1.** Show the survivor count and the cost estimate. Read-only: one DB read and no API calls.
```powershell
@'
import os, psycopg
from discovery.stages.filter import get_active_icp_config, select_survivor_eins
with psycopg.connect(os.environ["DATABASE_URL"]) as c:
    survivors = select_survivor_eins(c, 2)
    scoreable = c.execute("""SELECT count(*) FROM organizations o WHERE o.ein = ANY(%s)
        AND EXISTS (SELECT 1 FROM filings f WHERE f.ein = o.ein AND f.extracted_at IS NOT NULL)
        AND EXISTS (SELECT 1 FROM signals s WHERE s.ein = o.ein)""", (survivors,)).fetchone()[0]
    cut = get_active_icp_config(c, 2).get("haiku_cut_n", 3000)
    reviewed = c.execute("SELECT status, count(*) FROM prospects WHERE client_id = 2 GROUP BY status ORDER BY status").fetchall()
n = min(cut, scoreable)
# Batch rates (50% off): Haiku 4.5 $0.50/$2.50 per MTok, Sonnet 5 $1.00/$5.00.
# Tokens per org from the QA gate's measured request sizes: Haiku ~1,100 in, 50-100 out (max 512);
# Sonnet ~2,600 in, 2,500-5,000 out incl. thinking (max 8,000).
h_lo, h_hi, h_cap = (scoreable * (1100 * 0.50 + o * 2.50) / 1e6 for o in (50, 100, 512))
s_lo, s_hi, s_cap = (n * (2600 * 1.00 + o * 5.00) / 1e6 for o in (2500, 5000, 8000))
print(f"survivors {len(survivors):,}  scoreable (Haiku calls) {scoreable:,}  Haiku-to-Sonnet cut {cut:,}  Sonnet calls <= {n:,}")
print(f"Haiku  ${h_lo:,.0f}-${h_hi:,.0f} (ceiling ${h_cap:,.0f})")
print(f"Sonnet ${s_lo:,.0f}-${s_hi:,.0f} (ceiling ${s_cap:,.0f})")
print(f"TOTAL  ${h_lo + s_lo:,.0f}-${h_hi + s_hi:,.0f} (ceiling ${h_cap + s_cap:,.0f})")
print("current prospects by status:", reviewed)
'@ | & $py -
```
**STOP. Paste this output and approve the dollar range before going on.** At about 119k scoreable orgs and a cut of 3,000, expect about $125–180, with a ceiling of about $345. The brief's $30–60 assumed 50k orgs. These are character-count estimates, not `count_tokens` results. A resume doesn't add to this. A **new** run (after abandoning one, B7) re-scores every org and pays again.

**B2.** Point the job at `score 2`, with automatic retries off. The guard would stop a retry from paying twice, but an automatic retry would still bury the real failure.
```powershell
Set-JobArgs @('score', '2') -retry 0
```
**STOP IF** the diff doesn't show both the args and `replicaRetryLimit: 0`.

**B3.** Deploy and start. This is the step that spends money.
```powershell
Deploy-Job; Start-Run
```
**STOP IF** the deploy doesn't print `Succeeded` or the execution name is empty.

**B4.** Wait. A batch usually finishes within an hour but may take up to 24 h, and this job is killed at 4 h.
```powershell
Wait-Run
```

**B5.** Read the results.
```powershell
Get-RunLogs "Log_s has 'Stage 3:' or Log_s has 'submitted batch' or Log_s has 'recorded ' or Log_s has 'RESUME' or Log_s has 'score complete' or Log_s has 'score not started' or Log_s has 'RUN_FAILED' or Log_s has 'ZERO_OUTPUT_RUN'" | Select -Last 20
```
If there's a `score complete:` line, **STOP IF** `ZERO_OUTPUT_RUN` appears, `haiku_failed` or `sonnet_failed` is more than a few percent, or `hard_rule_violations` is non-zero. Otherwise go to Phase C.

**If B4 ended `Failed` and there's no `score complete:` line, don't start a plain `score 2` again.** Its batches are paid for and may still be running. Use B6 to resume the run:
- **No `RUN_FAILED` traceback, last lines are `submitted`/`recorded`/`collecting` lines:** it hit the 4 h timeout while waiting on a batch. Resume.
- **`RUN_FAILED` with a transient cause** (a dropped DB connection, an Anthropic 5xx or overload error): resume.
- **`RUN_FAILED` with anything else, or `score not started:`:** stop and paste it. `score not started:` means the guard found an unfinished run, and the line names it and gives the resume command.

**B6.** Resume an unfinished run.

1. Find the run id. A new run's ID doesn't appear in any log line; only a resume logs it (`RESUME: continuing scoring run <id>`). Use the newest `scoring` row with status `running` (killed at the timeout) or `failed`, and recorded `batches`.
   ```powershell
   @'
   import os, psycopg
   with psycopg.connect(os.environ["DATABASE_URL"]) as c:
       for r in c.execute("""SELECT id, status, started_at, finished_at, counts->'batches', counts->'resumed_times', left(error, 200)
           FROM runs WHERE client_id = 2 AND stage = 'scoring' ORDER BY id DESC LIMIT 3"""): print(r)
   '@ | & $py -
   ```
2. Point the job at the resume, start it, wait, and read the logs.
   ```powershell
   $runId = <id>
   Set-JobArgs @('score', '2', '--resume-run', "$runId") -retry 0
   Deploy-Job; Start-Run; Wait-Run
   ```
   Then repeat B5.

**A resume can need repeating.** It waits for every recorded batch to end before submitting the remaining chunks. A batch can take up to 24 h and each execution is killed at 4 h, so a resume can itself time out. Repeat step 2 of B6 with the same `$runId` until a `score complete:` line appears. Each resume adds 1 to `resumed_times` and pays only for orgs that no recorded batch covers. Batch results are kept 29 days, so finish within that window.

**B7.** Abandon a stuck run, only if you deliberately want to discard it. For 29 days, the guard refuses a plain `score 2` while this run is `running` or `failed` with recorded batches. `runs.status` has no CHECK constraint, so a new value is allowed:
```sql
UPDATE runs SET status = 'abandoned', finished_at = now(), error = coalesce(error || E'\n', '') || 'abandoned by operator' WHERE id = <id> AND client_id = 2 AND stage = 'scoring' AND status IN ('running', 'failed');
```
The next plain `score 2` pays again for every org. Cancel any of that run's batches that are still in progress in the Anthropic Console first, or they keep billing. `--resume-run <id>` still accepts an abandoned run if you change your mind.

## Phase C: Stages 4-6 + QA, `publish 2`

What re-publishing does to existing prospects (from `upsert_prospect` in `publish.py`):
- **Kept:** `status`, `notes` and `updated_by` are never in the UPDATE, so the 154 reviewed rows keep their review decisions and comments.
- **Overwritten:** `gap_rank`, `assigned_trigger`, `trigger_angle`, `trigger_evidence`, `suppression_flag` and `updated_at`, using Phase B's new scores. The dashboard's order and trigger angles change for reviewed rows too.
- **Added:** every newly Sonnet-scored, non-disqualified, non-suppressed org becomes a `'new'` prospect, up to about 3,000. The customer sees all of these.
- **Never deleted or demoted:** a prospect that drops out (newly disqualified, suppressed, or below the cut) keeps its old row and values.

**C1.** Back up client 2's prospects to a local JSON file. Status and notes are safe, but this keeps the old `gap_rank`/trigger values for comparison or restore. **The file holds customer data: don't commit it.**
```powershell
@'
import json, os, psycopg
with psycopg.connect(os.environ["DATABASE_URL"]) as c:
    cur = c.execute("SELECT * FROM prospects WHERE client_id = 2 ORDER BY id")
    cols = [d.name for d in cur.description]; rows = [dict(zip(cols, r)) for r in cur]
json.dump(rows, open("prospects-backup-client2.json", "w"), default=str, indent=1)
print(len(rows), "prospects backed up")
'@ | & $py -
```
**STOP IF** the count isn't the number you expect (154 before the first national publish).

**C2.** Point the job at `publish 2`.
```powershell
Set-JobArgs @('publish', '2')
```

**C3.** Deploy, start and wait.
```powershell
Deploy-Job; Start-Run; Wait-Run
```
**STOP IF** the deploy isn't `Succeeded` or the final status isn't `Succeeded`.

**C4.** Read the results.
```powershell
Get-RunLogs "Log_s has 'publish complete' or Log_s has 'RUN_FAILED' or Log_s has 'QA_MISMATCH_RATE_EXCEEDED' or Log_s has 'ZERO_OUTPUT_RUN'"
```
**STOP IF** any marker other than `publish complete:` appears. The CSV the job writes stays inside the container; export from the dashboard instead.

**C5.** Final check: counts for client 2, the latest run records, and whether every backed-up prospect kept its status and notes.
```powershell
@'
import json, os, psycopg
with psycopg.connect(os.environ["DATABASE_URL"]) as c:
    print("prospects by status:", c.execute("SELECT status, count(*) FROM prospects WHERE client_id = 2 GROUP BY status ORDER BY status").fetchall())
    print("scores by stage:", c.execute("SELECT stage, count(*) FROM scores WHERE client_id = 2 GROUP BY stage ORDER BY stage").fetchall())
    print("runs:", c.execute("SELECT count(*) FROM runs WHERE client_id = 2").fetchone()[0])
    for r in c.execute("SELECT id, stage, status, started_at, finished_at, counts FROM runs WHERE client_id = 2 ORDER BY id DESC LIMIT 3"): print(r)
    now = {r[0]: r[1:] for r in c.execute("SELECT ein, status, notes FROM prospects WHERE client_id = 2")}
before = json.load(open("prospects-backup-client2.json"))
lost = [b["ein"] for b in before if b["ein"] not in now]
changed = [b["ein"] for b in before if b["ein"] in now and (now[b["ein"]][0], now[b["ein"]][1]) != (b["status"], b["notes"])]
print(f"backed-up prospects missing: {len(lost)}, status/notes changed: {len(changed)}", lost[:10], changed[:10])
'@ | & $py -
```
**STOP IF** any backed-up prospect is missing or had its status or notes changed (a reviewer editing in the dashboard during the run would also show here), or the newest three runs aren't `publish`, `scoring` and `filter_and_signals` with status `success`.

## Close

**Z1.** Revert the bicep edit and redeploy, so the job goes back to its monthly `ingest` on `:latest`, with no args and a retry limit of 1. Do this promptly: while an edit is deployed, the 1st-of-month 06:00 UTC schedule would run that command instead of `ingest`.
```powershell
git checkout -- infra/modules/containerAppsJob.bicep; git status --short infra; az deployment group create -g $rg -f infra/main.bicep -p infra/main.bicepparam --query properties.provisioningState -o tsv
```

**Z2.** Confirm the job has no leftover overrides.
```powershell
az containerapp job show -n $jobName -g $rg --query "{image:properties.template.containers[0].image, args:properties.template.containers[0].args, command:properties.template.containers[0].command, env:properties.template.containers[0].env[].name, resources:properties.template.containers[0].resources, retry:properties.configuration.replicaRetryLimit}" -o json
```
**STOP IF** `args` or `command` isn't `null`, the image isn't `discovery-pipeline:latest`, `env` lists anything besides `DATABASE_URL`, `ANTHROPIC_API_KEY` and `ARCHIVE_CACHE_DIR`, resources aren't `2.0` / `4Gi`, or `retry` isn't `1`.

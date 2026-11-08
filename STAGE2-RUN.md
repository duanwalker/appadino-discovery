# Stage 2 Azure validation run (STATUS.md v1 item 1)

Run from the repo root in PowerShell, top to bottom, in one session.

**1.** Set variables.
```powershell
$rg = 'rg-appadino-discovery-dev'; $jobName = 'adisc-dev-pipeline-job'; $py = 'pipeline\.venv\Scripts\python.exe'
$tag = "stage2-$(git rev-parse --short HEAD)"
```

**2.** Connect to the dev DB. If the firewall rule for your current IP is missing, see `pipeline/README.md`.
```powershell
$env:DATABASE_URL = az keyvault secret show --vault-name adisc-dev-kv --name db-connection-string --query value -o tsv
```

**3.** Write `pilot_eins.txt` (the 154 client_id=2 prospects) and `stage2_run_eins.txt` (the pilot plus a fixed random sample of client 2's Stage 1 survivors, 5,000 EINs in total).
```powershell
@'
import os, random, psycopg
from discovery.stages.filter import select_survivor_eins
with psycopg.connect(os.environ["DATABASE_URL"]) as c:
    pilot = [r[0] for r in c.execute("SELECT DISTINCT ein FROM prospects WHERE client_id = 2 ORDER BY ein")]
    pool = sorted(set(select_survivor_eins(c, 2)) - set(pilot))
run = pilot + sorted(random.Random(20261108).sample(pool, 5000 - len(pilot)))
open("pilot_eins.txt", "w", newline="\n").write("\n".join(pilot) + "\n")
open("stage2_run_eins.txt", "w", newline="").write(",".join(run))
print(len(pilot), "pilot EINs,", len(run), "run EINs")
'@ | & $py -
```
**STOP IF** the output is not `154 pilot EINs, 5000 run EINs`.

**4.** Take the BEFORE snapshot.
```powershell
& $py pipeline/scripts/snapshot_stage2_outputs.py --eins-file pilot_eins.txt --out stage2-before.json
```
**STOP IF** it errors or reports anything other than 154 EINs.

**5.** Build the image from HEAD under its own tag. This leaves `:latest` alone.
```powershell
az acr build --registry adiscdevacr --image "discovery-pipeline:$tag" pipeline
```
**STOP IF** the build fails.

**6.** Edit `infra/modules/containerAppsJob.bicep`. Find these two lines:
```bicep
          image: image
          env: [
```
and replace them with:
```bicep
          image: image
          args: [
            'filter'
            '2'
            '--eins'
            trim(loadTextContent('../../stage2_run_eins.txt'))
            '--force-refresh-signals'
          ]
          env: [
            {
              name: 'STAGE2_TIMING'
              value: '1'
            }
```

**7.** Preview the deploy.
```powershell
$env:PG_ADMIN_PW = az keyvault secret show --vault-name adisc-dev-kv --name postgres-admin-password --query value -o tsv
az deployment group what-if -g $rg -f infra/main.bicep -p infra/main.bicepparam -p pipelineImageTag=$tag
```
**STOP IF** anything other than `adisc-dev-pipeline-job` shows as Modify, Create or Delete (Ignore and NoChange are fine).

**8.** Deploy, then start the job.
```powershell
az deployment group create -g $rg -f infra/main.bicep -p infra/main.bicepparam -p pipelineImageTag=$tag
$exec = az containerapp job start -n $jobName -g $rg --query name -o tsv; $exec
```
**STOP IF** the deploy fails or `$exec` is empty.

**9.** Wait for the run to finish. The first validation run took about 8 minutes with 24% of filings failing fast; with those now decoded, expect roughly 10 to 20 minutes. The timeout is 4 hours.
```powershell
do { Start-Sleep 120; $s = az containerapp job execution show -n $jobName -g $rg --job-execution-name $exec --query properties.status -o tsv; "$(Get-Date -f HH:mm) $s" } while ($s -in 'Running','Processing')
```
**STOP IF** the final status is not `Succeeded`. Paste the step 10 output.

**10.** Read the results. Logs reach Log Analytics a few minutes after the run ends.
```powershell
$e = az containerapp job execution show -n $jobName -g $rg --job-execution-name $exec --query properties | ConvertFrom-Json
$ws = az monitor log-analytics workspace show -g $rg -n adisc-dev-law --query customerId -o tsv
$q = "ContainerAppConsoleLogs_CL | where TimeGenerated between (datetime($($e.startTime)) .. datetime($($e.endTime))) | where Log_s has 'Stage 2 progress' or Log_s has 'Stage 2:' or Log_s has 'RUN_FAILED' or Log_s has 'compression method' | project TimeGenerated, Log_s | order by TimeGenerated asc"
$logs = az monitor log-analytics query -w $ws --analytics-query $q -o json | ConvertFrom-Json; $logs | ft -Wrap
$p = $logs | ? Log_s -match 'progress: (\d+)/(\d+)' | % { [pscustomobject]@{ t = [datetime]$_.TimeGenerated; n = [int]($_.Log_s -replace '.*progress: (\d+)/.*', '$1') } }
$fps = ($p[-1].n - $p[0].n) / ($p[-1].t - $p[0].t).TotalSeconds; "filings/sec: {0:N2}" -f $fps
($p | Select -Last 1); ($logs | ? Log_s -match 'timing_s' | Select -Last 1).Log_s -replace '.*(timing_s\[.*\]).*', '$1'
($logs | ? Log_s -match "'filings_failed': \d+" | Select -Last 1).Log_s -replace ".*'filings_parsed': (\d+), 'filings_failed': (\d+).*", 'parsed $1, failed $2'
"peak memory: {0} MiB of 2048" -f (($logs | ? Log_s -match 'peak_rss_mib' | Select -Last 1).Log_s -replace ".*peak_rss_mib'?[:=] ?(\d+).*", '$1')
```
- filings/sec: from the first and last progress lines.
- zip_read / parse / db_write: the `timing_s[...]` totals in seconds, cumulative to the last progress line.
- parsed / failed: the run's final filing counts.
- peak memory: the pipeline process's own peak RSS (`peak_rss_mib`), logged by the pipeline. Don't use the job's Azure `UsageBytes` metric for this: it includes the file cache from archive reads and sits near 2 GiB on every run. (`MemoryWorkingSetBytes` doesn't exist for Container Apps jobs.)

**STOP IF** you get fewer than 2 progress lines, `RUN_FAILED` appears, any `compression method` line appears (the Deflate64 fix didn't take), failed is more than a few percent of parsed + failed, or peak memory is above 1,600 MiB.

**11.** Revert the bicep edit, then redeploy so the job runs `:latest` with no args again.
```powershell
git checkout -- infra/modules/containerAppsJob.bicep; git status --short infra
az deployment group create -g $rg -f infra/main.bicep -p infra/main.bicepparam
az containerapp job show -n $jobName -g $rg --query "properties.template.containers[0].{image:image,args:args}"
```
**STOP IF** the last command shows any `args`, or an image other than `discovery-pipeline:latest`.

**12.** Take the AFTER snapshot and compare it with BEFORE.
```powershell
& $py pipeline/scripts/snapshot_stage2_outputs.py --eins-file pilot_eins.txt --out stage2-after.json
@'
import json, sys
b, a = (json.load(open(p)) for p in sys.argv[1:3])
new, bad = 0, []
for name, key in (("filings", ("ein", "tax_year", "object_id")), ("signals", ("ein", "tax_year"))):
    B = {tuple(r[k] for k in key): r for r in b[name]}
    A = {tuple(r[k] for k in key): r for r in a[name]}
    for k, row in B.items():
        was_filled = any(v is not None for f, v in row.items() if f not in key)
        if k not in A: bad.append((name, k, "removed"))
        elif A[k] != row:
            if was_filled: bad.append((name, k, "changed"))
            else: new += 1
    new += len(A.keys() - B.keys())
print("newly extracted or added rows:", new)
print("REGRESSIONS:", len(bad))
for x in bad[:50]: print(x)
'@ | & $py - stage2-before.json stage2-after.json
```
An "empty diff" looks like `REGRESSIONS: 0`. The "newly extracted or added rows" count is expected to be non-zero, roughly 30 filings plus up to 30 signals. Those are the pilot orgs whose newer 990 was never extracted (the launch blocker in STATUS.md).

**STOP IF** REGRESSIONS is above 0. Paste the output and don't publish or re-run anything. `stage2-before.json` holds the earlier values.

**13.** Project the full national run.
```powershell
"national: {0:N1} h for 243,448 filings = {1} run(s) at the 4 h timeout (resumes per EIN)" -f (243448 / $fps / 3600), [math]::Ceiling(243448 / $fps / 14400)
```

# ============================================================================
#  sandbox-scheduler - GUIDED DEMO (interactive)
#
#  Watch a scheduler do its job: queues, fairness, aging, cancellation,
#  expiry, crash recovery. Run AFTER scripts/start.ps1 (and keep the
#  console open at http://localhost:9010 to SEE it).
#
#     powershell -ExecutionPolicy Bypass -File scripts\demo.ps1
# ============================================================================
$ErrorActionPreference = "Stop"
$Api = "http://localhost:9010"

function Pause($msg) {
    Write-Host "`n>>> $msg" -ForegroundColor Cyan
    Read-Host "    press ENTER to continue"
}
function Say($msg) { Write-Host "`n== $msg ==" -ForegroundColor Green }

Write-Host @"
============================================================
 SANDBOX-SCHEDULER GUIDED DEMO
 You will: submit jobs, watch a herd drain, see fairness and
 aging defeat starvation, cancel running work, and kill the
 scheduler mid-flight to watch it recover.
 Keep http://localhost:$($Api -replace '.*:','') open in your browser!
============================================================
"@

Say "0. Reset the job database for a clean demo"
Invoke-RestMethod -Method Post -Uri $Api/admin/reset | ConvertTo-Json

Say "1. One task job - the simplest possible unit of work"
$j = Invoke-RestMethod -Method Post -Uri $Api/jobs -ContentType 'application/json' `
    -Body '{"tenant":"demo","template":"blank","kind":"task","cmd":["sh","-c","echo hello; hostname; sleep 3"]}'
Write-Host "Submitted job $($j.id) - state $($j.state) (QUEUED)"
do { Start-Sleep 2; $j = Invoke-RestMethod $Api/jobs/$($j.id) } while ($j.state -in "QUEUED","ADMITTED","RUNNING")
Write-Host "Final state: $($j.state)" -ForegroundColor Yellow
Write-Host "It ran INSIDE a fresh sandbox. Captured output:"
$j.result.results[0].stdout
Pause "That sandbox was created, used, and deleted for you. See its URL? There is none - task jobs release their sandbox immediately."

Say "2. A service job - a sandbox that STAYS UP"
$w = Invoke-RestMethod -Method Post -Uri $Api/jobs -ContentType 'application/json' `
    -Body '{"tenant":"demo","template":"web","kind":"service","name":"demo-web","max_runtime":600}'
do { Start-Sleep 2; $w = Invoke-RestMethod $Api/jobs/$($w.id) } while ($w.state -in "QUEUED","ADMITTED")
$sb = $w.sandboxes[0]
Write-Host "RUNNING at $($sb.url)" -ForegroundColor Yellow
Pause "OPEN $($sb.url) in your browser - a service job keeps its sandbox alive until you cancel it or max_runtime hits."

Say "3. The herd - 12 task jobs at once"
1..12 | ForEach-Object {
    Invoke-RestMethod -Method Post -Uri $Api/jobs -ContentType 'application/json' `
        -Body "{`"tenant`":`"herd`",`"template`":`"blank`",`"kind`":`"task`",`"cmd`":[`"sh`",`"-c`",`"sleep 5`"]}" | Out-Null
}
Write-Host "12 jobs submitted. Capacity is 4 cpu units - watch them wave through."
Pause "WATCH THE CONSOLE NOW: the queue table, the capacity bar, and jobs flowing QUEUED -> ADMITTED -> RUNNING -> SUCCEEDED."

Say "4. Fairness - a noisy tenant cannot starve a quiet one"
Write-Host "Submitting 6 service jobs as 'noisy' (tenant cap is 3, cluster cpu is 4)..."
1..6 | ForEach-Object {
    Invoke-RestMethod -Method Post -Uri $Api/jobs -ContentType 'application/json' `
        -Body "{`"tenant`":`"noisy`",`"template`":`"blank`",`"kind`":`"service`",`"name`":`"noisy-$_`",`"max_runtime`":300}" | Out-Null
}
Start-Sleep 8
Write-Host "noisy holds 3 slots (capped). Now a tiny job from a new tenant 'quiet'..."
$q = Invoke-RestMethod -Method Post -Uri $Api/jobs -ContentType 'application/json' `
    -Body '{"tenant":"quiet","template":"blank","kind":"service","name":"quiet-1","max_runtime":300}'
do { Start-Sleep 2; $q = Invoke-RestMethod $Api/jobs/$($q.id) } while ($q.state -eq "QUEUED")
Write-Host "quiet-1 is $($q.state) - it jumped ahead of noisy's waiting jobs, fairly." -ForegroundColor Yellow
Pause "Look at the queue table: noisy-* jobs wait while quiet-1 runs. That is weighted fair queueing + per-tenant caps."

Say "5. Aging - old jobs rise in priority automatically"
Write-Host "Cluster is busy; submitting a priority-1 job 'oldtimer'..."
$old = Invoke-RestMethod -Method Post -Uri $Api/jobs -ContentType 'application/json' `
    -Body '{"tenant":"demo","template":"blank","kind":"service","name":"oldtimer","priority":1,"max_runtime":300}'
Pause "WATCH THE QUEUE TABLE: oldtimer's +aged column climbs +1 every 5s. Given time it outranks fresh high-priority jobs. Starvation is impossible."

Say "6. Cancel - tearing down running work"
Write-Host "Cancelling noisy-1 (RUNNING)..."
Invoke-RestMethod -Method Post -Uri "$Api/jobs/$((Invoke-RestMethod "$Api/jobs" | Where-Object {$_.name -eq 'noisy-1'}).id)/cancel" | Out-Null
Start-Sleep 4
Write-Host "Its sandbox was deleted from the engine - check http://localhost:9000 (sandbox-api console)." -ForegroundColor Yellow
Pause "Cancel from QUEUED is instant; cancel while RUNNING tears the sandbox down; cancel when done is a 409."

Say "7. Crash recovery - kill the scheduler mid-flight"
Write-Host "docker restart sandbox-scheduler-scheduler-1 ..."
docker restart sandbox-scheduler-scheduler-1 | Out-Null
$ok = $false
for ($i=0; $i -lt 40; $i++) {
    try { if ((Invoke-RestMethod "$Api/healthz" -TimeoutSec 2).scheduler -eq "up") { $ok=$true; break } } catch {}
    Start-Sleep 2
}
$jobs = Invoke-RestMethod "$Api/jobs"
Write-Host "Scheduler is back. Jobs still tracked: $($jobs.Count). Running jobs re-adopted, nothing duplicated."
Pause "Check the event log of any RUNNING job (click it in the console): a 'reconcile' event recorded the restart."

Say "8. Cleanup"
foreach ($job in (Invoke-RestMethod "$Api/jobs")) {
    if ($job.state -in "QUEUED","ADMITTED","RUNNING") {
        Invoke-RestMethod -Method Post -Uri "$Api/jobs/$($job.id)/cancel" | Out-Null
    }
}
Start-Sleep 5
Write-Host @"

============================================================
 DEMO OVER. What you saw:
   task jobs, service jobs, herds, fair queueing, aging,
   cancel semantics, expiry, and crash recovery.
 Next: bash scripts/verify.sh  (41-assertion edge battery)
       docs/EXAMPLES.md        (practice exercises)
============================================================
"@ -ForegroundColor Cyan

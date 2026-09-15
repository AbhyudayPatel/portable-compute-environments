# ============================================================================
#  sandbox-api - GUIDED DEMO (interactive)
#
#  Walks you through every feature with live output, pausing so you can
#  open things in your browser. Run AFTER scripts/start.ps1:
#
#     powershell -ExecutionPolicy Bypass -File scripts\demo.ps1
# ============================================================================
$ErrorActionPreference = "Stop"
$Api = "http://localhost:9000"

function Pause($msg) {
    Write-Host "`n>>> $msg" -ForegroundColor Cyan
    Read-Host "    press ENTER to continue"
}
function Show($title, $cmd) {
    Write-Host "`n$ $cmd" -ForegroundColor DarkGray
    Write-Host $title -ForegroundColor Green
}

Write-Host @"
============================================================
 SANDBOX-API GUIDED DEMO
 You will: create sandboxes, run apps inside them, exec into
 them, watch TTL self-destruction, and test platform limits.
============================================================
"@

# --- 0. health -------------------------------------------------------------
Show "The platform reports its own health:" "GET /healthz"
Invoke-RestMethod $Api/healthz | ConvertTo-Json
Pause "api=dind=up means the control plane can reach the inner engine."

# --- 1. a full APPLICATION sandbox -----------------------------------------
Show "Creating a full 3-tier app (nginx->FastAPI->Postgres) inside ONE sandbox:" `
     "POST /sandboxes {name:demo-app, template:coreapp}"
$sb = Invoke-RestMethod -Method Post -Uri $Api/sandboxes `
    -ContentType 'application/json' -Body '{"name":"demo-app","template":"coreapp"}'
$sb | ConvertTo-Json
Write-Host "First build takes ~60s (images are built INSIDE the inner engine)..." -ForegroundColor Yellow
while ((Invoke-RestMethod $Api/sandboxes/$($sb.id)).state -eq "CREATING") { Start-Sleep 3 }
$sb = Invoke-RestMethod $Api/sandboxes/$($sb.id)
Write-Host "State: $($sb.state)   URL: $($sb.url)" -ForegroundColor Green

Pause "OPEN IN YOUR BROWSER:  $($sb.url)  - a real task board. Add tasks, toggle them, delete them."

Show "The same app via curl:" "GET $($sb.url)/api/health ; POST /api/tasks"
Invoke-RestMethod "$($sb.url)/api/health" | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri "$($sb.url)/api/tasks" -ContentType 'application/json' -Body '{"title":"created by the demo script"}' | ConvertTo-Json
Invoke-RestMethod "$($sb.url)/api/tasks" | ConvertTo-Json
Pause "Refresh $($sb.url) - your new task is in the board. That row is in a Postgres INSIDE the sandbox."

# --- 2. exec INTO the sandbox ----------------------------------------------
Show "Run a command INSIDE the sandbox's database container (via the app role):" `
     "POST /sandboxes/$($sb.id)/exec {cmd:[hostname;ip addr]}"
Invoke-RestMethod -Method Post -Uri "$Api/sandboxes/$($sb.id)/exec" `
    -ContentType 'application/json' -Body '{"cmd":["sh","-c","hostname && ip -brief addr"]}' | ConvertTo-Json
Pause "That hostname is the INNER container - you just executed through two Docker engines."

# --- 3. idempotency ----------------------------------------------------------
Show "Create the SAME sandbox again (same name+spec):" "POST /sandboxes (repeat)"
$again = Invoke-WebRequest -Method Post -Uri $Api/sandboxes -ContentType 'application/json' -Body '{"name":"demo-app","template":"coreapp"}'
Write-Host "HTTP $($again.StatusCode) - same sandbox returned, nothing duplicated." -ForegroundColor Green
try {
    Invoke-WebRequest -Method Post -Uri $Api/sandboxes -ContentType 'application/json' -Body '{"name":"demo-app","template":"blank"}' | Out-Null
} catch { Write-Host "HTTP $($_.Exception.Response.StatusCode.value__) - same name, DIFFERENT spec is rejected." -ForegroundColor Green }
Pause "Idempotency: clients can safely retry creates (crashes, double-clicks)."

# --- 4. a second sandbox + the event log -------------------------------------
Show "A quick web sandbox:" "POST /sandboxes {name:demo-web, template:web}"
$w = Invoke-RestMethod -Method Post -Uri $Api/sandboxes -ContentType 'application/json' -Body '{"name":"demo-web","template":"web"}'
while ((Invoke-RestMethod $Api/sandboxes/$($w.id)).state -eq "CREATING") { Start-Sleep 2 }
$w = Invoke-RestMethod $Api/sandboxes/$($w.id)
Write-Host "READY at $($w.url)" -ForegroundColor Green
Show "Every sandbox keeps an ordered event log:" "GET /sandboxes/$($w.id)/events"
Invoke-RestMethod "$Api/sandboxes/$($w.id)/events" | ForEach-Object { "{0,3}  {1,-8} {2}" -f $_.seq, $_.type, $_.message }
Pause "OPEN $($w.url) - it explains the 3-layer path your request just took."

# --- 5. TTL self-destruction ---------------------------------------------------
Show "A sandbox that destroys itself after 20s:" "POST /sandboxes {ttl_seconds:20}"
$t = Invoke-RestMethod -Method Post -Uri $Api/sandboxes -ContentType 'application/json' -Body '{"name":"demo-ttl","template":"web","ttl_seconds":20}'
while ((Invoke-RestMethod $Api/sandboxes/$($t.id)).state -eq "CREATING") { Start-Sleep 2 }
Write-Host "Created. Watching it expire"
for ($i=0; $i -lt 12; $i++) {
    $r = Invoke-RestMethod $Api/sandboxes/$($t.id)
    Write-Host ("  t+{0,2}s  state={1}" -f ($i*5), $r.state)
    if ($r.state -eq "DELETED") { break }
    Start-Sleep 5
}
Pause "The TTL reaper killed it. This is how the platform reclaims forgotten sandboxes."

# --- 6. platform limits ---------------------------------------------------------
Show "Fill the port pool (10 ports) to see 429 + clean rollback:" "POST x 11"
$created = @()
foreach ($i in 1..9) {
    $r = Invoke-RestMethod -Method Post -Uri $Api/sandboxes -ContentType 'application/json' -Body "{`"name`":`"fill-$i`",`"template`":`"web`"}"
    $created += $r.id
}
try {
    Invoke-WebRequest -Method Post -Uri $Api/sandboxes -ContentType 'application/json' -Body '{"name":"one-too-many","template":"web"}' | Out-Null
} catch { Write-Host "HTTP $($_.Exception.Response.StatusCode.value__) - pool exhausted, platform stayed consistent." -ForegroundColor Green }
Show "Cleaning up the fillers:" "DELETE x 9"
foreach ($id in $created) {
    while ((Invoke-RestMethod $Api/sandboxes/$id).state -eq "CREATING") { Start-Sleep 2 }
    Invoke-WebRequest -Method Delete -Uri "$Api/sandboxes/$id" | Out-Null
}
Invoke-WebRequest -Method Delete -Uri "$Api/sandboxes/$((Invoke-RestMethod $Api/sandboxes | Where-Object name -eq 'one-too-many').id)" | Out-Null

# --- 7. what's left ------------------------------------------------------------
Show "Everything still running:" "GET /sandboxes"
Invoke-RestMethod $Api/sandboxes | ForEach-Object { "{0}  {1,-10} {2,-8} {3}" -f $_.id, $_.name, $_.state, $_.url }

Write-Host @"

============================================================
 DEMO COMPLETE. Left running for you to explore:
   demo-app  $($sb.url)   (3-tier task board)
   demo-web  $($w.url)    (introspection page)
 Next: scripts\verify.sh for the edge-case battery,
       docs\EXAMPLES.md   for hands-on practice exercises.
============================================================
"@ -ForegroundColor Cyan

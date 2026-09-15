#!/usr/bin/env bash
# ============================================================================
#  sandbox-scheduler - verification battery
#
#  Asserts the edge cases from docs/EDGE-CASES.md against a LIVE system:
#
#    1  healthz + upstream reachability
#    2  task job runs to SUCCEEDED with captured output
#    3  failing task (exit 3) -> FAILED with exit code in result
#    4  Idempotency-Key replay returns the same job
#    5  queue bound: 55 submits vs MAX_QUEUE=50 -> 5x 429
#    6  herd: 50 tasks drain, NEVER more than CLUSTER_CPU_UNITS active
#    7  tenant fairness: flooded tenant-a cannot block tenant-b
#    8  skip-and-fill: big head-of-queue job does not block small jobs
#    9  aging: queued job's effective priority rises; aged job beats a
#       newer higher-base-priority job
#   10  cancel: queued -> CANCELLED; running -> torn down; terminal -> 409
#   11  cancel race loop x30: zero leaked sandboxes afterwards
#   12  gang: count=2 both run; oversized gang never partially admits
#   13  max_runtime -> EXPIRED and sandbox deleted
#   14  downstream outage: jobs wait QUEUED, upstream_down=true, recovery
#   15  crash recovery: scheduler restart keeps RUNNING jobs, reaps orphans
#   16  metrics: wait percentiles + prometheus endpoint
#
#  Prereq: sandbox-api up (../sandbox-api), scheduler up.
# ============================================================================
set -uo pipefail

API="${API:-http://localhost:9010}"
SBX="${SBX:-http://localhost:9000}"
PASS=0; FAIL=0

ok()   { PASS=$((PASS+1)); echo "  PASS  $1"; }
bad()  { FAIL=$((FAIL+1)); echo "  FAIL  $1"; }
check(){ if [ "$2" = "$3" ]; then ok "$1"; else bad "$1 - expected [$2] got [$3]"; fi; }

jstate() { curl -s "$API/jobs/$1" | grep -o '"state":"[^"]*"' | head -1 | cut -d'"' -f4; }
wait_state() { # wait_state <id> <target1,target2> <timeout>
  local t=0; while [ $t -lt "$3" ]; do
    local S=$(jstate "$1")
    echo ",$2," | grep -q ",$S," && { echo "$S"; return; }
    sleep 2; t=$((t+2))
  done
  jstate "$1"
}
submit() { # submit <json> -> prints job id
  curl -s -X POST "$API/jobs" -H 'content-type: application/json' -d "$1" \
    | grep -o '"id":"[^"]*"' | head -1 | cut -d'"' -f4
}
cancel() { curl -s -o /dev/null -X POST "$API/jobs/$1/cancel"; }
engine_sbx_count() { curl -s "$SBX/sandboxes" | grep -o '"name":"sched-' | wc -l; }

echo "== 0. clean slate =="
curl -s -o /dev/null -X POST "$API/admin/reset"
for id in $(curl -s "$SBX/sandboxes" | grep -o '"id":"[^"]*"' | cut -d'"' -f4); do
  curl -s -o /dev/null -X DELETE "$SBX/sandboxes/$id"
done
sleep 3

echo "== 1. health =="
H=$(curl -s "$API/healthz")
echo "$H" | grep -q '"scheduler":"up"' && ok "scheduler up" || bad "scheduler: $H"
echo "$H" | grep -q '"upstream":"up"' && ok "upstream up" || bad "upstream: $H"

echo "== 2. task job succeeds =="
J=$(submit '{"tenant":"v","template":"blank","kind":"task","cmd":["sh","-c","echo MARKER-$RANDOM; hostname"]}')
S=$(wait_state "$J" "SUCCEEDED,FAILED" 60)
check "task SUCCEEDED" "SUCCEEDED" "$S"
curl -s "$API/jobs/$J" | grep -q "MARKER-" && ok "stdout captured in result" || bad "no result captured"

echo "== 3. failing task =="
J=$(submit '{"tenant":"v","template":"blank","kind":"task","cmd":["sh","-c","echo boom >&2; exit 3"]}')
S=$(wait_state "$J" "SUCCEEDED,FAILED" 60)
check "task FAILED" "FAILED" "$S"
curl -s "$API/jobs/$J" | grep -q '"exit_code":3' && ok "exit code 3 captured" || bad "exit code missing"

echo "== 4. idempotency =="
BODY='{"tenant":"v","template":"blank","kind":"task","cmd":["true"]}'
J1=$(curl -s -X POST "$API/jobs" -H 'content-type: application/json' -H 'Idempotency-Key: key-123' -d "$BODY" | grep -o '"id":"[^"]*"' | head -1 | cut -d'"' -f4)
J2=$(curl -s -X POST "$API/jobs" -H 'content-type: application/json' -H 'Idempotency-Key: key-123' -d "$BODY" | grep -o '"id":"[^"]*"' | head -1 | cut -d'"' -f4)
check "same key -> same job" "$J1" "$J2"
C=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$API/jobs" -H 'content-type: application/json' -H 'Idempotency-Key: key-123' -d '{"tenant":"v","template":"web","kind":"service"}')
check "same key, diff body -> 409" "409" "$C"
wait_state "$J1" "SUCCEEDED,FAILED" 60 >/dev/null

echo "== 5+6. herd + queue bound + concurrency cap =="
# Fill the cluster first so the herd MUST queue (deterministic 429s).
BLK=()
for i in 1 2 3 4; do BLK+=($(submit "{\"tenant\":\"blk\",\"template\":\"blank\",\"kind\":\"service\",\"name\":\"blk-$i\",\"max_runtime\":240}")); done
for j in "${BLK[@]}"; do wait_state "$j" "RUNNING" 40 >/dev/null; done
MAXA=0; N429=0
for i in $(seq 1 55); do
  T=$([ $((i % 2)) -eq 0 ] && echo "ha" || echo "hb")
  CODE=$(curl -s -o /tmp/h.json -w '%{http_code}' -X POST "$API/jobs" -H 'content-type: application/json' \
    -d "{\"tenant\":\"$T\",\"template\":\"blank\",\"kind\":\"task\",\"cmd\":[\"sh\",\"-c\",\"sleep 4\"]}")
  [ "$CODE" = "429" ] && N429=$((N429+1))
done
check "5 submits over MAX_QUEUE got 429" "5" "$N429"
for j in "${BLK[@]}"; do cancel "$j"; done   # free capacity; herd drains
sleep 3
# watch the drain, sampling concurrency
DEADLINE=$(( $(date +%s) + 300 ))
while [ $(date +%s) -lt $DEADLINE ]; do
  A=$(curl -s "$API/jobs" | grep -o '"state":"ADMITTED\|"state":"RUNNING' | wc -l)
  [ "$A" -gt "$MAXA" ] && MAXA=$A
  LEFT=$(curl -s "$API/jobs" | grep -o '"state":"QUEUED\|"state":"ADMITTED\|"state":"RUNNING' | wc -l)
  [ "$LEFT" = "0" ] && break
  sleep 2
done
check "herd fully drained" "0" "$LEFT"
if [ "$MAXA" -le 4 ]; then ok "concurrency never exceeded 4 (max seen: $MAXA)"; else bad "concurrency oversubscribed: $MAXA"; fi
SBXLEAK=$(engine_sbx_count)
check "no sched sandboxes leaked after herd" "0" "$SBXLEAK"

echo "== 7. tenant fairness =="
FA=(); for i in 1 2 3 4 5 6; do FA+=($(submit "{\"tenant\":\"fa\",\"template\":\"blank\",\"kind\":\"service\",\"name\":\"fa-$i\",\"max_runtime\":300}")); done
sleep 6   # let 3 of them reach RUNNING (tenant cap 3)
FB=$(submit '{"tenant":"fb","template":"blank","kind":"service","name":"fb-0","max_runtime":300}')
S=$(wait_state "$FB" "RUNNING" 30)
check "tenant-b admitted despite tenant-a flood" "RUNNING" "$S"
FAQ=$(curl -s "$API/jobs" | grep -o '"tenant":"fa"[^}]*"state":"QUEUED"' | wc -l)
if [ "$FAQ" -ge 2 ]; then ok "tenant-a still queued ($FAQ waiting) - fairness held"; else bad "fairness: fa queued=$FAQ"; fi
for j in "${FA[@]}" "$FB"; do cancel "$j"; done
sleep 5

echo "== 8. skip-and-fill =="
BIG1=$(submit '{"tenant":"s","template":"blank","kind":"service","name":"big1","cpu_units":3,"max_runtime":120}')
S=$(wait_state "$BIG1" "RUNNING" 30); check "blocker RUNNING" "RUNNING" "$S"
BIG2=$(submit '{"tenant":"s","template":"blank","kind":"service","name":"big2","cpu_units":2,"max_runtime":120}')
SMALL=$(submit '{"tenant":"s","template":"blank","kind":"service","name":"small","cpu_units":1,"max_runtime":120}')
sleep 6
check "big head-of-queue still QUEUED (does not fit)" "QUEUED" "$(jstate $BIG2)"
check "small job skip-and-filled to RUNNING" "RUNNING" "$(jstate $SMALL)"
cancel "$BIG1"; cancel "$BIG2"; cancel "$SMALL"; sleep 5

echo "== 9. aging (about 60s) =="
BLOCK=$(submit '{"tenant":"a","template":"blank","kind":"service","name":"blocker","cpu_units":4,"max_runtime":30}')
S=$(wait_state "$BLOCK" "RUNNING" 30)
B=$(submit '{"tenant":"a","template":"blank","kind":"service","name":"aged-b","priority":5,"cpu_units":4,"max_runtime":90}')
sleep 12   # B ages: +1 per 5s -> effective >= 7
C=$(submit '{"tenant":"a","template":"blank","kind":"service","name":"newer-c","priority":6,"cpu_units":4,"max_runtime":90}')
EFFB=$(curl -s "$API/queue" | python -c "import json,sys; q=[x for x in json.load(sys.stdin) if x['id']=='$B']; print(q[0]['effective_priority'] if q else 0)")
[ "$EFFB" -ge 7 ] && ok "B aged to effective $EFFB (>= 7)" || bad "B effective=$EFFB"
S=$(wait_state "$B" "RUNNING" 60)
check "aged B admitted before newer higher-base C" "RUNNING" "$S"
check "C still queued behind B" "QUEUED" "$(jstate $C)"
cancel "$B"; cancel "$C"; sleep 5

echo "== 10. cancel paths =="
FULL1=$(submit '{"tenant":"c","template":"blank","kind":"service","name":"full1","cpu_units":4,"max_runtime":120}')
wait_state "$FULL1" "RUNNING" 30 >/dev/null
Q=$(submit '{"tenant":"c","template":"blank","kind":"service","name":"queuedjob","cpu_units":4,"max_runtime":120}')
sleep 2
cancel "$Q"
check "cancel QUEUED -> CANCELLED" "CANCELLED" "$(jstate $Q)"
cancel "$FULL1"
S=$(wait_state "$FULL1" "CANCELLED" 30)
check "cancel RUNNING -> CANCELLED (torn down)" "CANCELLED" "$S"
C=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$API/jobs/$FULL1/cancel")
check "cancel terminal -> 409" "409" "$C"

echo "== 11. cancel race loop x30 =="
for i in $(seq 1 30); do
  J=$(submit '{"tenant":"race","template":"blank","kind":"task","cmd":["sh","-c","sleep 20"]}')
  cancel "$J"
done
sleep 12
LEAK=$(engine_sbx_count)
check "zero leaked sandboxes after 30 races" "0" "$LEAK"
OPEN=$(curl -s "$API/jobs" | grep -o '"state":"\(ADMITTED\|RUNNING\|CANCELLING\)"' | wc -l)
check "no jobs stuck mid-state after races" "0" "$OPEN"

echo "== 12. gang admission =="
G=$(submit '{"tenant":"g","template":"blank","kind":"task","count":2,"cmd":["sh","-c","echo gang-member"]}')
S=$(wait_state "$G" "SUCCEEDED,FAILED" 90)
check "gang count=2 SUCCEEDED" "SUCCEEDED" "$S"
N=$(curl -s "$API/jobs/$G" | grep -o '"exit_code":0' | wc -l)
check "both gang members ran" "2" "$N"
BIG=$(submit '{"tenant":"g","template":"blank","kind":"service","name":"toobig","count":5,"max_runtime":60}')   # 5x1cpu > 4cpu capacity
sleep 6
check "oversized gang never partially admits" "QUEUED" "$(jstate $BIG)"
NS=$(curl -s "$SBX/sandboxes" | grep -o "\"name\":\"sched-$BIG" | wc -l)
check "oversized gang created zero sandboxes" "0" "$NS"
cancel "$BIG"

echo "== 13. max_runtime expiry =="
E=$(submit '{"tenant":"e","template":"web","kind":"service","name":"shortlived","max_runtime":15}')
S=$(wait_state "$E" "RUNNING" 60); check "web job RUNNING" "RUNNING" "$S"
S=$(wait_state "$E" "EXPIRED" 60); check "job EXPIRED after max_runtime" "EXPIRED" "$S"
sleep 3
NS=$(curl -s "$SBX/sandboxes" | grep -o "\"name\":\"sched-$E" | wc -l)
check "expired job's sandbox deleted" "0" "$NS"

echo "== 14. downstream outage =="
docker stop sandbox-api-api-1 >/dev/null 2>&1
sleep 3
J=$(submit '{"tenant":"o","template":"blank","kind":"task","cmd":["true"]}')
sleep 6
check "job stays QUEUED while upstream down" "QUEUED" "$(jstate $J)"
M=$(curl -s "$API/metrics" | grep -o '"upstream_down":true')
check "metrics show upstream_down" '"upstream_down":true' "$M"
docker start sandbox-api-api-1 >/dev/null 2>&1
for i in $(seq 1 30); do curl -s "$SBX/healthz" 2>/dev/null | grep -q '"dind":"up"' && break; sleep 2; done
S=$(wait_state "$J" "SUCCEEDED,FAILED" 60)
check "job completes after upstream recovery" "SUCCEEDED" "$S"

echo "== 15. crash recovery + orphan sweep =="
R1=$(submit '{"tenant":"r","template":"web","kind":"service","name":"survivor1","max_runtime":300}')
R2=$(submit '{"tenant":"r","template":"blank","kind":"service","name":"survivor2","max_runtime":300}')
wait_state "$R1" "RUNNING" 60 >/dev/null; wait_state "$R2" "RUNNING" 60 >/dev/null
# plant an orphan: raw sandbox named like a scheduler sandbox, no job row
curl -s -o /dev/null -X POST "$SBX/sandboxes" -H 'content-type: application/json' \
  -d '{"name":"sched-orphan999-0","template":"blank"}'
docker restart sandbox-scheduler-scheduler-1 >/dev/null 2>&1
for i in $(seq 1 40); do curl -s "$API/healthz" 2>/dev/null | grep -q '"scheduler":"up"' && break; sleep 2; done
check "R1 still RUNNING after restart" "RUNNING" "$(jstate $R1)"
check "R2 still RUNNING after restart" "RUNNING" "$(jstate $R2)"
N1=$(curl -s "$SBX/sandboxes" | grep -o "\"name\":\"sched-$R1" | wc -l)
check "no duplicate sandbox created" "1" "$N1"
sleep 8   # janitor sweep
ORPH=$(curl -s "$SBX/sandboxes" | grep -o '"name":"sched-orphan999-0"' | wc -l)
check "planted orphan reaped by janitor" "0" "$ORPH"
cancel "$R1"; cancel "$R2"; sleep 5

echo "== 16. metrics =="
M=$(curl -s "$API/metrics")
echo "$M" | grep -q '"p50":' && ok "wait percentiles present" || bad "no percentiles: $M"
P=$(curl -s "$API/metrics/prometheus")
echo "$P" | grep -q "scheduler_queue_depth" && ok "prometheus exposition works" || bad "prom: $P"

echo
echo "======================================================"
echo "  RESULT:  $PASS passed, $FAIL failed"
echo "======================================================"
[ "$FAIL" -eq 0 ]

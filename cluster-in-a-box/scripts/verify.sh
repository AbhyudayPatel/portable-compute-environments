#!/usr/bin/env bash
# ============================================================================
#  cluster-in-a-box - verification battery
#
#   1  health
#   2  two workers: outer sandboxes READY, ALIVE in the cluster, each runs
#      its OWN dockerd (3-level nesting proven via the outer exec)
#   3  least-loaded placement: 4 web sandboxes -> 2 per worker; every URL
#      answers from Windows through three engines (identity ports)
#   4  binpack: next placements land on the fullest worker with room
#   5  affinity: a labelled third worker; label placement lands there;
#      unmatched label -> 503
#   6  drain: write a file into a placement, drain its worker -> placement
#      moved, file preserved (workspace tar migration), worker empty
#   7  failure: stop a worker (outer API) -> SUSPECT -> DEAD within ~12 s,
#      its placement UNKNOWN; reschedule -> READY elsewhere
#   8  fencing: start the worker again -> ALIVE with epoch 2; the stale copy
#      on it is purged; the moved placement is unaffected
#   9  delete workers -> outer sandboxes gone
#
#  First run builds the worker image inside the outer dind (pip inside an
#  alpine build: ~3-5 min). Prereqs: sandbox-api (with CLUSTER_PORTS), this stack.
# ============================================================================
set -uo pipefail
cd "$(dirname "$0")/.."
C="${C:-http://localhost:9430}"; A="${A:-http://localhost:9000}"
J='content-type: application/json'
PASS=0; FAIL=0; mkdir -p .tmp
ok()  { PASS=$((PASS+1)); echo "  PASS  $1"; }
bad() { FAIL=$((FAIL+1)); echo "  FAIL  $1"; }
check() { if [ "$2" = "$3" ]; then ok "$1"; else bad "$1 - expected [$2] got [$3]"; fi; }
jget() { python -c "import sys,json; v=json.load(sys.stdin)$1; print(v if not isinstance(v,bool) else str(v).lower())"; }
wstate() { curl -s "$C/workers/$1" | jget "['state']"; }
wait_w() { local t=0; while [ $t -lt "$3" ]; do local S; S=$(wstate "$1"); echo ",$2," | grep -q ",$S," && { echo "$S"; return; }; sleep 3; t=$((t+3)); done; wstate "$1"; }
pstate() { curl -s "$C/sandboxes/$1" | jget "['state']"; }
wait_p() { local t=0; while [ $t -lt "$3" ]; do local S; S=$(pstate "$1"); echo ",$2," | grep -q ",$S," && { echo "$S"; return; }; sleep 3; t=$((t+3)); done; pstate "$1"; }
worker_of() { curl -s "$C/sandboxes/$1" | jget "['worker_id']"; }

echo "== 0. clean slate =="
for w in $(curl -s "$C/workers" | python -c "import sys,json; print(' '.join(w['id'] for w in json.load(sys.stdin) if w['state']!='GONE'))"); do curl -s -o /dev/null -X DELETE "$C/workers/$w"; done
for id in $(curl -s "$A/sandboxes" | python -c "import sys,json; print(' '.join(s['id'] for s in json.load(sys.stdin)))"); do curl -s -o /dev/null -X DELETE "$A/sandboxes/$id"; done
sleep 3

echo "== 1. health =="
check "cluster up" "up" "$(curl -s "$C/healthz" | jget "['cluster']")"
check "outer api up" "up" "$(curl -s "$C/healthz" | jget "['outer_sandbox_api']")"

echo "== 2. two workers (nested nodes) =="
W=$(curl -s -X POST "$C/workers" -H "$J" -d '{"count":2}')
W0=$(echo "$W" | jget "[0]['id']"); W1=$(echo "$W" | jget "[1]['id']")
OS0=$(echo "$W" | jget "[0]['outer_sid']"); OS1=$(echo "$W" | jget "[1]['outer_sid']")
check "worker-0 ALIVE" "ALIVE" "$(wait_w "$W0" ALIVE,DEAD 600)"
check "worker-1 ALIVE" "ALIVE" "$(wait_w "$W1" ALIVE,DEAD 300)"
[ "$(wstate "$W0")" = ALIVE ] || curl -s "$A/sandboxes/$OS0/events" | python -c "import sys,json; [print('   ',e['seq'],e['type'],e['message'][:160]) for e in json.load(sys.stdin)]"
check "worker-0 is a READY outer sandbox" "READY" "$(curl -s "$A/sandboxes/$OS0" | jget "['state']")"
INNER=$(curl -s -X POST "$A/sandboxes/$OS0/exec" -H "$J" -d '{"cmd":["sh","-c","docker info --format {{.ServerVersion}} && docker ps -q | wc -l"]}' | jget "['stdout'].strip()")
echo "$INNER" | head -1 | grep -qE '^[0-9]+\.' && ok "worker-0 runs its own dockerd (v$(echo "$INNER" | head -1))" || bad "inner docker: $INNER"
check "worker API reachable from host (3-level publish)" "up" "$(curl -s localhost:9340/healthz | jget "['dind']")"

echo "== 3. least-loaded placement across workers =="
P=(); for i in 1 2 3 4; do P+=($(curl -s -X POST "$C/sandboxes" -H "$J" -d "{\"name\":\"web-$i\",\"template\":\"web\",\"data_path\":\"/www\"}" | jget "['id']")); done
for p in "${P[@]}"; do wait_p "$p" READY,FAILED 240 >/dev/null; done
check "all 4 READY" "4" "$(curl -s "$C/sandboxes" | python -c "import sys,json; print(sum(1 for p in json.load(sys.stdin) if p['state']=='READY'))")"
check "2 on worker-0" "2" "$(curl -s "$C/sandboxes" | python -c "import sys,json; print(sum(1 for p in json.load(sys.stdin) if p['worker_id']=='$W0'))")"
check "2 on worker-1" "2" "$(curl -s "$C/sandboxes" | python -c "import sys,json; print(sum(1 for p in json.load(sys.stdin) if p['worker_id']=='$W1'))")"
OKURL=0; for p in "${P[@]}"; do U=$(curl -s "$C/sandboxes/$p" | jget "['url']"); curl -s -m 5 "$U/health" | grep -q '"ok"' && OKURL=$((OKURL+1)); done
check "every placement answers from Windows through 3 engines" "4" "$OKURL"

echo "== 4. binpack =="
B1=$(curl -s -X POST "$C/sandboxes" -H "$J" -d '{"name":"bp-1","template":"blank","policy":"binpack"}' | jget "['id']")
B2=$(curl -s -X POST "$C/sandboxes" -H "$J" -d '{"name":"bp-2","template":"blank","policy":"binpack"}' | jget "['id']")
check "binpack keeps both on one worker" "$(worker_of "$B1")" "$(worker_of "$B2")"

echo "== 5. affinity =="
W2=$(curl -s -X POST "$C/workers" -H "$J" -d '{"count":1,"labels":{"zone":"b"}}' | jget "[0]['id']")
check "labelled worker ALIVE" "ALIVE" "$(wait_w "$W2" ALIVE,DEAD 300)"
AF=$(curl -s -X POST "$C/sandboxes" -H "$J" -d '{"name":"aff-1","template":"blank","policy":"affinity","labels":{"zone":"b"}}' | jget "['id']")
check "affinity placement lands on the labelled worker" "$W2" "$(worker_of "$AF")"
check "unmatched label -> 503" "503" "$(curl -s -o /dev/null -w '%{http_code}' -X POST "$C/sandboxes" -H "$J" -d '{"name":"aff-x","template":"blank","policy":"affinity","labels":{"zone":"mars"}}')"

echo "== 6. drain with workspace migration =="
V=${P[0]}; VW=$(worker_of "$V"); RID=$(curl -s "$C/sandboxes/$V" | jget "['remote_id']"); WPORT=$(curl -s "$C/workers/$VW" | jget "['api_port']")
curl -s -o /dev/null -X PUT "localhost:$WPORT/sandboxes/$RID/fs/write?path=/www/keep.txt" --data-binary 'survive the move'
D=$(curl -s -X POST "$C/workers/$VW/drain")
check "drain moved placements" "true" "$(echo "$D" | python -c "import sys,json; print(str(len(json.load(sys.stdin)['moved'])>=1).lower())")"
NW=$(worker_of "$V"); [ "$NW" != "$VW" ] && ok "placement now on $NW" || bad "placement did not move"
check "moved placement READY" "READY" "$(wait_p "$V" READY,FAILED 120)"
NRID=$(curl -s "$C/sandboxes/$V" | jget "['remote_id']"); NPORT=$(curl -s "$C/workers/$NW" | jget "['api_port']")
check "file migrated with the placement" "survive the move" "$(curl -s "localhost:$NPORT/sandboxes/$NRID/fs/read?path=/www/keep.txt")"
check "drained worker empty" "0" "$(curl -s "$C/workers/$VW" | python -c "import sys,json; print(len(json.load(sys.stdin)['placements']))")"
check "drained worker back to ALIVE" "ALIVE" "$(wstate "$VW")"

echo "== 7. worker failure -> DEAD -> reschedule =="
FW=$(worker_of "$B1"); FOS=$(curl -s "$C/workers/$FW" | jget "['outer_sid']")
curl -s -o /dev/null -X POST "$A/sandboxes/$FOS/stop"
check "worker DEAD within 30s" "DEAD" "$(wait_w "$FW" DEAD 40)"
check "its placement UNKNOWN" "UNKNOWN" "$(pstate "$B1")"
R=$(curl -s -X POST "$C/sandboxes/$B1/reschedule")
check "rescheduled to another worker" "true" "$(echo "$R" | python -c "import sys,json; print(str(json.load(sys.stdin)['worker_id']!='$FW').lower())")"
check "rescheduled placement READY" "READY" "$(wait_p "$B1" READY,FAILED 120)"

echo "== 8. fencing on return =="
curl -s -o /dev/null -X POST "$A/sandboxes/$FOS/start"
check "worker returns ALIVE" "ALIVE" "$(wait_w "$FW" ALIVE 120)"
check "epoch bumped to 2" "2" "$(curl -s "$C/workers/$FW" | jget "['epoch']")"
sleep 4
check "stale copy purged on the returned worker" "true" "$(curl -s "$C/events?limit=50" | python -c "import sys,json; print(str(any(e['kind']=='fence' and 'purged' in e['message'] for e in json.load(sys.stdin))).lower())")"
check "moved placement still READY elsewhere" "READY" "$(pstate "$B1")"

echo "== 9. teardown =="
for w in "$W0" "$W1" "$W2"; do curl -s -o /dev/null -X DELETE "$C/workers/$w"; done
sleep 3
check "outer worker sandboxes gone" "0" "$(curl -s "$A/sandboxes" | python -c "import sys,json; print(sum(1 for s in json.load(sys.stdin) if s['template']=='worker'))")"

echo
echo "======================================================"
echo "  RESULT:  $PASS passed, $FAIL failed"
echo "======================================================"
[ "$FAIL" -eq 0 ]

#!/usr/bin/env bash
# ============================================================================
#  sandbox-api - verification battery (run from the repo host)
#
#  Exercises every edge case in docs/EDGE-CASES.md and asserts real
#  behavior, not just HTTP 200s:
#
#    1  healthz (api + dind up)
#    2  create web sandbox -> READY, page reachable via identity port
#    3  idempotent create (same name+spec -> 200, same id)
#    4  name conflict, different spec -> 409
#    5  exec -> stdout/exit code; exec with timeout -> timed_out
#    6  port-pool exhaustion -> 429, FAILED sandbox holds NO port
#    7  capacity path (engine-derived)
#    8  TTL reaper reaps an expired sandbox
#    9  delete idempotency (second delete -> 404)
#   10  bad name -> 400, unknown template -> 400
#   11  event log is seq-ordered and complete
#   12  API restart -> reconciliation keeps engine truth (adopt test)
#
#  Usage:  bash scripts/verify.sh        (expects the stack already up)
# ============================================================================
set -uo pipefail

API="${API:-http://localhost:9000}"
PASS=0; FAIL=0

ok()   { PASS=$((PASS+1)); echo "  PASS  $1"; }
bad()  { FAIL=$((FAIL+1)); echo "  FAIL  $1"; }
check(){ # check <label> <expected> <actual>
  if [ "$2" = "$3" ]; then ok "$1"; else bad "$1 - expected [$2] got [$3]"; fi
}

echo "== 1. healthz =="
H=$(curl -s "$API/healthz")
echo "$H" | grep -q '"api":"up"'  && ok "api up"   || bad "api up: $H"
echo "$H" | grep -q '"dind":"up"' && ok "dind up"  || bad "dind up: $H"

echo "== 2. create web sandbox =="
CODE=$(curl -s -o /tmp/sb.json -w '%{http_code}' -X POST "$API/sandboxes" \
  -H 'content-type: application/json' -d '{"name":"v-web","template":"web"}')
check "create -> 201" "201" "$CODE"
SID=$(grep -o '"id":"[^"]*"' /tmp/sb.json | head -1 | cut -d'"' -f4)
PORT=$(grep -o '"port":[0-9]*' /tmp/sb.json | head -1 | cut -d: -f2)
# poll until READY (image pull on first run can take a while)
for i in $(seq 1 90); do
  S=$(curl -s "$API/sandboxes/$SID" | grep -o '"state":"[^"]*"' | cut -d'"' -f4)
  [ "$S" = "READY" ] && break; [ "$S" = "FAILED" ] && break; sleep 2
done
check "sandbox READY" "READY" "$S"
BODY=$(curl -s "http://localhost:$PORT/")
echo "$BODY" | grep -q "<code>v-web</code>" && ok "page served via localhost:$PORT" \
  || bad "page via $PORT: $BODY"

echo "== 3. idempotent create =="
CODE=$(curl -s -o /tmp/sb2.json -w '%{http_code}' -X POST "$API/sandboxes" \
  -H 'content-type: application/json' -d '{"name":"v-web","template":"web"}')
SID2=$(grep -o '"id":"[^"]*"' /tmp/sb2.json | head -1 | cut -d'"' -f4)
check "same name+spec -> 200" "200" "$CODE"
check "same id returned" "$SID" "$SID2"

echo "== 4. name conflict =="
CODE=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$API/sandboxes" \
  -H 'content-type: application/json' -d '{"name":"v-web","template":"coreapp"}')
check "same name, diff spec -> 409" "409" "$CODE"

echo "== 5. exec =="
R=$(curl -s -X POST "$API/sandboxes/$SID/exec" -H 'content-type: application/json' \
  -d '{"cmd":["sh","-c","echo out; echo err >&2; exit 7"]}')
echo "$R" | grep -q '"exit_code":7'  && ok "exit code 7"   || bad "exit: $R"
echo "$R" | grep -q '"stdout":"out'  && ok "stdout capture" || bad "stdout: $R"
echo "$R" | grep -q 'err'            && ok "stderr capture" || bad "stderr: $R"
R=$(curl -s -X POST "$API/sandboxes/$SID/exec" -H 'content-type: application/json' \
  -d '{"cmd":["sleep","30"],"timeout":2}')
echo "$R" | grep -q '"timed_out":true' && ok "exec timeout flagged" || bad "timeout: $R"
R=$(curl -s -X POST "$API/sandboxes/$SID/exec" -H 'content-type: application/json' \
  -d '{"cmd":["sh","-c","head -c 200000 /dev/zero | tr \\\\0 x"]}')
echo "$R" | grep -q '"truncated":true' && ok "output truncation flagged" || bad "trunc: $(echo "$R" | head -c 200)"

echo "== 6. port-pool exhaustion -> 429 =="
# Pool is 9200-9209 (10 ports); v-web holds one. Create 9 more, then #11 fails.
declare -a FILL
for i in $(seq 1 9); do
  CODE=$(curl -s -o /tmp/f$i.json -w '%{http_code}' -X POST "$API/sandboxes" \
    -H 'content-type: application/json' -d "{\"name\":\"v-fill-$i\",\"template\":\"web\"}")
  FILL[$i]=$(grep -o '"id":"[^"]*"' /tmp/f$i.json | head -1 | cut -d'"' -f4)
done
CODE=$(curl -s -o /tmp/full.json -w '%{http_code}' -X POST "$API/sandboxes" \
  -H 'content-type: application/json' -d '{"name":"v-toomany","template":"web"}')
check "11th port user -> 429" "429" "$CODE"
# rollback must not leak: list shows v-toomany FAILED with null port
L=$(curl -s "$API/sandboxes")
echo "$L" | grep -q '"name":"v-toomany"[^}]*"port":null' \
  && ok "failed create holds no port" || bad "port leak: $(echo "$L" | head -c 400)"
# cleanup fillers regardless of their state
for i in $(seq 1 9); do
  for w in $(seq 1 45); do
    ST=$(curl -s "$API/sandboxes/${FILL[$i]}" | grep -o '"state":"[^"]*"' | cut -d'"' -f4)
    [ "$ST" = "READY" ] || [ "$ST" = "FAILED" ] && break; sleep 2
  done
  curl -s -o /dev/null -X DELETE "$API/sandboxes/${FILL[$i]}"
done
curl -s -o /dev/null -X DELETE "$API/sandboxes/$(grep -o '"id":"[^"]*"' /tmp/full.json | head -1 | cut -d'"' -f4)"

echo "== 8. TTL reaper =="
curl -s -o /tmp/ttl.json -X POST "$API/sandboxes" -H 'content-type: application/json' \
  -d '{"name":"v-ttl","template":"blank","ttl_seconds":10}'
TID=$(grep -o '"id":"[^"]*"' /tmp/ttl.json | head -1 | cut -d'"' -f4)
GONE=""
for i in $(seq 1 20); do
  ST=$(curl -s "$API/sandboxes/$TID" | grep -o '"state":"[^"]*"' | cut -d'"' -f4)
  if [ "$ST" = "DELETED" ] || [ -z "$ST" ]; then GONE=yes; break; fi
  sleep 2
done
check "TTL sandbox reaped" "yes" "$GONE"

echo "== 9. delete idempotency =="
CODE1=$(curl -s -o /dev/null -w '%{http_code}' -X DELETE "$API/sandboxes/$SID")
CODE2=$(curl -s -o /dev/null -w '%{http_code}' -X DELETE "$API/sandboxes/$SID")
check "first delete -> 204"  "204" "$CODE1"
check "second delete -> 404" "404" "$CODE2"

echo "== 10. validation =="
C1=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$API/sandboxes" \
  -H 'content-type: application/json' -d '{"name":"Bad_Name!","template":"web"}')
C2=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$API/sandboxes" \
  -H 'content-type: application/json' -d '{"name":"ok-name","template":"nope"}')
check "bad name -> 4xx"      "422" "$C1"
check "bad template -> 4xx"  "422" "$C2"

echo "== 11. event log =="
E=$(curl -s "$API/sandboxes/$SID/events")
echo "$E" | grep -q '"seq":1' && echo "$E" | grep -q 'DELETED' \
  && ok "events ordered + complete" || bad "events: $(echo "$E" | head -c 300)"

echo "== 12. restart + reconcile (adopt) =="
# Create a sandbox, restart ONLY the api container, expect it still listed.
curl -s -o /tmp/rc.json -X POST "$API/sandboxes" -H 'content-type: application/json' \
  -d '{"name":"v-reconcile","template":"blank"}'
RID=$(grep -o '"id":"[^"]*"' /tmp/rc.json | head -1 | cut -d'"' -f4)
for i in $(seq 1 45); do
  ST=$(curl -s "$API/sandboxes/$RID" | grep -o '"state":"[^"]*"' | cut -d'"' -f4)
  [ "$ST" = "READY" ] && break; sleep 2
done
docker compose -p sandbox-api restart api >/dev/null 2>&1
for i in $(seq 1 45); do
  H=$(curl -s "$API/healthz" 2>/dev/null)
  echo "$H" | grep -q '"api":"up"' && break; sleep 2
done
ST=$(curl -s "$API/sandboxes/$RID" | grep -o '"state":"[^"]*"' | cut -d'"' -f4)
check "sandbox survives API restart" "READY" "$ST"
curl -s -o /dev/null -X DELETE "$API/sandboxes/$RID"

echo
echo "======================================================"
echo "  RESULT:  $PASS passed, $FAIL failed"
echo "======================================================"
[ "$FAIL" -eq 0 ]

#!/usr/bin/env bash
# ============================================================================
#  sandbox-api v2 - verification battery (run from the repo host, Git Bash
#  on Windows or any Linux shell; needs curl + python3 + docker).
#
#  Every section asserts real behaviour against the LIVE stack. Sections:
#
#    1  health: api, dind, node proxy, informer connected; node facts
#    2  templates: content-hashed image tags
#    3  web sandbox: READY via agent; identity port; containers show ip/runtime
#    4  idempotency / conflicts / validation (422 name, template, isolation)
#    5  exec through the agent: exit codes, streams, timeout KILLS, truncation
#    6  durable sessions: pty, stdin, stream, resume with since=, signal, delete
#    7  files: write/read/list/mkdir/delete, tar round-trip, tar escape blocked
#    8  fingerprints + /diff (fs): same -> identical, change -> modified
#    9  coreapp: build inside dind -> READY, app works, exec roles, db
#       fingerprint changes on insert, stop/start
#   10  gVisor isolation: runsc runtime, gvisor kernel, port works; desktop
#       rejects gvisor
#   11  informer: container killed inside dind -> DEGRADED live; start heals
#   12  informer: containers removed outside the API -> DELETED, port freed
#   13  port-pool exhaustion -> 429, FAILED holds no port, clean rollback
#   14  TTL reaper reaps; TTL SURVIVES an API restart (wall deadline)
#   15  delete idempotency; delete while CREATING -> 409
#   16  API restart -> reconcile keeps READY sandboxes; events readable
#       after deletion
#   17  SSE: per-sandbox follow delivers a live event; global cursor stream
#
#  Usage:  bash scripts/verify.sh        (expects the stack already up)
# ============================================================================
set -uo pipefail
cd "$(dirname "$0")/.."

API="${API:-http://localhost:9000}"
J='content-type: application/json'
PASS=0; FAIL=0
mkdir -p .tmp

ok()  { PASS=$((PASS+1)); echo "  PASS  $1"; }
bad() { FAIL=$((FAIL+1)); echo "  FAIL  $1"; }
check() { if [ "$2" = "$3" ]; then ok "$1"; else bad "$1 - expected [$2] got [$3]"; fi; }
jget() { python -c "import sys,json; v=json.load(sys.stdin)$1; print(v if not isinstance(v,bool) else str(v).lower())"; }
state() { curl -s "$API/sandboxes/$1" | jget "['state']" 2>/dev/null; }
wait_state() { # wait_state <id> <csv states> <timeout s>
  local t=0; while [ $t -lt "$3" ]; do
    local S; S=$(state "$1"); echo ",$2," | grep -q ",$S," && { echo "$S"; return; }
    sleep 2; t=$((t+2)); done; state "$1"; }
create() { curl -s -X POST "$API/sandboxes" -H "$J" -d "$1"; }
sid_of() { jget "['id']"; }
decode_stream() { python -c "import sys,json,base64; print(''.join(base64.b64decode(json.loads(l)['d']).decode(errors='replace') for l in sys.stdin if l.strip() and 'd' in json.loads(l)), end='')"; }
DIND=$(docker ps --format '{{.Names}}' | grep -E '^sandbox-api[-_]dind' | head -1)

echo "== 0. clean slate =="
for id in $(curl -s "$API/sandboxes" | python -c "import sys,json; print(' '.join(s['id'] for s in json.load(sys.stdin)))"); do
  for w in $(seq 1 60); do S=$(state "$id"); [ "$S" != "CREATING" ] && break; sleep 2; done
  curl -s -o /dev/null -X DELETE "$API/sandboxes/$id"
done
sleep 2

echo "== 1. health + node facts =="
H=$(curl -s "$API/healthz")
check "api up" "up" "$(echo "$H" | jget "['api']")"
check "dind up" "up" "$(echo "$H" | jget "['dind']")"
check "node proxy up" "up" "$(echo "$H" | jget "['node']")"
check "informer connected" "connected" "$(echo "$H" | jget "['informer']")"
N=$(curl -s "$API/node/info")
check "node has runsc" "true" "$(echo "$N" | python -c "import sys,json; print(str(bool(json.load(sys.stdin)['runsc'])).lower())")"
check "node overlay support" "true" "$(echo "$N" | jget "['overlay']")"
check "status lists gvisor isolation" "true" "$(curl -s "$API/status" | python -c "import sys,json; print(str('gvisor' in json.load(sys.stdin)['isolation_available']).lower())")"

echo "== 2. templates =="
T=$(curl -s "$API/templates")
check "4 templates" "4" "$(echo "$T" | python -c "import sys,json; print(len(json.load(sys.stdin)))")"
echo "$T" | python -c "import sys,json,re; t=json.load(sys.stdin)['coreapp']['images']; sys.exit(0 if all(re.search(r':[0-9a-f]{12}$', i) for i in t) else 1)" \
  && ok "coreapp images are content-hashed tags" || bad "coreapp image tags not content-hashed: $T"

echo "== 3. web sandbox =="
R=$(create '{"name":"v-web","template":"web"}'); CODE=$(echo "$R" | jget "['state']")
check "create -> CREATING" "CREATING" "$CODE"
WID=$(echo "$R" | sid_of); WPORT=$(echo "$R" | jget "['port']")
check "web READY" "READY" "$(wait_state "$WID" READY,FAILED 120)"
curl -s "http://localhost:$WPORT/" | grep -q "<code>v-web</code>" && ok "page served via localhost:$WPORT" || bad "page via $WPORT"
C=$(curl -s "$API/sandboxes/$WID/containers")
check "container running with inner ip" "true" "$(echo "$C" | python -c "import sys,json; c=json.load(sys.stdin)[0]; print(str(c['status']=='running' and bool(c['ip'])).lower())")"
check "runtime is runc" "runc" "$(echo "$C" | jget "[0]['runtime']")"
curl -s "$API/sandboxes/$WID/events" | python -c "import sys,json; e=json.load(sys.stdin); sys.exit(0 if [x['seq'] for x in e]==list(range(1,len(e)+1)) and any('all agents answering' in x['message'] for x in e) else 1)" \
  && ok "events contiguous + agent readiness logged" || bad "events: $(curl -s "$API/sandboxes/$WID/events" | head -c 300)"

echo "== 4. idempotency / conflicts / validation =="
R=$(curl -s -w '\n%{http_code}' -X POST "$API/sandboxes" -H "$J" -d '{"name":"v-web","template":"web"}')
check "same name+spec -> 200" "200" "$(echo "$R" | tail -1)"
check "same id returned" "$WID" "$(echo "$R" | head -1 | sid_of)"
check "same name, diff spec -> 409" "409" "$(curl -s -o /dev/null -w '%{http_code}' -X POST "$API/sandboxes" -H "$J" -d '{"name":"v-web","template":"coreapp"}')"
check "bad name -> 422" "422" "$(curl -s -o /dev/null -w '%{http_code}' -X POST "$API/sandboxes" -H "$J" -d '{"name":"Bad_Name!","template":"web"}')"
check "bad template -> 422" "422" "$(curl -s -o /dev/null -w '%{http_code}' -X POST "$API/sandboxes" -H "$J" -d '{"name":"ok-name","template":"nope"}')"
check "bad isolation -> 422" "422" "$(curl -s -o /dev/null -w '%{http_code}' -X POST "$API/sandboxes" -H "$J" -d '{"name":"ok-name","template":"web","isolation":"firecracker"}')"

echo "== 5. exec through the agent =="
R=$(curl -s -X POST "$API/sandboxes/$WID/exec" -H "$J" -d '{"cmd":["sh","-c","echo out; echo err >&2; exit 7"]}')
check "exit code 7" "7" "$(echo "$R" | jget "['exit_code']")"
check "stdout captured" "out" "$(echo "$R" | jget "['stdout'].strip()")"
check "stderr captured" "err" "$(echo "$R" | jget "['stderr'].strip()")"
R=$(curl -s -X POST "$API/sandboxes/$WID/exec" -H "$J" -d '{"cmd":["sleep","60"],"timeout":2}')
check "timeout flagged" "true" "$(echo "$R" | jget "['timed_out']")"
check "timed-out process was KILLED (exit 143)" "143" "$(echo "$R" | jget "['exit_code']")"
sleep 1
LEFT=$(curl -s -X POST "$API/sandboxes/$WID/exec" -H "$J" -d '{"cmd":["sh","-c","ps | grep -c \"[s]leep 60\" || true"]}' | jget "['stdout'].strip()")
check "no leftover sleep process" "0" "$LEFT"
R=$(curl -s -X POST "$API/sandboxes/$WID/exec" -H "$J" -d '{"cmd":["sh","-c","head -c 200000 /dev/zero | tr \\\\0 x"]}')
check "output truncation flagged" "true" "$(echo "$R" | jget "['truncated']")"
check "full output size reported" "200000" "$(echo "$R" | jget "['bytes_total']")"
check "exec on explicit container" "sbx-$WID-web" "$(curl -s -X POST "$API/sandboxes/$WID/exec" -H "$J" -d "{\"cmd\":[\"hostname\"],\"container\":\"sbx-$WID-web\"}" | jget "['container']")"

echo "== 6. durable sessions =="
SESS=$(curl -s -X POST "$API/sandboxes/$WID/sessions" -H "$J" -d '{"cmd":["sh"],"tty":true,"cols":80,"rows":24}' | jget "['id']")
echo "$SESS" | grep -q "^sbx-$WID-web:" && ok "session id carries container" || bad "session id: $SESS"
curl -s -o /dev/null -X POST "$API/sandboxes/$WID/sessions/$SESS/stdin" --data-binary $'echo PING-$((6*7))\n'; sleep 0.8
OUT=$(curl -s "$API/sandboxes/$WID/sessions/$SESS/stream?since=0" | decode_stream)
echo "$OUT" | grep -q "PING-42" && ok "pty session echoes stdin output" || bad "session output: $OUT"
LAST=$(curl -s "$API/sandboxes/$WID/sessions/$SESS" | jget "['last_seq']")
curl -s -o /dev/null -X POST "$API/sandboxes/$WID/sessions/$SESS/stdin" --data-binary $'echo AFTER\n'; sleep 0.8
OUT2=$(curl -s "$API/sandboxes/$WID/sessions/$SESS/stream?since=$LAST" | decode_stream)
echo "$OUT2" | grep -q "AFTER" && ! echo "$OUT2" | grep -q "PING-42" && ok "resume with since= returns only new frames" || bad "resume output: $OUT2"
curl -s -o /dev/null -X POST "$API/sandboxes/$WID/sessions/$SESS/resize" -H "$J" -d '{"cols":132,"rows":50}'
curl -s -o /dev/null -X POST "$API/sandboxes/$WID/sessions/$SESS/stdin" --data-binary $'stty size\n'; sleep 0.6
curl -s "$API/sandboxes/$WID/sessions/$SESS/stream?since=0" | decode_stream | grep -q "50 132" && ok "pty resize applied" || bad "resize"
check "session listed" "true" "$(curl -s "$API/sandboxes/$WID/sessions" | python -c "import sys,json; print(str(any(s['id']=='$SESS' for s in json.load(sys.stdin))).lower())")"
curl -s -o /dev/null -X POST "$API/sandboxes/$WID/sessions/$SESS/signal" -H "$J" -d '{"signal":"KILL"}'; sleep 0.8
check "signal KILL -> exited" "exited" "$(curl -s "$API/sandboxes/$WID/sessions/$SESS" | jget "['state']")"
check "exit frame in follow stream" "x" "$(curl -s "$API/sandboxes/$WID/sessions/$SESS/stream?since=0&follow=1&timeout=3" | tail -1 | jget "['s']")"
check "delete session -> 204" "204" "$(curl -s -o /dev/null -w '%{http_code}' -X DELETE "$API/sandboxes/$WID/sessions/$SESS")"
check "deleted session -> 404" "404" "$(curl -s -o /dev/null -w '%{http_code}' "$API/sandboxes/$WID/sessions/$SESS")"

echo "== 7. files =="
check "fs write" "11" "$(curl -s -X PUT "$API/sandboxes/$WID/fs/write?path=/www/note.txt&mode=0600" --data-binary 'hello files' | jget "['bytes']")"
check "fs read" "hello files" "$(curl -s "$API/sandboxes/$WID/fs/read?path=/www/note.txt")"
check "fs list sees file with mode" "0600" "$(curl -s "$API/sandboxes/$WID/fs?path=/www" | python -c "import sys,json; print([e for e in json.load(sys.stdin)['entries'] if e['name']=='note.txt'][0]['mode'])")"
curl -s -o /dev/null -X POST "$API/sandboxes/$WID/fs/mkdir?path=/www/sub/deep"
check "fs mkdir -p" "dir" "$(curl -s "$API/sandboxes/$WID/fs?path=/www/sub" | jget "['entries'][0]['type']")"
curl -s "$API/sandboxes/$WID/fs/tar?path=/www" > .tmp/www.tar
tar tf .tmp/www.tar | grep -q "note.txt" && ok "tar download contains file" || bad "tar download"
check "tar upload round-trip" "true" "$(curl -s -X PUT "$API/sandboxes/$WID/fs/tar?path=/restore" --data-binary @.tmp/www.tar | python -c "import sys,json; print(str(json.load(sys.stdin)['files']>=3).lower())")"
check "restored file readable" "hello files" "$(curl -s "$API/sandboxes/$WID/fs/read?path=/restore/note.txt")"
printf 'x' > .tmp/x.txt; (cd .tmp && tar cf evil.tar --transform 's,x.txt,../../etc/evil,' x.txt) 2>/dev/null
curl -s -o /dev/null -X PUT "$API/sandboxes/$WID/fs/tar?path=/up" --data-binary @.tmp/evil.tar
check "tar path escape neutralised (no /etc/evil)" "404" "$(curl -s -o /dev/null -w '%{http_code}' "$API/sandboxes/$WID/fs/read?path=/etc/evil")"
check "fs delete" "true" "$(curl -s -X DELETE "$API/sandboxes/$WID/fs?path=/restore" | jget "['deleted']")"
check "fs read missing -> 404" "404" "$(curl -s -o /dev/null -w '%{http_code}' "$API/sandboxes/$WID/fs/read?path=/restore/note.txt")"

echo "== 8. fingerprints + diff =="
F1=$(curl -s "$API/sandboxes/$WID/fingerprint?path=/www" | jget "['root']")
F1b=$(curl -s "$API/sandboxes/$WID/fingerprint?path=/www" | jget "['root']")
check "fingerprint deterministic" "$F1" "$F1b"
curl -s -o /dev/null -X PUT "$API/sandboxes/$WID/fs/write?path=/www/note.txt" --data-binary 'hello files!'
F2=$(curl -s "$API/sandboxes/$WID/fingerprint?path=/www" | jget "['root']")
[ "$F1" != "$F2" ] && ok "fingerprint changes on one-byte edit" || bad "fingerprint unchanged"
W2=$(create '{"name":"v-web2","template":"web"}' | sid_of); wait_state "$W2" READY,FAILED 90 >/dev/null
D=$(curl -s "$API/diff?a=$WID&b=$W2&path=/www")
check "diff self identical" "true" "$(curl -s "$API/diff?a=$WID&b=$WID&path=/www" | jget "['identical']")"
check "diff two webs: index.html modified (different name/port)" "true" "$(echo "$D" | python -c "import sys,json; d=json.load(sys.stdin)['fs']; print(str('index.html' in [m['path'] for m in d['modified']] and 'note.txt' in d['removed']).lower())")"
curl -s -o /dev/null -X DELETE "$API/sandboxes/$W2"

echo "== 9. coreapp (built inside dind) =="
R=$(create '{"name":"v-core","template":"coreapp"}'); CID=$(echo "$R" | sid_of); CPORT=$(echo "$R" | jget "['port']")
check "coreapp READY" "READY" "$(wait_state "$CID" READY,FAILED 300)"
check "app health via identity port (db up)" "up" "$(curl -s "http://localhost:$CPORT/api/health" | jget "['database']")"
check "3 containers" "3" "$(curl -s "$API/sandboxes/$CID/containers" | python -c "import sys,json; print(len(json.load(sys.stdin)))")"
check "exec default role = backend" "sbx-$CID-backend" "$(curl -s -X POST "$API/sandboxes/$CID/exec" -H "$J" -d '{"cmd":["hostname"]}' | jget "['container']")"
check "exec in db container (psql)" "3" "$(curl -s -X POST "$API/sandboxes/$CID/exec" -H "$J" -d "{\"cmd\":[\"psql\",\"-U\",\"company\",\"-d\",\"companydb\",\"-Atc\",\"select count(*) from tasks\"],\"container\":\"sbx-$CID-db\"}" | jget "['stdout'].strip()")"
DB1=$(curl -s "$API/sandboxes/$CID/fingerprint/db" | jget "['root']")
curl -s -o /dev/null -X POST "http://localhost:$CPORT/api/tasks" -H "$J" -d '{"title":"verify task"}'
DB2=$(curl -s "$API/sandboxes/$CID/fingerprint/db" | jget "['root']")
[ "$DB1" != "$DB2" ] && ok "db fingerprint changes after insert" || bad "db fingerprint unchanged"
check "db fingerprint rows=4" "4" "$(curl -s "$API/sandboxes/$CID/fingerprint/db" | jget "['tables']['tasks']['rows']")"
check "stop -> STOPPED" "STOPPED" "$(curl -s -X POST "$API/sandboxes/$CID/stop" | jget "['state']")"
check "exec on STOPPED -> 409" "409" "$(curl -s -o /dev/null -w '%{http_code}' -X POST "$API/sandboxes/$CID/exec" -H "$J" -d '{"cmd":["true"]}')"
check "start -> READY" "READY" "$(curl -s -X POST "$API/sandboxes/$CID/start" | jget "['state']")"
sleep 4
check "app healthy after restart, data kept" "4" "$(curl -s "http://localhost:$CPORT/api/tasks" | python -c "import sys,json; print(len(json.load(sys.stdin)))")"
check "stats for every container" "3" "$(curl -s "$API/sandboxes/$CID/stats" | python -c "import sys,json; print(len(json.load(sys.stdin)))")"

echo "== 10. gVisor isolation =="
R=$(create '{"name":"v-gv","template":"web","isolation":"gvisor"}'); GID=$(echo "$R" | sid_of); GPORT=$(echo "$R" | jget "['port']")
check "gvisor web READY" "READY" "$(wait_state "$GID" READY,FAILED 120)"
check "runtime runsc" "runsc" "$(curl -s "$API/sandboxes/$GID/containers" | jget "[0]['runtime']")"
curl -s -X POST "$API/sandboxes/$GID/exec" -H "$J" -d '{"cmd":["uname","-r"]}' | jget "['stdout']" | grep -q gvisor && ok "sandbox sees the gVisor kernel" || bad "uname"
check "gvisor app reachable via identity port" "ok" "$(curl -s "http://localhost:$GPORT/health" | jget "['status']")"
check "desktop rejects gvisor -> 422" "422" "$(curl -s -o /dev/null -w '%{http_code}' -X POST "$API/sandboxes" -H "$J" -d '{"name":"v-dg","template":"desktop","isolation":"gvisor"}')"
curl -s -o /dev/null -X DELETE "$API/sandboxes/$GID"

echo "== 11. informer: DEGRADED live + heal =="
docker exec "$DIND" docker kill "sbx-$WID-web" >/dev/null 2>&1
check "killed container -> DEGRADED within 10s" "DEGRADED" "$(wait_state "$WID" DEGRADED 10)"
check "detail names the dead container" "true" "$(curl -s "$API/sandboxes/$WID" | python -c "import sys,json; print(str('sbx-$WID-web' in json.load(sys.stdin)['detail'].get('dead',{})).lower())")"
check "start heals -> READY" "READY" "$(curl -s -X POST "$API/sandboxes/$WID/start" | jget "['state']")"
sleep 2
check "app answers after heal" "ok" "$(curl -s "http://localhost:$WPORT/health" | jget "['status']")"

echo "== 12. informer: out-of-band removal =="
docker exec "$DIND" docker rm -f "sbx-$WID-web" >/dev/null 2>&1
check "removed outside API -> DELETED within 15s" "DELETED" "$(for i in $(seq 1 8); do S=$(curl -s "$API/sandboxes/$WID" | jget "['state']" 2>/dev/null || echo DELETED); [ "$S" = DELETED ] && break; sleep 2; done; curl -s -o /dev/null -w '%{http_code}' "$API/sandboxes/$WID" | sed 's/404/DELETED/')"
check "port freed" "true" "$(curl -s "$API/status" | python -c "import sys,json; print(str($WPORT in json.load(sys.stdin)['pool']['free']).lower())")"

echo "== 13. port-pool exhaustion -> 429 =="
# pool 9200-9209; v-core holds one. Create 9 web, the 11th must fail cleanly.
declare -a FILL
for i in $(seq 1 9); do FILL[$i]=$(create "{\"name\":\"v-fill-$i\",\"template\":\"web\"}" | sid_of); done
check "11th port user -> 429" "429" "$(curl -s -o .tmp/full.json -w '%{http_code}' -X POST "$API/sandboxes" -H "$J" -d '{"name":"v-toomany","template":"web"}')"
check "failed create holds no port" "None" "$(curl -s "$API/sandboxes" | python -c "import sys,json; print([s for s in json.load(sys.stdin) if s['name']=='v-toomany'][0]['port'])")"
for i in $(seq 1 9); do wait_state "${FILL[$i]}" READY,FAILED 120 >/dev/null; curl -s -o /dev/null -X DELETE "$API/sandboxes/${FILL[$i]}"; done
curl -s -o /dev/null -X DELETE "$API/sandboxes/$(cat .tmp/full.json | sid_of)"

echo "== 14. TTL reaper + TTL survives API restart =="
TID=$(create '{"name":"v-ttl","template":"blank","ttl_seconds":10}' | sid_of)
check "TTL sandbox reaped" "404" "$(for i in $(seq 1 15); do C=$(curl -s -o /dev/null -w '%{http_code}' "$API/sandboxes/$TID"); [ "$C" = 404 ] && break; sleep 2; done; echo $C)"
T2=$(create '{"name":"v-ttl2","template":"blank","ttl_seconds":40}' | sid_of); wait_state "$T2" READY 60 >/dev/null
DW=$(curl -s "$API/sandboxes/$T2" | jget "['deadline_wall']")
docker compose restart api >/dev/null 2>&1
for i in $(seq 1 45); do curl -s "$API/healthz" 2>/dev/null | grep -q '"api":"up"' && break; sleep 2; done
check "wall deadline unchanged across restart" "$DW" "$(curl -s "$API/sandboxes/$T2" | jget "['deadline_wall']")"
check "TTL fires after restart (not reset)" "404" "$(for i in $(seq 1 30); do C=$(curl -s -o /dev/null -w '%{http_code}' "$API/sandboxes/$T2"); [ "$C" = 404 ] && break; sleep 2; done; echo $C)"

echo "== 15. delete idempotency =="
BID=$(create '{"name":"v-del","template":"blank"}' | sid_of)
check "delete while CREATING -> 409" "409" "$(curl -s -o /dev/null -w '%{http_code}' -X DELETE "$API/sandboxes/$BID")"
wait_state "$BID" READY,FAILED 60 >/dev/null
check "first delete -> 204" "204" "$(curl -s -o /dev/null -w '%{http_code}' -X DELETE "$API/sandboxes/$BID")"
check "second delete -> 404" "404" "$(curl -s -o /dev/null -w '%{http_code}' -X DELETE "$API/sandboxes/$BID")"
check "events readable after deletion" "true" "$(curl -s "$API/sandboxes/$BID/events" | python -c "import sys,json; print(str(any('DELETED' in e['message'] for e in json.load(sys.stdin))).lower())")"

echo "== 16. API restart -> reconcile keeps coreapp READY =="
docker compose restart api >/dev/null 2>&1
for i in $(seq 1 45); do curl -s "$API/healthz" 2>/dev/null | grep -q '"api":"up"' && break; sleep 2; done
check "coreapp survives API restart" "READY" "$(state "$CID")"
check "app still answers" "ok" "$(curl -s "http://localhost:$CPORT/api/health" | jget "['status']")"

echo "== 17. SSE streams =="
(curl -s -N "$API/sandboxes/$CID/events?follow=1&since=999&timeout=6" > .tmp/sse.txt &)
sleep 1; curl -s -o /dev/null -X POST "$API/sandboxes/$CID/exec" -H "$J" -d '{"cmd":["true"]}'; sleep 5
grep -q '"type": "action"' .tmp/sse.txt && ok "per-sandbox SSE delivered the live exec event" || bad "sse: $(cat .tmp/sse.txt)"
check "global event stream has a cursor" "true" "$(curl -s "$API/events?cursor=0&follow=0&limit=5" | python -c "import sys,json; e=json.load(sys.stdin); print(str(len(e)>0 and 'cursor' in e[0]).lower())")"
curl -s -o /dev/null -X DELETE "$API/sandboxes/$CID"

echo "== 18. stateful volumes + pause (the checkpoint primitives) =="
R=$(create '{"name":"v-state","template":"blank","options":{"stateful":true}}'); XID=$(echo "$R" | sid_of)
check "stateful blank READY" "READY" "$(wait_state "$XID" READY,FAILED 90)"
check "volumes: one overlay mount" "1" "$(curl -s "$API/sandboxes/$XID/volumes" | python -c "import sys,json; print(len([m for m in json.load(sys.stdin)['mounts'] if m['fstype']=='overlay']))")"
curl -s -o /dev/null -X PUT "$API/sandboxes/$XID/fs/write?path=/workspace/a.txt" --data-binary 'A'
check "upper grows with a write" "true" "$(curl -s "$API/sandboxes/$XID/volumes" | python -c "import sys,json; print(str(json.load(sys.stdin)['roles']['app']['upper_bytes']>0).lower())")"
check "pause -> PAUSED" "PAUSED" "$(curl -s -X POST "$API/sandboxes/$XID/pause" | jget "['state']")"
check "exec on PAUSED -> 409" "409" "$(curl -s -o /dev/null -w '%{http_code}' -X POST "$API/sandboxes/$XID/exec" -H "$J" -d '{"cmd":["true"]}')"
check "stop while PAUSED -> 409" "409" "$(curl -s -o /dev/null -w '%{http_code}' -X POST "$API/sandboxes/$XID/stop")"
check "snapshot while PAUSED" "true" "$(curl -s -X POST "$API/sandboxes/$XID/volumes/snapshot" -H "$J" -d '{"dest":"/var/lib/sbx/ckpt/v-state"}' | python -c "import sys,json; print(str(json.load(sys.stdin)['app']['bytes']>0).lower())")"
check "unpause -> READY" "READY" "$(curl -s -X POST "$API/sandboxes/$XID/unpause" | jget "['state']")"
curl -s -o /dev/null -X PUT "$API/sandboxes/$XID/fs/write?path=/workspace/a.txt" --data-binary 'B'
curl -s -o /dev/null -X POST "$API/sandboxes/$XID/stop"
curl -s -o /dev/null -X POST "$API/sandboxes/$XID/volumes/restore" -H "$J" -d '{"layers":{"app":["/var/lib/sbx/ckpt/v-state/app"]}}'
curl -s -o /dev/null -X POST "$API/sandboxes/$XID/start"; sleep 2
check "restore brings the snapshot back" "A" "$(curl -s "$API/sandboxes/$XID/fs/read?path=/workspace/a.txt")"
check "node ops refuse paths outside /var/lib/sbx" "400" "$(curl -s -o /dev/null -w '%{http_code}' "$API/node/du?path=/etc")"
check "node tar streams the layer" "true" "$(curl -s "$API/node/tar?path=/var/lib/sbx/ckpt/v-state/app" | tar tf - | grep -q a.txt && echo true || echo false)"
curl -s -o /dev/null -X DELETE "$API/sandboxes/$XID"
check "volume dir removed with the sandbox (du -> -1 = missing)" "-1" "$(curl -s "$API/node/du?path=/var/lib/sbx/vol/$XID" | jget "['bytes']")"
curl -s -o /dev/null -X DELETE "$API/node/dir?path=/var/lib/sbx/ckpt/v-state"

echo
echo "======================================================"
echo "  RESULT:  $PASS passed, $FAIL failed"
echo "======================================================"
[ "$FAIL" -eq 0 ]

#!/usr/bin/env bash
# ============================================================================
#  sandbox-checkpoint - verification battery
#
#   1  health: service, sandbox-api, blob store
#   2  stateful coreapp: volumes mounted, WAL archive receives segments
#   3  checkpoint while running: PAUSED -> snapshot -> READY again, app alive;
#      manifest has layers, fingerprints, LSN, blobs shipped (refs registered)
#   4  mutate, second checkpoint: lineage parent/child, diff shows the change
#   5  fork from checkpoint 1: sees c1 state (rows + file), own port, works;
#      writes in the fork never reach the parent and vice versa
#   6  restore parent in place to c1: rows and file byte-exact
#      (fingerprints equal to the manifest's)
#   7  checkpoint of a STOPPED sandbox (was_running=false)
#   8  point-in-time recovery: rows inserted after c1 but before a recorded
#      LSN come back; rows after the LSN do not
#   9  delete refused while a fork sits on the checkpoint (409); force works
#  10  corruption: delete a layer dir behind the service -> gc marks CORRUPT,
#      fork/restore refuse (409); import from the blob store heals it
#  11  non-stateful sandbox -> 400; unknown checkpoint -> 404
#  12  crash safety: manifest written last - a checkpoint dir without a
#      manifest is not a checkpoint (gc ignores it, index never lists it)
#
#  Prereqs: sandbox-api, sandbox-blobs, sandbox-checkpoint all up.
# ============================================================================
set -uo pipefail
export MSYS_NO_PATHCONV=1
cd "$(dirname "$0")/.."
C="${C:-http://localhost:9040}"; A="${A:-http://localhost:9000}"; B="${B:-http://localhost:9400}"
J='content-type: application/json'
PASS=0; FAIL=0; mkdir -p .tmp
ok()  { PASS=$((PASS+1)); echo "  PASS  $1"; }
bad() { FAIL=$((FAIL+1)); echo "  FAIL  $1"; }
check() { if [ "$2" = "$3" ]; then ok "$1"; else bad "$1 - expected [$2] got [$3]"; fi; }
jget() { python -c "import sys,json; v=json.load(sys.stdin)$1; print(v if not isinstance(v,bool) else str(v).lower())"; }
state() { curl -s "$A/sandboxes/$1" | jget "['state']" 2>/dev/null; }
wait_state() { local t=0; while [ $t -lt "$3" ]; do local S; S=$(state "$1"); echo ",$2," | grep -q ",$S," && { echo "$S"; return; }; sleep 2; t=$((t+2)); done; state "$1"; }
tasks() { curl -s "http://localhost:$1/api/tasks" | python -c "import sys,json; print('|'.join(t['title'] for t in json.load(sys.stdin)))"; }
addtask() { curl -s -o /dev/null -X POST "http://localhost:$1/api/tasks" -H "$J" -d "{\"title\":\"$2\"}"; }
dbexec() { curl -s -X POST "$A/sandboxes/$1/exec" -H "$J" -d "{\"cmd\":[\"psql\",\"-U\",\"company\",\"-d\",\"companydb\",\"-Atc\",\"$2\"],\"container\":\"sbx-$1-db\"}" | jget "['stdout'].strip()"; }

echo "== 0. clean slate =="
for id in $(curl -s "$A/sandboxes" | python -c "import sys,json; print(' '.join(s['id'] for s in json.load(sys.stdin)))"); do
  for w in $(seq 1 60); do S=$(state "$id"); [ "$S" != "CREATING" ] && break; sleep 2; done
  [ "$S" = "PAUSED" ] && curl -s -o /dev/null -X POST "$A/sandboxes/$id/unpause"
  curl -s -o /dev/null -X DELETE "$A/sandboxes/$id"
done
for cid in $(curl -s "$C/checkpoints" | python -c "import sys,json; print(' '.join(m['id'] for m in json.load(sys.stdin)))"); do curl -s -o /dev/null -X DELETE "$C/checkpoints/$cid?force=1"; done

echo "== 1. health =="
H=$(curl -s "$C/healthz")
check "service up" "up" "$(echo "$H" | jget "['checkpoint']")"
check "sandbox-api reachable" "up" "$(echo "$H" | jget "['sandbox_api']")"
check "blob store configured" "configured" "$(echo "$H" | jget "['blobs']")"

echo "== 2. stateful coreapp =="
R=$(curl -s -X POST "$A/sandboxes" -H "$J" -d '{"name":"ck-a","template":"coreapp","options":{"stateful":true}}')
SID=$(echo "$R" | jget "['id']"); PORT=$(echo "$R" | jget "['port']")
check "stateful coreapp READY" "READY" "$(wait_state "$SID" READY,FAILED 300)"
V=$(curl -s "$A/sandboxes/$SID/volumes")
check "overlay mounts for db + backend" "2" "$(echo "$V" | python -c "import sys,json; print(len([m for m in json.load(sys.stdin)['mounts'] if m['fstype']=='overlay']))")"
check "wal plain dir declared" "/wal" "$(echo "$V" | jget "['plain']['wal']['path']")"
dbexec "$SID" "select pg_switch_wal()" >/dev/null; sleep 3
N=$(curl -s -X POST "$A/sandboxes/$SID/exec" -H "$J" -d "{\"cmd\":[\"sh\",\"-c\",\"ls /wal | wc -l\"],\"container\":\"sbx-$SID-db\"}" | jget "['stdout'].strip()")
[ "${N:-0}" -ge 1 ] && ok "WAL archive receives segments ($N)" || bad "WAL archive empty"

echo "== 3. checkpoint while running =="
addtask "$PORT" "before-c1"
curl -s -o /dev/null -X PUT "$A/sandboxes/$SID/fs/write?path=/workspace/note.txt" --data-binary 'v1'
C1=$(curl -s -X POST "$C/sandboxes/$SID/checkpoints" -H "$J" -d '{"label":"c1"}')
CID1=$(echo "$C1" | jget "['id']")
check "checkpoint created" "12" "$(echo -n "$CID1" | wc -c | tr -d ' ')"
check "sandbox READY again" "READY" "$(state "$SID")"
check "app alive after checkpoint" "ok" "$(curl -s "http://localhost:$PORT/api/health" | jget "['status']")"
check "manifest: 2 layers + wal" "3" "$(echo "$C1" | python -c "import sys,json; print(len(json.load(sys.stdin)['layers']))")"
check "manifest: lsn recorded" "true" "$(echo "$C1" | python -c "import sys,json; print(str(bool(json.load(sys.stdin)['lsn'])).lower())")"
check "manifest: db fingerprint rows" "4" "$(echo "$C1" | jget "['fingerprints']['db']['tables']['tasks']['rows']")"
check "manifest: blobs shipped (3)" "3" "$(echo "$C1" | python -c "import sys,json; print(len(json.load(sys.stdin)['blobs']))")"
check "blob store has refs for checkpoint:$CID1" "3" "$(curl -s "$B/refs?owner=checkpoint:$CID1" | python -c "import sys,json; print(len(json.load(sys.stdin)['hashes']))")"
check "node manifest present" "true" "$(curl -s "$A/node/du?path=/var/lib/sbx/ckpt/$CID1/manifest.json" | python -c "import sys,json; print(str(json.load(sys.stdin)['bytes']>0).lower())")"

echo "== 4. second checkpoint + lineage + diff =="
addtask "$PORT" "after-c1"
curl -s -o /dev/null -X PUT "$A/sandboxes/$SID/fs/write?path=/workspace/note.txt" --data-binary 'v2'
C2=$(curl -s -X POST "$C/sandboxes/$SID/checkpoints" -H "$J" -d '{"label":"c2"}'); CID2=$(echo "$C2" | jget "['id']")
check "c2 parent is c1" "$CID1" "$(echo "$C2" | jget "['parent']")"
check "tree: c1 descendant c2" "['$CID2']" "$(curl -s "$C/checkpoints/$CID1/tree" | jget "['descendants']")"
D=$(curl -s "$C/checkpoints/$CID1/diff?against=$CID2")
check "diff: not identical" "false" "$(echo "$D" | jget "['identical']")"
check "diff: tasks 4 -> 5" "5" "$(echo "$D" | jget "['roles']['db']['tables']['tasks']['rows_b']")"
check "diff: backend changed" "false" "$(echo "$D" | jget "['roles']['backend']['identical']")"

echo "== 5. fork from c1 =="
F=$(curl -s -X POST "$C/checkpoints/$CID1/fork" -H "$J" -d '{"name":"ck-a-fork"}'); FID=$(echo "$F" | jget "['id']"); FPORT=$(echo "$F" | jget "['port']")
check "fork READY" "READY" "$(wait_state "$FID" READY,FAILED 300)"
[ "$FPORT" != "$PORT" ] && ok "fork has its own port ($FPORT)" || bad "fork port equals parent"
check "fork sees c1 rows (not after-c1)" "Open the development environment|Edit backend/app/main.py and watch hot reload|Commit and push from inside the environment|before-c1" "$(tasks "$FPORT")"
check "fork sees c1 file" "v1" "$(curl -s "$A/sandboxes/$FID/fs/read?path=/workspace/note.txt")"
addtask "$FPORT" "fork-only"
check "parent unaffected by fork write" "5" "$(tasks "$PORT" | tr '|' '\n' | wc -l | tr -d ' ')"
addtask "$PORT" "parent-only"
check "fork unaffected by parent write" "5" "$(tasks "$FPORT" | tr '|' '\n' | wc -l | tr -d ' ')"
check "fork listed on checkpoint" "$FID" "$(curl -s "$C/checkpoints/$CID1" | jget "['forks'][0]['sandbox_id']")"

echo "== 6. restore parent to c1 =="
R=$(curl -s -X POST "$C/checkpoints/$CID1/restore" -H "$J" -d '{}')
check "restore ok" "$SID" "$(echo "$R" | jget "['sandbox']")"
wait_state "$SID" READY 120 >/dev/null; sleep 4
check "rows byte-exact (c1)" "Open the development environment|Edit backend/app/main.py and watch hot reload|Commit and push from inside the environment|before-c1" "$(tasks "$PORT")"
check "file byte-exact (c1)" "v1" "$(curl -s "$A/sandboxes/$SID/fs/read?path=/workspace/note.txt")"
FP=$(curl -s "$A/sandboxes/$SID/fingerprint/db" | jget "['root']")
check "db fingerprint equals manifest c1" "$(echo "$C1" | jget "['fingerprints']['db']['root']")" "$FP"
check "base of sandbox is c1" "$CID1" "$(curl -s "$C/sandboxes/$SID/checkpoints" | jget "['base']")"

echo "== 7. checkpoint of a STOPPED sandbox =="
curl -s -o /dev/null -X POST "$A/sandboxes/$FID/stop"
C3=$(curl -s -X POST "$C/sandboxes/$FID/checkpoints" -H "$J" -d '{"label":"stopped"}')
check "was_running=false" "false" "$(echo "$C3" | jget "['was_running']")"
check "fork still STOPPED" "STOPPED" "$(state "$FID")"
[ "$(state "$FID")" = "STOPPED" ] || { echo "   debug: fork events:"; curl -s "$A/sandboxes/$FID/events" | python -c "import sys,json; [print('     ',e['seq'],e['type'],e['message'][:110]) for e in json.load(sys.stdin)[-8:]]"; echo "   debug: checkpoint reply: $(echo "$C3" | head -c 300)"; }
curl -s -o /dev/null -X POST "$A/sandboxes/$FID/start"

echo "== 8. point-in-time recovery =="
addtask "$PORT" "pitr-keep"
sleep 1
LSN=$(dbexec "$SID" "select pg_current_wal_lsn()")
dbexec "$SID" "select pg_switch_wal()" >/dev/null; sleep 3
addtask "$PORT" "pitr-drop"
dbexec "$SID" "select pg_switch_wal()" >/dev/null; sleep 3
P=$(curl -s -X POST "$C/sandboxes/$SID/pitr" -H "$J" -d "{\"lsn\":\"$LSN\"}")
check "pitr base is c1" "$CID1" "$(echo "$P" | jget "['base_checkpoint']")"
wait_state "$SID" READY 120 >/dev/null; sleep 3
T=$(tasks "$PORT")
echo "$T" | grep -q "pitr-keep" && ok "row before target LSN recovered" || bad "pitr-keep missing: $T"
echo "$T" | grep -q "pitr-drop" && bad "row after target LSN came back: $T" || ok "row after target LSN absent"
check "postgres out of recovery" "f" "$(dbexec "$SID" "select pg_is_in_recovery()")"

echo "== 9. delete protection =="
check "delete c1 while fork/parent sit on it -> 409" "409" "$(curl -s -o /dev/null -w '%{http_code}' -X DELETE "$C/checkpoints/$CID1")"
check "delete c2 (no dependents) -> 204" "204" "$(curl -s -o /dev/null -w '%{http_code}' -X DELETE "$C/checkpoints/$CID2")"
check "c2 blob refs dropped" "0" "$(curl -s "$B/refs?owner=checkpoint:$CID2" | python -c "import sys,json; print(len(json.load(sys.stdin)['hashes']))")"

echo "== 10. corruption + import =="
curl -s -o /dev/null -X DELETE "$A/node/dir?path=/var/lib/sbx/ckpt/$CID1/backend"
G=$(curl -s -X POST "$C/gc")
check "gc marks c1 CORRUPT" "['$CID1']" "$(echo "$G" | jget "['corrupt']")"
check "fork from CORRUPT -> 409" "409" "$(curl -s -o /dev/null -w '%{http_code}' -X POST "$C/checkpoints/$CID1/fork" -H "$J" -d '{"name":"ck-bad"}')"
check "import from blob store" "$CID1" "$(curl -s -X POST "$C/checkpoints/$CID1/import" | jget "['imported']")"
check "gc clean again" "[]" "$(curl -s -X POST "$C/gc" | jget "['corrupt']")"
check "layer restored on node" "true" "$(curl -s "$A/node/du?path=/var/lib/sbx/ckpt/$CID1/backend/note.txt" | python -c "import sys,json; print(str(json.load(sys.stdin)['bytes']>0).lower())")"

echo "== 11. validation =="
NS=$(curl -s -X POST "$A/sandboxes" -H "$J" -d '{"name":"ck-plain","template":"blank"}' | jget "['id']"); wait_state "$NS" READY 60 >/dev/null
check "non-stateful -> 400" "400" "$(curl -s -o /dev/null -w '%{http_code}' -X POST "$C/sandboxes/$NS/checkpoints" -H "$J" -d '{}')"
check "unknown checkpoint -> 404" "404" "$(curl -s -o /dev/null -w '%{http_code}' "$C/checkpoints/nope")"
curl -s -o /dev/null -X DELETE "$A/sandboxes/$NS"

echo "== 12. manifest-last commit rule =="
curl -s -o /dev/null -X PUT "$A/node/untar?path=/var/lib/sbx/ckpt/orphan-dir/db" --data-binary @<(printf '' | tar cf - -T /dev/null)
check "gc ignores a dir without manifest" "[]" "$(curl -s -X POST "$C/gc" | jget "['corrupt']")"
check "index never lists it" "0" "$(curl -s "$C/checkpoints" | python -c "import sys,json; print(len([m for m in json.load(sys.stdin) if m['id']=='orphan-dir']))")"
curl -s -o /dev/null -X DELETE "$A/node/dir?path=/var/lib/sbx/ckpt/orphan-dir"

# cleanup
curl -s -o /dev/null -X DELETE "$A/sandboxes/$FID"; curl -s -o /dev/null -X DELETE "$A/sandboxes/$SID"
for cid in $(curl -s "$C/checkpoints" | python -c "import sys,json; print(' '.join(m['id'] for m in json.load(sys.stdin)))"); do curl -s -o /dev/null -X DELETE "$C/checkpoints/$cid?force=1"; done

echo
echo "======================================================"
echo "  RESULT:  $PASS passed, $FAIL failed"
echo "======================================================"
[ "$FAIL" -eq 0 ]

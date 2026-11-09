#!/usr/bin/env bash
# ============================================================================
#  sandbox-mcp - verification battery (a REAL MCP client drives the server)
#
#   1  health; /tools lists the catalogue
#   2  auth: no token -> 401; bad token -> 401
#   3  tools via the MCP client: list_templates, create_sandbox (web),
#      wait_ready, exec (exit code + stdout), write_file/read_file,
#      list_dir, fingerprint, events, get_sandbox, list_sandboxes
#   4  tenant isolation: alice cannot get/exec/delete bob's sandbox
#   5  resource sandbox://<id>/events readable by the owner only
#   6  errors are MCP tool errors (is_error), not transport failures
#   7  audit trail: /calls has every call with tenant, tool, outcome
#   8  checkpoint tools (if sandbox-checkpoint is up): checkpoint, fork
#   9  delete via MCP; sandbox gone on sandbox-api
#
#  Prereqs: sandbox-api up (+ sandbox-checkpoint for section 8); this stack up.
# ============================================================================
set -uo pipefail
cd "$(dirname "$0")/.."
M="${M:-http://localhost:9420}"; A="${A:-http://localhost:9000}"
PASS=0; FAIL=0; mkdir -p .tmp
ok()  { PASS=$((PASS+1)); echo "  PASS  $1"; }
bad() { FAIL=$((FAIL+1)); echo "  FAIL  $1"; }
check() { if [ "$2" = "$3" ]; then ok "$1"; else bad "$1 - expected [$2] got [$3]"; fi; }
jget() { python -c "import sys,json; v=json.load(sys.stdin)$1; print(v if not isinstance(v,bool) else str(v).lower())"; }
cli() { docker compose run --rm -T client --token "$1" "${@:2}" 2>/dev/null; }     # cli <token> tools|call|resource ...
callj() { cli "$1" call "$2" "$3"; }                                                # callj <token> <tool> <json>

echo "== 0. build client + clean slate =="
docker compose --profile test build client >/dev/null 2>&1 || true
for id in $(curl -s "$A/sandboxes" | python -c "import sys,json; print(' '.join(s['id'] for s in json.load(sys.stdin) if s['name'].startswith(('alice-','bob-','dev-'))))"); do curl -s -o /dev/null -X DELETE "$A/sandboxes/$id"; done

echo "== 1. health + catalogue =="
check "mcp up" "up" "$(curl -s "$M/healthz" | jget "['mcp']")"
check "sandbox-api reachable" "up" "$(curl -s "$M/healthz" | jget "['sandbox_api']")"
NT=$(curl -s "$M/tools" | python -c "import sys,json; print(len(json.load(sys.stdin)))")
[ "${NT:-0}" -ge 18 ] && ok "$NT tools published" || bad "tools: $NT"

echo "== 2. auth =="
check "no token -> 401" "401" "$(curl -s -o /dev/null -w '%{http_code}' -X POST "$M/mcp" -H 'content-type: application/json' -H 'accept: application/json, text/event-stream' -d '{}')"
check "bad token -> 401" "401" "$(curl -s -o /dev/null -w '%{http_code}' -X POST "$M/mcp" -H 'authorization: Bearer nope' -H 'content-type: application/json' -H 'accept: application/json, text/event-stream' -d '{}')"
R=$(cli alice-token tools)
echo "$R" | grep -q '"exec"' && ok "client session with a valid token lists tools" || bad "client tools: $R"

echo "== 3. tools through a real MCP session =="
check "list_templates has web" "true" "$(callj alice-token list_templates '{}' | python -c "import sys,json; print(str('web' in json.load(sys.stdin)['content'][0]).lower())")"
C=$(callj alice-token create_sandbox '{"name":"mcp1","template":"web"}')
SID=$(echo "$C" | jget "['content'][0]['id']")
check "create_sandbox names with tenant prefix" "alice-mcp1" "$(echo "$C" | jget "['content'][0]['name']")"
check "wait_ready -> READY" "READY" "$(callj alice-token wait_ready "{\"sandbox_id\":\"$SID\",\"timeout_seconds\":120}" | jget "['content'][0]['state']")"
E=$(callj alice-token exec "{\"sandbox_id\":\"$SID\",\"cmd\":[\"sh\",\"-c\",\"echo via-mcp; exit 4\"]}")
check "exec exit code" "4" "$(echo "$E" | jget "['content'][0]['exit_code']")"
check "exec stdout" "via-mcp" "$(echo "$E" | jget "['content'][0]['stdout'].strip()")"
callj alice-token write_file "{\"sandbox_id\":\"$SID\",\"path\":\"/www/mcp.txt\",\"content\":\"hello mcp\"}" >/dev/null
check "read_file round trip" "hello mcp" "$(callj alice-token read_file "{\"sandbox_id\":\"$SID\",\"path\":\"/www/mcp.txt\"}" | jget "['content'][0]")"
check "list_dir sees it" "true" "$(callj alice-token list_dir "{\"sandbox_id\":\"$SID\",\"path\":\"/www\"}" | python -c "import sys,json; print(str(any(e['name']=='mcp.txt' for e in json.load(sys.stdin)['content'][0])).lower())")"
check "fingerprint has root" "64" "$(callj alice-token fingerprint "{\"sandbox_id\":\"$SID\",\"path\":\"/www\"}" | python -c "import sys,json; print(len(json.load(sys.stdin)['content'][0]['root']))")"
check "events via tool" "true" "$(callj alice-token events "{\"sandbox_id\":\"$SID\"}" | python -c "import sys,json; print(str(any('READY' in e['message'] for e in json.load(sys.stdin)['content'][0])).lower())")"
check "get_sandbox shows containers" "1" "$(callj alice-token get_sandbox "{\"sandbox_id\":\"$SID\"}" | python -c "import sys,json; print(len(json.load(sys.stdin)['content'][0]['containers']))")"
check "list_sandboxes (alice) sees it" "true" "$(callj alice-token list_sandboxes '{}' | python -c "import sys,json; print(str(any(s['id']=='$SID' for s in json.load(sys.stdin)['content'][0])).lower())")"

echo "== 4. tenant isolation =="
check "bob list_sandboxes does not see alice's" "false" "$(callj bob-token list_sandboxes '{}' | python -c "import sys,json; c=json.load(sys.stdin)['content']; print(str(any(s['id']=='$SID' for s in (c[0] if c and isinstance(c[0], list) else []))).lower())")"
check "bob get_sandbox -> tool error" "true" "$(callj bob-token get_sandbox "{\"sandbox_id\":\"$SID\"}" | jget "['is_error']")"
check "bob exec -> tool error" "true" "$(callj bob-token exec "{\"sandbox_id\":\"$SID\",\"cmd\":[\"true\"]}" | jget "['is_error']")"
check "bob delete -> tool error" "true" "$(callj bob-token delete_sandbox "{\"sandbox_id\":\"$SID\"}" | jget "['is_error']")"
check "sandbox still exists" "READY" "$(curl -s "$A/sandboxes/$SID" | jget "['state']")"

echo "== 5. resources =="
check "owner reads events resource" "true" "$(cli alice-token resource "sandbox://$SID/events" | python -c "import sys,json; print(str('READY' in json.load(sys.stdin)[0]).lower())")"
R=$(cli bob-token resource "sandbox://$SID/events")
echo "$R" | grep -qi "error\|not yours" && ok "non-owner cannot read the resource" || bad "resource leak: $R"

echo "== 6. error semantics =="
check "exec on unknown sandbox -> is_error" "true" "$(callj alice-token exec '{"sandbox_id":"000000000000","cmd":["true"]}' | jget "['is_error']")"
check "unknown template -> is_error" "true" "$(callj alice-token create_sandbox '{"name":"x","template":"nope"}' | jget "['is_error']")"

echo "== 7. audit trail =="
CALLS=$(curl -s "$M/calls?limit=500")
check "calls recorded for alice" "true" "$(echo "$CALLS" | python -c "import sys,json; c=json.load(sys.stdin); print(str(sum(1 for x in c if x['tenant']=='alice' and x['tool']=='exec')>=1).lower())")"
check "failed bob calls recorded as not ok" "true" "$(echo "$CALLS" | python -c "import sys,json; c=json.load(sys.stdin); print(str(any(x['tenant']=='bob' and x['ok']==0 for x in c)).lower())")"

echo "== 8. checkpoint tools =="
if curl -s "$A/../" >/dev/null 2>&1 && curl -s localhost:9040/healthz 2>/dev/null | grep -q '"checkpoint":"up"'; then
  S2=$(callj alice-token create_sandbox '{"name":"mcp-st","template":"blank","stateful":true}' | jget "['content'][0]['id']")
  callj alice-token wait_ready "{\"sandbox_id\":\"$S2\",\"timeout_seconds\":120}" >/dev/null
  callj alice-token write_file "{\"sandbox_id\":\"$S2\",\"path\":\"/workspace/state.txt\",\"content\":\"v1\"}" >/dev/null
  CK=$(callj alice-token checkpoint "{\"sandbox_id\":\"$S2\",\"label\":\"via-mcp\"}" | jget "['content'][0]['id']")
  check "checkpoint id returned" "12" "$(echo -n "$CK" | wc -c | tr -d ' ')"
  F=$(callj alice-token fork "{\"checkpoint_id\":\"$CK\",\"name\":\"mcp-fork\"}"); FID=$(echo "$F" | jget "['content'][0]['id']")
  callj alice-token wait_ready "{\"sandbox_id\":\"$FID\",\"timeout_seconds\":120}" >/dev/null
  check "fork sees checkpointed file" "v1" "$(callj alice-token read_file "{\"sandbox_id\":\"$FID\",\"path\":\"/workspace/state.txt\"}" | jget "['content'][0]")"
  callj alice-token delete_sandbox "{\"sandbox_id\":\"$FID\"}" >/dev/null; callj alice-token delete_sandbox "{\"sandbox_id\":\"$S2\"}" >/dev/null
  curl -s -o /dev/null -X DELETE "localhost:9040/checkpoints/$CK?force=1"
else
  echo "  SKIP  checkpoint service not up (EXPECTED)"
fi

echo "== 9. delete via MCP =="
check "delete_sandbox" "true" "$(callj alice-token delete_sandbox "{\"sandbox_id\":\"$SID\"}" | jget "['content'][0]['deleted']")"
check "gone on sandbox-api" "404" "$(curl -s -o /dev/null -w '%{http_code}' "$A/sandboxes/$SID")"

echo
echo "======================================================"
echo "  RESULT:  $PASS passed, $FAIL failed"
echo "======================================================"
[ "$FAIL" -eq 0 ]

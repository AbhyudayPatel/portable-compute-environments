#!/usr/bin/env bash
# ============================================================================
#  sandbox-egress - verification battery
#
#   1  health (service, sandbox-api, blobs)
#   2  fixture: an `upstream` sandbox = the fake internet (upstream.test)
#   3  a coreapp with options.egress mode=deny joined to the upstream net:
#      proxy container present, fence chain installed, env injected
#   4  deny: HTTP request -> 403 from the proxy (X-Sandbox-Egress: deny)
#   5  direct bypass (ignore the proxy, dial the upstream IP) -> dropped by
#      iptables (times out); intra-sandbox traffic still fine (app health)
#   6  allow with allowlist: upstream.test passes, other.host blocked (403)
#   7  TLS interception: https://upstream.test verified against the
#      injected CA (issuer is the sandbox CA), body served
#   8  zero-secret: Authorization: Bearer PLACEHOLDER -> upstream /auth says
#      authorized (real token injected); /secret body comes back scrubbed
#      (placeholder, never the real value); placeholder is NOT injected for
#      a different host
#   9  record: 3 exchanges in the cassette, placeholders only (no real token)
#  10  ship cassette -> blob store (refs), second ship dedups (same hash)
#  11  replay: recorded body returned (counter frozen), unknown URL -> 599,
#      stats count replays and misses; nothing reached upstream
#  12  portable cassette: load into a NEW sandbox -> same recorded body
#  13  live policy change without restart (deny -> allow -> deny)
#  14  delete: fence chain removed from the node, proxy container gone
#
#  Prereqs: sandbox-api, sandbox-blobs, sandbox-egress up. First run builds
#  the mitmproxy-based proxy image and the upstream image inside dind.
# ============================================================================
set -uo pipefail
export MSYS_NO_PATHCONV=1
cd "$(dirname "$0")/.."
E="${E:-http://localhost:9410}"; A="${A:-http://localhost:9000}"; B="${B:-http://localhost:9400}"
J='content-type: application/json'
PASS=0; FAIL=0; mkdir -p .tmp
ok()  { PASS=$((PASS+1)); echo "  PASS  $1"; }
bad() { FAIL=$((FAIL+1)); echo "  FAIL  $1"; }
check() { if [ "$2" = "$3" ]; then ok "$1"; else bad "$1 - expected [$2] got [$3]"; fi; }
jget() { python -c "import sys,json; v=json.load(sys.stdin)$1; print(v if not isinstance(v,bool) else str(v).lower())"; }
state() { curl -s "$A/sandboxes/$1" | jget "['state']" 2>/dev/null; }
wait_state() { local t=0; while [ $t -lt "$3" ]; do local S; S=$(state "$1"); echo ",$2," | grep -q ",$S," && { echo "$S"; return; }; sleep 2; t=$((t+2)); done; state "$1"; }
DIND=$(docker ps --format '{{.Names}}' | grep -E '^sandbox-api[-_]dind' | head -1)
# python inside the coreapp backend: honours HTTP(S)_PROXY + SSL_CERT_FILE
PY='import sys,urllib.request,urllib.error
url=sys.argv[1]; hdr=dict(h.split("=",1) for h in sys.argv[2:])
req=urllib.request.Request(url, headers=hdr)
try:
    r=urllib.request.urlopen(req, timeout=8); print(r.status, r.headers.get("X-Sandbox-Egress",""), r.read().decode().strip())
except urllib.error.HTTPError as e: print(e.code, e.headers.get("X-Sandbox-Egress",""), e.read().decode().strip()[:80])
except Exception as e: print("ERR", type(e).__name__)'
fetch() { # fetch <sid> <url> [Header=value...]
  local sid=$1; shift
  python - "$@" <<PYEOF | curl -s -X POST "$A/sandboxes/$sid/exec" -H "$J" -d @- | jget "['stdout'].strip()"
import json,sys
print(json.dumps({"cmd":["python","-c",'''$PY'''] + sys.argv[1:], "timeout": 20, "container": "sbx-$sid-backend"}))
PYEOF
}
setpol() { curl -s -o /dev/null -X PUT "$A/sandboxes/$1/egress" -H "$J" -d "$2"; sleep 0.5; }

echo "== 0. clean slate =="
for id in $(curl -s "$A/sandboxes" | python -c "import sys,json; print(' '.join(s['id'] for s in json.load(sys.stdin)))"); do
  for w in $(seq 1 60); do S=$(state "$id"); [ "$S" != "CREATING" ] && break; sleep 2; done
  [ "$S" = "PAUSED" ] && curl -s -o /dev/null -X POST "$A/sandboxes/$id/unpause"
  curl -s -o /dev/null -X DELETE "$A/sandboxes/$id"
done
for c in $(curl -s "$E/cassettes" | python -c "import sys,json; print(' '.join(x['id'] for x in json.load(sys.stdin)))"); do curl -s -o /dev/null -X DELETE "$E/cassettes/$c"; done

echo "== 1. health =="
H=$(curl -s "$E/healthz")
check "service up" "up" "$(echo "$H" | jget "['egress']")"
check "sandbox-api up" "up" "$(echo "$H" | jget "['sandbox_api']")"
check "blobs up" "up" "$(echo "$H" | jget "['blobs']")"

echo "== 2. upstream fixture =="
UID_=$(curl -s -X POST "$A/sandboxes" -H "$J" -d '{"name":"eg-upstream","template":"upstream"}' | jget "['id']")
check "upstream READY" "READY" "$(wait_state "$UID_" READY,FAILED 300)"
UIP=$(curl -s "$A/sandboxes/$UID_/containers" | jget "[0]['ip']")
UNET="sbx-$UID_"
echo "   upstream ip $UIP on $UNET"

echo "== 3. coreapp with egress policy (deny) =="
R=$(curl -s -X POST "$A/sandboxes" -H "$J" -d "{\"name\":\"eg-app\",\"template\":\"coreapp\",\"options\":{\"egress\":{\"mode\":\"deny\",\"peer_networks\":[\"$UNET\"]}}}")
SID=$(echo "$R" | jget "['id']"); PORT=$(echo "$R" | jget "['port']")
check "coreapp with egress READY" "READY" "$(wait_state "$SID" READY,FAILED 400)"
[ "$(state "$SID")" = READY ] || { curl -s "$A/sandboxes/$SID/events" | python -c "import sys,json; [print('   ',e['seq'],e['type'],e['message'][:150]) for e in json.load(sys.stdin)]"; }
check "proxy container present" "true" "$(curl -s "$A/sandboxes/$SID/containers" | python -c "import sys,json; print(str(any(c['role']=='egress' and c['status']=='running' for c in json.load(sys.stdin))).lower())")"
EG=$(curl -s "$A/sandboxes/$SID/egress")
check "policy mode deny" "deny" "$(echo "$EG" | jget "['policy']['mode']")"
echo "$EG" | python -c "import sys,json; f=json.load(sys.stdin)['fence']; sys.exit(0 if any('-j DROP' in l for l in f) and any('DOCKER-USER' in l for l in f) else 1)" && ok "fence chain installed (DROP + DOCKER-USER hook)" || bad "fence: $EG"
check "proxy env injected into backend" "http://egress:8080" "$(curl -s -X POST "$A/sandboxes/$SID/exec" -H "$J" -d '{"cmd":["sh","-c","echo $HTTPS_PROXY"]}' | jget "['stdout'].strip()")"
check "CA cert injected" "true" "$(curl -s -X POST "$A/sandboxes/$SID/exec" -H "$J" -d '{"cmd":["sh","-c","head -1 /sbx/ca.pem"]}' | python -c "import sys,json; print(str('BEGIN CERTIFICATE' in json.load(sys.stdin)['stdout']).lower())")"

echo "== 4. deny =="
check "http -> 403 deny" "403 deny" "$(fetch "$SID" http://upstream.test/ | cut -d' ' -f1,2)"
check "app itself still healthy (intra-sandbox unaffected)" "ok" "$(curl -s "http://localhost:$PORT/api/health" | jget "['status']")"

echo "== 5. direct bypass is fenced =="
BYP=$(curl -s -X POST "$A/sandboxes/$SID/exec" -H "$J" -d "{\"cmd\":[\"python\",\"-c\",\"import urllib.request as u\\ntry:\\n  o=u.build_opener(u.ProxyHandler({})); o.open('http://$UIP/',timeout=4); print('REACHED')\\nexcept Exception as e: print('BLOCKED', type(e).__name__)\"],\"timeout\":20}" | jget "['stdout'].strip()")
echo "$BYP" | grep -q BLOCKED && ok "no-proxy dial to upstream ip dropped by iptables ($BYP)" || bad "bypass: $BYP"

echo "== 6. allowlist =="
setpol "$SID" '{"mode":"allow","allow":["upstream.test"]}'
R1=$(fetch "$SID" http://upstream.test/)
check "allowed host passes" "200 allow" "$(echo "$R1" | cut -d' ' -f1,2)"
echo "$R1" | grep -q "upstream-ok" && ok "live body from upstream ($R1)" || bad "body: $R1"
check "other host blocked" "403 blocked" "$(fetch "$SID" http://other.host/ | cut -d' ' -f1,2)"

echo "== 7. TLS interception =="
R2=$(fetch "$SID" https://upstream.test/)
check "https via proxy, verified against injected CA" "200 allow" "$(echo "$R2" | cut -d' ' -f1,2)"
ISS=$(curl -s -X POST "$A/sandboxes/$SID/exec" -H "$J" -d '{"cmd":["python","-c","import ssl,socket,os\nctx=ssl.create_default_context(cafile=\"/sbx/ca.pem\")\ns=socket.create_connection((\"egress\",8080)); s.sendall(b\"CONNECT upstream.test:443 HTTP/1.1\\r\\nHost: upstream.test\\r\\n\\r\\n\"); s.recv(4096)\nt=ctx.wrap_socket(s, server_hostname=\"upstream.test\"); print(dict(x[0] for x in t.getpeercert()[\"issuer\"])[\"commonName\"])"],"timeout":20}' | jget "['stdout'].strip()")
echo "$ISS" | grep -q "sandbox-egress" && ok "server cert issued by the sandbox CA ($ISS)" || bad "issuer: $ISS"

echo "== 8. zero-secret sandbox =="
setpol "$SID" '{"mode":"allow","allow":["upstream.test","other.test"],"secrets":{"PLACEHOLDER_TOKEN":{"host":"upstream.test","value":"real-token-XYZ"}}}'
check "placeholder injected for the right host (upstream /auth)" "200 allow authorized" "$(fetch "$SID" http://upstream.test/auth "Authorization=Bearer PLACEHOLDER_TOKEN")"
SEC=$(fetch "$SID" http://upstream.test/secret)
echo "$SEC" | grep -q "PLACEHOLDER_TOKEN" && ! echo "$SEC" | grep -q "real-token-XYZ" && ok "real value scrubbed from responses ($SEC)" || bad "scrub: $SEC"
check "sandbox never holds the real token (env + files)" "0" "$(curl -s -X POST "$A/sandboxes/$SID/exec" -H "$J" -d '{"cmd":["sh","-c","(env; cat /sbx/ca.pem) | grep -c real-token-XYZ || true"]}' | jget "['stdout'].strip()")"
check "no injection for another host (401 from upstream via other.test alias absent -> 403/ERR acceptable)" "true" "$(fetch "$SID" http://other.test/auth "Authorization=Bearer PLACEHOLDER_TOKEN" | python -c "import sys; s=sys.stdin.read(); print(str('200' not in s).lower())")"

echo "== 9. record =="
curl -s -o /dev/null -X DELETE "$A/sandboxes/$SID/egress/cassette"
setpol "$SID" '{"mode":"record","allow":["upstream.test"],"secrets":{"PLACEHOLDER_TOKEN":{"host":"upstream.test","value":"real-token-XYZ"}}}'
REC1=$(fetch "$SID" http://upstream.test/); fetch "$SID" http://upstream.test/headers "Authorization=Bearer PLACEHOLDER_TOKEN" >/dev/null; fetch "$SID" https://upstream.test/auth "Authorization=Bearer PLACEHOLDER_TOKEN" >/dev/null
CAS=$(curl -s "$A/sandboxes/$SID/egress/cassette")
check "cassette has 3 exchanges" "3" "$(echo "$CAS" | grep -c '"key"')"
echo "$CAS" | grep -q "real-token-XYZ" && bad "cassette leaks the real token" || ok "cassette holds placeholders only"
check "stats recorded=3" "3" "$(curl -s "$A/sandboxes/$SID/egress" | jget "['stats']['recorded']")"

echo "== 10. ship to blob store =="
S1=$(curl -s -X POST "$E/sandboxes/$SID/cassettes/ship" -H "$J" -d '{"label":"run1"}')
CID=$(echo "$S1" | jget "['id']"); HASH=$(echo "$S1" | jget "['hash']")
check "cassette shipped (3 exchanges)" "3" "$(echo "$S1" | jget "['exchanges']")"
check "blob referenced by cassette:$CID" "true" "$(curl -s "$B/refs?hash=$HASH" | python -c "import sys,json; print(str('cassette:$CID' in json.load(sys.stdin)['owners']).lower())")"
S2=$(curl -s -X POST "$E/sandboxes/$SID/cassettes/ship" -H "$J" -d '{"label":"run1-again"}')
check "second ship dedups to the same blob" "$HASH" "$(echo "$S2" | jget "['hash']")"

echo "== 11. replay =="
setpol "$SID" '{"mode":"replay","allow":[],"secrets":{}}'
R3=$(fetch "$SID" http://upstream.test/)
check "replayed body equals recorded body" "$(echo "$REC1" | cut -d' ' -f3-)" "$(echo "$R3" | cut -d' ' -f3-)"
check "replay header" "replay" "$(echo "$R3" | cut -d' ' -f2)"
check "unknown request -> 599" "599" "$(fetch "$SID" http://upstream.test/never-recorded | cut -d' ' -f1)"
ST=$(curl -s "$A/sandboxes/$SID/egress" | jget "['stats']")
echo "$ST" | grep -q "'replayed': 1" && echo "$ST" | grep -q "'replay_miss': 1" && ok "stats: 1 replay, 1 miss" || bad "stats: $ST"

echo "== 12. portable cassette into a new sandbox =="
R=$(curl -s -X POST "$A/sandboxes" -H "$J" -d "{\"name\":\"eg-app2\",\"template\":\"coreapp\",\"options\":{\"egress\":{\"mode\":\"deny\"}}}")
SID2=$(echo "$R" | jget "['id']")
check "second sandbox READY" "READY" "$(wait_state "$SID2" READY,FAILED 400)"
L=$(curl -s -X POST "$E/sandboxes/$SID2/cassettes/$CID/load")
check "cassette loaded, mode replay" "replay" "$(echo "$L" | jget "['mode']")"
check "new sandbox replays the recorded body (no upstream access at all)" "$(echo "$REC1" | cut -d' ' -f3-)" "$(fetch "$SID2" http://upstream.test/ | cut -d' ' -f3-)"

echo "== 13. live policy switches =="
setpol "$SID" '{"mode":"deny"}'
check "back to deny" "403" "$(fetch "$SID" http://upstream.test/ | cut -d' ' -f1)"
setpol "$SID" '{"mode":"allow","allow":["*.test"]}'
check "glob allow" "200" "$(fetch "$SID" http://upstream.test/ | cut -d' ' -f1)"

echo "== 14. delete removes the fence =="
curl -s -o /dev/null -X DELETE "$A/sandboxes/$SID"; sleep 2
check "fence chain gone from the node" "0" "$(docker exec "$DIND" sh -c "iptables -S DOCKER-USER | grep -c SBX_$SID || true")"
check "proxy container gone" "0" "$(docker exec "$DIND" docker ps -a --format '{{.Names}}' | grep -c "sbx-$SID-egress" || true)"
curl -s -o /dev/null -X DELETE "$A/sandboxes/$SID2"; curl -s -o /dev/null -X DELETE "$A/sandboxes/$UID_"
for c in $(curl -s "$E/cassettes" | python -c "import sys,json; print(' '.join(x['id'] for x in json.load(sys.stdin)))"); do curl -s -o /dev/null -X DELETE "$E/cassettes/$c"; done

echo
echo "======================================================"
echo "  RESULT:  $PASS passed, $FAIL failed"
echo "======================================================"
[ "$FAIL" -eq 0 ]

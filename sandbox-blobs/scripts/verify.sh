#!/usr/bin/env bash
# ============================================================================
#  sandbox-blobs - verification battery
#
#   1  health + empty stats
#   2  put: hash equals sha256 of the bytes; 201 then 200 existed (dedup)
#   3  get bytes back identical; HEAD size; ETag; Range request
#   4  meta / verify endpoint; bad hash 400; unknown 404
#   5  refs: add, list by hash and owner, drop owner; unknown blob 404
#   6  pins: set, list, delete
#   7  gc: grace keeps young blobs; grace=0 deletes unreferenced; referenced
#      and pinned survive; dry_run reports without deleting
#   8  delete endpoint: 409 while referenced, 204 after
#   9  streaming: a 300 MB upload under a 256 MB container memory limit;
#      memory stays flat; download hash matches
#  10  concurrency: 20 parallel uploads of distinct blobs, all present
#  11  crash recovery: remove a file behind the API, restart -> index drops it
#  12  413 beyond MAX_BLOB_BYTES (set small via env in a side container)
# ============================================================================
set -uo pipefail
export MSYS_NO_PATHCONV=1   # Git Bash: do not rewrite /blobs/... args of docker exec into Windows paths
cd "$(dirname "$0")/.."
API="${API:-http://localhost:9400}"
J='content-type: application/json'
PASS=0; FAIL=0; mkdir -p .tmp
ok()  { PASS=$((PASS+1)); echo "  PASS  $1"; }
bad() { FAIL=$((FAIL+1)); echo "  FAIL  $1"; }
check() { if [ "$2" = "$3" ]; then ok "$1"; else bad "$1 - expected [$2] got [$3]"; fi; }
jget() { python -c "import sys,json; v=json.load(sys.stdin)$1; print(v if not isinstance(v,bool) else str(v).lower())"; }
CT=$(docker ps --format '{{.Names}}' | grep -E '^sandbox-blobs[-_]blobs' | head -1)

echo "== 0. clean slate =="
docker compose down -v >/dev/null 2>&1; docker compose up -d --build >/dev/null 2>&1
for i in $(seq 1 30); do curl -s "$API/healthz" | grep -q '"up"' && break; sleep 2; done
CT=$(docker ps --format '{{.Names}}' | grep -E '^sandbox-blobs[-_]blobs' | head -1)

echo "== 1. health =="
check "healthz" "up" "$(curl -s "$API/healthz" | jget "['blobs']")"
check "empty store" "0" "$(curl -s "$API/stats" | jget "['blobs']")"

echo "== 2. put + dedup =="
printf 'hello blobs\n' > .tmp/a.txt
H=$(sha256sum .tmp/a.txt | cut -d' ' -f1)
R=$(curl -s -w '\n%{http_code}' -T .tmp/a.txt "$API/blobs")
check "put -> 201" "201" "$(echo "$R" | tail -1)"
check "hash == sha256" "$H" "$(echo "$R" | head -1 | jget "['hash']")"
check "size recorded" "12" "$(echo "$R" | head -1 | jget "['size']")"
R=$(curl -s -w '\n%{http_code}' -T .tmp/a.txt "$API/blobs")
check "same bytes -> 200 existed" "200" "$(echo "$R" | tail -1)"
check "existed flag" "true" "$(echo "$R" | head -1 | jget "['existed']")"
check "still one blob" "1" "$(curl -s "$API/stats" | jget "['blobs']")"

echo "== 3. get / head / range =="
check "get bytes identical" "hello blobs" "$(curl -s "$API/blobs/$H")"
check "HEAD content-length" "12" "$(curl -s -I "$API/blobs/$H" | tr -d '\r' | grep -i content-length | awk '{print $2}')"
check "etag is the hash" "\"$H\"" "$(curl -s -I "$API/blobs/$H" | tr -d '\r' | grep -i etag | awk '{print $2}')"
check "range 0-4" "hello" "$(curl -s -H 'Range: bytes=0-4' "$API/blobs/$H")"
check "range -> 206" "206" "$(curl -s -o /dev/null -w '%{http_code}' -H 'Range: bytes=6-10' "$API/blobs/$H")"

echo "== 4. meta / verify / errors =="
check "meta size" "12" "$(curl -s "$API/blobs/$H/meta" | jget "['size']")"
check "verify ok" "true" "$(curl -s "$API/blobs/$H/verify" | jget "['ok']")"
check "bad hash -> 400" "400" "$(curl -s -o /dev/null -w '%{http_code}' "$API/blobs/zzz")"
check "unknown -> 404" "404" "$(curl -s -o /dev/null -w '%{http_code}' "$API/blobs/$(printf 'a%.0s' $(seq 1 64))")"

echo "== 5. refs =="
check "add ref" "ckpt:1" "$(curl -s -X POST "$API/refs" -H "$J" -d "{\"owner\":\"ckpt:1\",\"hashes\":[\"$H\"]}" | jget "['owner']")"
check "refs by hash" "['ckpt:1']" "$(curl -s "$API/refs?hash=$H" | jget "['owners']")"
check "refs by owner" "['$H']" "$(curl -s "$API/refs?owner=ckpt:1" | jget "['hashes']")"
check "ref to unknown blob -> 404" "404" "$(curl -s -o /dev/null -w '%{http_code}' -X POST "$API/refs" -H "$J" -d "{\"owner\":\"x\",\"hashes\":[\"$(printf 'b%.0s' $(seq 1 64))\"]}")"
check "put with ?owner registers ref" "true" "$(printf 'second\n' | curl -s -T - "$API/blobs?owner=ckpt:2" | python -c "import sys,json; print(str(json.load(sys.stdin)['owner']=='ckpt:2').lower())")"
H2=$(printf 'second\n' | sha256sum | cut -d' ' -f1)
check "owner ckpt:2 has it" "['$H2']" "$(curl -s "$API/refs?owner=ckpt:2" | jget "['hashes']")"

echo "== 6. pins =="
printf 'pinned\n' > .tmp/p.txt; HP=$(sha256sum .tmp/p.txt | cut -d' ' -f1); curl -s -o /dev/null -T .tmp/p.txt "$API/blobs"
check "pin set" "$HP" "$(curl -s -X PUT "$API/pins/golden" -H "$J" -d "{\"hash\":\"$HP\"}" | jget "['hash']")"
check "pins listed" "$HP" "$(curl -s "$API/pins" | jget "['golden']")"

echo "== 7. gc =="
printf 'orphan\n' > .tmp/o.txt; HO=$(sha256sum .tmp/o.txt | cut -d' ' -f1); curl -s -o /dev/null -T .tmp/o.txt "$API/blobs"
G=$(curl -s -X POST "$API/gc?grace=3600")
check "young orphan kept by grace" "0" "$(echo "$G" | jget "['deleted']")"
check "kept_young counted" "1" "$(echo "$G" | jget "['kept_young']")"
G=$(curl -s -X POST "$API/gc?grace=0&dry_run=1")
check "dry run reports 1" "1" "$(echo "$G" | jget "['deleted']")"
check "dry run deleted nothing" "200" "$(curl -s -o /dev/null -w '%{http_code}' "$API/blobs/$HO")"
G=$(curl -s -X POST "$API/gc?grace=0")
check "gc grace=0 deletes the orphan" "1" "$(echo "$G" | jget "['deleted']")"
check "orphan gone" "404" "$(curl -s -o /dev/null -w '%{http_code}' "$API/blobs/$HO")"
check "referenced survived" "200" "$(curl -s -o /dev/null -w '%{http_code}' "$API/blobs/$H")"
check "pinned survived" "200" "$(curl -s -o /dev/null -w '%{http_code}' "$API/blobs/$HP")"

echo "== 8. delete endpoint =="
check "delete referenced -> 409" "409" "$(curl -s -o /dev/null -w '%{http_code}' -X DELETE "$API/blobs/$H")"
curl -s -o /dev/null -X DELETE "$API/refs/ckpt:1"
check "delete unreferenced -> 204" "204" "$(curl -s -o /dev/null -w '%{http_code}' -X DELETE "$API/blobs/$H")"
check "gone" "404" "$(curl -s -o /dev/null -w '%{http_code}' "$API/blobs/$H")"
curl -s -o /dev/null -X DELETE "$API/pins/golden"

echo "== 9. streaming 300 MB under a 256 MB memory limit =="
python -c "import os; f=open('.tmp/big.bin','wb'); [f.write(os.urandom(1<<20)) for _ in range(300)]; f.close()"
HB=$(sha256sum .tmp/big.bin | cut -d' ' -f1)
( for i in $(seq 1 12); do docker stats --no-stream --format '{{.MemUsage}}' "$CT" | cut -d/ -f1 >> .tmp/mem.txt; sleep 1; done ) &
R=$(curl -s -T .tmp/big.bin "$API/blobs?owner=big")
wait
check "big upload hash" "$HB" "$(echo "$R" | jget "['hash']")"
PEAK=$(python -c "
import re
vals=[]
for l in open('.tmp/mem.txt'):
    m=re.match(r'\s*([0-9.]+)([KMG])iB',l)
    if m: vals.append(float(m.group(1))*{'K':1/1024,'M':1,'G':1024}[m.group(2)])
print(int(max(vals)) if vals else -1)")
[ "$PEAK" -gt 0 ] && [ "$PEAK" -lt 200 ] && ok "service memory peak ${PEAK} MiB < 200 MiB while streaming 300 MB" || bad "memory peak ${PEAK} MiB"
curl -s "$API/blobs/$HB" -o .tmp/big.out
check "big download identical" "$HB" "$(sha256sum .tmp/big.out | cut -d' ' -f1)"
curl -s -o /dev/null -X DELETE "$API/refs/big"; curl -s -o /dev/null -X POST "$API/gc?grace=0"

echo "== 10. concurrent uploads =="
for i in $(seq 1 20); do printf "blob-$i-$RANDOM\n" > .tmp/c$i.txt; done
for i in $(seq 1 20); do curl -s -o /dev/null -T .tmp/c$i.txt "$API/blobs?owner=conc" & done; wait
check "20 concurrent uploads all present" "20" "$(curl -s "$API/refs?owner=conc" | python -c "import sys,json; print(len(json.load(sys.stdin)['hashes']))")"
ALLOK=1; for i in $(seq 1 20); do h=$(sha256sum .tmp/c$i.txt | cut -d' ' -f1); [ "$(curl -s "$API/blobs/$h/verify" | jget "['ok']")" = "true" ] || ALLOK=0; done
check "all 20 verify" "1" "$ALLOK"

echo "== 11. crash recovery =="
HC=$(sha256sum .tmp/c1.txt | cut -d' ' -f1)
docker exec "$CT" rm -f "/blobs/sha256/${HC:0:2}/$HC"
docker compose restart blobs >/dev/null 2>&1
for i in $(seq 1 30); do curl -s "$API/healthz" | grep -q '"up"' && break; sleep 2; done
check "index dropped the missing file" "404" "$(curl -s -o /dev/null -w '%{http_code}' "$API/blobs/$HC")"
check "its ref gone too" "19" "$(curl -s "$API/refs?owner=conc" | python -c "import sys,json; print(len(json.load(sys.stdin)['hashes']))")"

echo "== 12. size cap =="
docker run -d --rm --name blobs-tiny --network sandbox-blobs -e MAX_BLOB_BYTES=100 -e BLOB_ROOT=/tmp/b -e DB_PATH=/tmp/b/i.db sandbox-blobs:local >/dev/null
sleep 3
check "over cap -> 413" "413" "$(docker run --rm --network sandbox-blobs curlimages/curl:8.5.0 -s -o /dev/null -w '%{http_code}' -X PUT --data-binary "$(head -c 200 /dev/zero | tr '\0' x)" http://blobs-tiny:8000/blobs)"
docker rm -f blobs-tiny >/dev/null 2>&1

echo
echo "======================================================"
echo "  RESULT:  $PASS passed, $FAIL failed"
echo "======================================================"
[ "$FAIL" -eq 0 ]

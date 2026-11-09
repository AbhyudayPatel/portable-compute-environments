#!/usr/bin/env bash
# ============================================================================
#  sandbox-sim - verification
#
#   1. unit + property tests (pytest): 60 random seeds, 12 scenario tests,
#      4 mutation tests proving the harness catches injected bugs
#   2. a 300-seed x 300-step fuzz run with every fault enabled
#   3. determinism: the same seed twice yields a byte-identical log
#
#  Everything runs inside the sim image (no Docker engine is touched).
#  Usage: bash scripts/verify.sh
# ============================================================================
set -uo pipefail
cd "$(dirname "$0")/.."
PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); echo "  PASS  $1"; }
bad() { FAIL=$((FAIL+1)); echo "  FAIL  $1"; }

echo "== build =="
docker compose build >/dev/null 2>&1 && ok "sim image built" || { bad "build failed"; exit 1; }

echo "== 1. pytest (unit + property + mutation) =="
OUT=$(docker compose run --rm -T sim pytest -q 2>&1 | tail -3)
echo "$OUT" | grep -qE "^[0-9]+ passed" && ! echo "$OUT" | grep -q failed && ok "pytest: $(echo "$OUT" | grep -oE '[0-9]+ passed')" || bad "pytest: $OUT"

echo "== 2. fuzz: 300 seeds x 300 steps =="
OUT=$(docker compose run --rm -T sim python -m sim.run --seeds 300 --steps 300 --json 2>/dev/null | tail -1)
FAILS=$(echo "$OUT" | python -c "import sys,json; print(len(json.load(sys.stdin)['failures']))" 2>/dev/null || echo "?")
[ "$FAILS" = "0" ] && ok "0 failing seeds ($(echo "$OUT" | python -c "import sys,json; d=json.load(sys.stdin); print(f\"{d['created']} creates, {d['crashes']} crashes, {d['seconds']:.0f}s\")"))" || bad "fuzz failures: $OUT"

echo "== 3. determinism =="
A=$(docker compose run --rm -T sim python -m sim.run --seed 7 --steps 150 --verbose 2>/dev/null | grep '^\[t=' | md5sum)
B=$(docker compose run --rm -T sim python -m sim.run --seed 7 --steps 150 --verbose 2>/dev/null | grep '^\[t=' | md5sum)
[ "$A" = "$B" ] && ok "same seed -> identical log" || bad "non-deterministic replay"

echo
echo "======================================================"
echo "  RESULT:  $PASS passed, $FAIL failed"
echo "======================================================"
[ "$FAIL" -eq 0 ]

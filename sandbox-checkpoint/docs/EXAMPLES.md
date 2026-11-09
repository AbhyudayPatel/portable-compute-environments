# sandbox-checkpoint - Exercises

Prereqs: sandbox-api, sandbox-blobs, sandbox-checkpoint up. Shell helper:

```bash
j() { python -c "import sys,json; print(json.load(sys.stdin)$1)"; }
A=localhost:9000; C=localhost:9040; H='content-type: application/json'
```

## Level 0 - A stateful sandbox (3 min)

```bash
S=$(curl -s -X POST $A/sandboxes -H "$H" -d '{"name":"lab","template":"coreapp","options":{"stateful":true}}' | j "['id']")
# wait for READY, then:
curl -s $A/sandboxes/$S/volumes | j ""
```
**Observe:** two overlay mounts (db, backend) and a plain `wal` dir.
`upper_bytes` is what the sandbox has written so far.

## Level 1 - Checkpoint, break, restore (5 min)

```bash
P=$(curl -s $A/sandboxes/$S | j "['port']")
curl -s -X POST localhost:$P/api/tasks -H "$H" -d '{"title":"keep me"}'
C1=$(curl -s -X POST $C/sandboxes/$S/checkpoints -H "$H" -d '{"label":"good"}' | j "['id']")
curl -s -X POST localhost:$P/api/tasks -H "$H" -d '{"title":"mistake"}'
curl -s -X POST $C/checkpoints/$C1/restore -H "$H" -d '{}'
curl -s localhost:$P/api/tasks
```
**Observe:** "mistake" is gone, "keep me" is back; the sandbox kept its
port. Look at `GET $C/checkpoints/$C1`: layers with sizes, fingerprints,
LSN, blob hashes. **Teaches:** a checkpoint is a frozen copy of the
deltas plus a manifest.

## Level 2 - Branch per experiment (10 min)

```bash
F1=$(curl -s -X POST $C/checkpoints/$C1/fork -H "$H" -d '{"name":"try-a"}' | j "['id']")
F2=$(curl -s -X POST $C/checkpoints/$C1/fork -H "$H" -d '{"name":"try-b"}' | j "['id']")
```
Apply a different change in each (edit `/workspace/note.txt` via
`PUT $A/sandboxes/$F1/fs/write?path=/workspace/note.txt`), then compare:
`GET $A/diff?a=$F1&b=$F2&path=/workspace`. Decide, delete the loser.
**Teaches:** forks share bytes (both started in seconds) and diverge
privately; this is the primitive for an agent that explores N fixes.

## Level 3 - Lineage and diffs (5 min)

Make a second checkpoint of the original sandbox, then:
```bash
curl -s $C/checkpoints/$C1/tree | j ""
curl -s "$C/checkpoints/$C1/diff?against=$C2" | j ""
```
**Observe:** parent/child, forks listed, per-table row counts and which
roles changed - computed from the manifests, no layers touched.

## Level 4 - Time travel inside the database (10 min)

```bash
curl -s -X POST localhost:$P/api/tasks -H "$H" -d '{"title":"t1"}'
LSN=$(curl -s -X POST $A/sandboxes/$S/exec -H "$H" -d "{\"cmd\":[\"psql\",\"-U\",\"company\",\"-d\",\"companydb\",\"-Atc\",\"select pg_current_wal_lsn()\"],\"container\":\"sbx-$S-db\"}" | j "['stdout'].strip()")
curl -s -X POST localhost:$P/api/tasks -H "$H" -d '{"title":"t2"}'
curl -s -X POST $C/sandboxes/$S/pitr -H "$H" -d "{\"lsn\":\"$LSN\"}"
curl -s localhost:$P/api/tasks
```
**Observe:** `t1` present, `t2` gone, no checkpoint was taken between
them. **Teaches:** the WAL archive is a continuous record; checkpoints
are only the starting points for replay.

## Level 5 - Expert: lose the node, keep the checkpoint (10 min)

```bash
curl -s -X DELETE "$A/node/dir?path=/var/lib/sbx/ckpt/$C1/backend"
curl -s -X POST $C/gc            # c1 is CORRUPT
curl -s -X POST $C/checkpoints/$C1/fork -H "$H" -d '{"name":"x"}'   # 409
curl -s -X POST $C/checkpoints/$C1/import                             # from the blob store
curl -s -X POST $C/gc            # clean
```
**Teaches:** layers in the blob store are the durable copy; the node
directory is a cache. This is also the seed of migration between workers
(T12): import on another node.

# sandbox-checkpoint - Architecture

## Layers, from the kernel up

```
 node (dind container)                      sandbox container
 /var/lib/sbx  (ext4 volume `sbx-state`)    /var/lib/postgresql/data  <- bind of merged/
   vol/<sid>/db/
     base/      empty bottom                 overlayfs:
     upper/     the sandbox's writes            lowerdir = ckpt/c1/db : base   (fork/restore)
     work/                                      upperdir = upper
     merged/    what the container sees         workdir  = work
   vol/<sid>/wal/   plain dir, bind -> /wal   (postgres archives segments here)
   ckpt/<cid>/db/        immutable copy of an upper  (a layer)
   ckpt/<cid>/backend/   ...
   ckpt/<cid>/wal/       copy of the archive at checkpoint time
   ckpt/<cid>/manifest.json
```

Why overlayfs and not `docker commit`: commit snapshots the container's
rootfs, not its volumes, serialises everything through the image store,
and a "fork" would be a full image + a full volume copy. With overlay the
delta is explicit (`upper/`), a fork is a mount option, and the same layer
directory can sit under any number of sandboxes.

Why not stack checkpoint layers incrementally (c2 on top of c1)? Rotating a
live sandbox's upper into a new lower requires remounting the overlay,
which the running container's bind mount pins. So a checkpoint copies the
**whole** upper (the delta since the sandbox's base), making every
checkpoint self-contained: `fork(c2)` mounts `ckpt/c2/db` alone. Cost: a
checkpoint's size is the full delta, not the increment since the previous
checkpoint. The blob store deduplicates identical layer tars across
checkpoints; a true incremental scheme would stop the container for the
rotation (documented as EXPECTED future work).

## The checkpoint protocol

1. `GET /sandboxes/{sid}` - must be READY/DEGRADED/STOPPED; `volumes` must
   say `stateful: true`.
2. **quiesce** (running only): template hook `psql -c CHECKPOINT`; record
   `pg_current_wal_lsn()`; `pg_switch_wal()` so the current segment lands
   in `/wal` (archive_timeout=15 s would otherwise delay it).
3. **pause** - all containers frozen (`PAUSED` state in sandbox-api).
4. **snapshot** - `cp -a` of each upper and plain dir into
   `ckpt/<cid>/`; `sync`.
5. **unpause**.
6. **fingerprints** - merkle roots of each data path (excluding Postgres'
   volatile files) and per-table DB hashes, taken right after unpause
   (content identical to the snapshot because the sandbox was frozen).
7. **ship** - `GET /node/tar?path=ckpt/<cid>/<layer>` streamed into
   `PUT /blobs?owner=checkpoint:<cid>`.
8. **commit** - `manifest.json` written into `ckpt/<cid>/` via
   `PUT /node/untar`; index row; `bases[sid] = cid`.

The manifest is written last on purpose: a crash anywhere before step 8
leaves a directory without a manifest, which `gc` ignores and the index
never lists. A crash after step 8 but before the index insert leaves a
valid manifest the index does not know; `POST /checkpoints/{cid}/import`
can adopt it (EXPECTED path, the index is a cache of the manifests).

## Fork

`POST /sandboxes` on sandbox-api with
`options: {stateful: true, layers: {db: ["ckpt/<cid>/db"], backend: [...]}, copies: {wal: "ckpt/<cid>/wal"}}`.
sandbox-api's `VolumeManager.prepare` mounts `lowerdir=<layer>:<base>`
with a fresh upper; `plain_dir` copies the WAL archive. The fork gets its
own network, port, agent tokens - it is a normal sandbox that happens to
start from someone else's bytes. `bases[fork] = cid` records the lineage;
deleting `cid` is refused while the fork exists.

## Restore (in place)

stop -> `POST /sandboxes/{sid}/volumes/restore {layers, copies}` -> start.
`VolumeManager.restore` lazily unmounts the old overlay, discards the upper,
mounts the checkpoint layer underneath a fresh upper, and re-copies the
WAL dir. Postgres starts on a crash-consistent data directory and replays
its own WAL (`pg_wal` is inside the data path), which is why a checkpoint
taken mid-transaction restores to the last committed state (verify 6: the
DB fingerprint equals the manifest's).

## Point-in-time recovery

Postgres runs with `wal_level=replica`, `archive_mode=on`,
`archive_command='test ! -f /wal/%f && cp %p /wal/%f'` (the standard
idempotent form) and `archive_timeout=15`. The `/wal` directory is a plain
bind mount owned by uid 70 (postgres). PITR:

1. pick the newest checkpoint whose recorded LSN <= target;
2. restore its layers **but keep the live `/wal` archive** (it contains
   every segment since the sandbox was created, including those written
   after the checkpoint);
3. drop `recovery.signal` and `postgresql.auto.conf` into the data dir
   (`restore_command='cp /wal/%f %p'`, `recovery_target_lsn`,
   `recovery_target_action=promote`);
4. start; Postgres replays to the LSN and promotes; the service waits for
   `pg_is_in_recovery() = f`.

verify.sh section 8 inserts a row, records the LSN, inserts another row,
and after PITR sees the first row and not the second.

## Lineage

```
bases:        sandbox -> checkpoint it currently sits on (set by checkpoint, fork, restore, pitr)
checkpoints:  parent = the base of the sandbox at checkpoint time
forks:        checkpoint -> sandboxes created from it
```
`tree` walks ancestors/descendants; `dependents` (children + sandboxes on
it) gate deletion.

## What lives where

| Thing | Where | Lifetime |
|---|---|---|
| layers | node `/var/lib/sbx/ckpt/<cid>/` (sbx-state volume) | until `DELETE /checkpoints/{cid}` |
| blobs | sandbox-blobs, owner `checkpoint:<cid>` | refs dropped on delete; GC collects |
| manifest | node (`manifest.json`) + index (`checkpoints.db`) | index is a cache |
| sandbox volumes | node `/var/lib/sbx/vol/<sid>/` | removed when the sandbox is deleted |

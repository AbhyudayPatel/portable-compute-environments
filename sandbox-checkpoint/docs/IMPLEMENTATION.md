# sandbox-checkpoint - Implementation Notes

## Files

```
sandbox-checkpoint/
+-- docker-compose.yml       one service; joins sandbox-api_sandnet and sandbox-blobs (external networks)
+-- .env.example             CHECKPOINT_PORT, SANDBOX_API_URL, BLOBS_URL
+-- service/
|   +-- Dockerfile           python:3.12-slim + fastapi/uvicorn/requests
|   +-- app/main.py          the protocol: checkpoint / fork / restore / pitr / import / delete / gc
|   +-- app/store.py         SQLite: manifests, bases, forks, events, lineage queries
+-- scripts/  start/stop/reset.ps1, verify.sh (47 assertions)
+-- docs/     ARCHITECTURE . EDGE-CASES . EXAMPLES . SECURITY . this file
```

Plus, in sandbox-api (the primitives this service composes):

```
api/app/volumes.py       VolumeManager: prepare / plain_dir / snapshot / restore / remount / tar / untar / du / rmdir
api/app/engine.py        stateful binds at create, teardown at destroy, pause/unpause
api/app/core.py          pause() / unpause(); PAUSED in reconcile; options passed to the engine
api/app/main.py          /pause /unpause /volumes /volumes/snapshot /volumes/restore /node/{tar,untar,dir,du}
api/app/templates.py     `stateful` paths per template, Postgres WAL archive command, `quiesce` hook
agent/tarstream.go       POST /node/tarstream (proxy mode): stream a node dir as tar
```

## Build story

1. **Feasibility first** (recorded in sandbox-api/docs/IMPLEMENTATION.md):
   overlay mounts work on the ext4 `sbx-state` volume, not on the dind
   container's own overlay rootfs; `cp -a` of an upper into a child's lower
   stack gives an isolated fork; reflinks are unavailable on ext4.
2. **Primitives in sandbox-api**: `options.stateful`, `VolumeManager`,
   pause/unpause (new `PAUSED` state, also modelled in the simulator and
   fuzzed), node file ops restricted to `/var/lib/sbx`.
3. **Smoke test by hand** through sandbox-api alone: pause -> snapshot ->
   unpause -> fork on the layers -> isolation both ways -> in-place
   restore -> byte-exact. Only then the service.
4. **The service** composes those calls, adds manifests, lineage, blob
   shipping, PITR and gc. It never talks to docker or the node.

## Bugs found while building

1. **Root-owned WAL directory.** The plain `/wal` bind was created by root
   on the node; Postgres (uid 70) could not write, `archive_command`
   failed on every segment, and `ls /wal` stayed empty while the server
   looked healthy. Fix: templates declare `chown` for plain binds.
2. **`engine.volumes.BASE`** did not exist (BASE is a module constant);
   `GET /sandboxes/{id}/volumes` returned 500. Fix: import the constant.
3. **A method named `list`** in `Store` shadowed the builtin inside the
   class body, so the next annotation `-> list[dict]` raised
   "'function' object is not subscriptable" at import and the service
   never started (the first battery run reported 35 failures for that one
   reason). Renamed to `list_all`.
4. **`archive_timeout`**: without `pg_switch_wal()` in the quiesce step,
   the segment containing the checkpoint's LSN could still be open and
   PITR to that LSN would wait for an archive that arrives up to 15 s
   later. The service switches the WAL explicitly after recording the LSN.

## Verified

- [x] `bash scripts/verify.sh` - **51/51** (health, stateful volumes + WAL
      archive, checkpoint while running incl. blobs shipped, lineage +
      diff, fork isolation both ways, in-place restore byte-exact, stopped
      checkpoint with node-side hashes, PITR to an LSN, delete protection,
      corruption detection + import from the blob store, validation,
      manifest-last rule)
- [x] sandbox-api's own battery section 18 covers the primitives
      (stateful mount, pause/unpause, snapshot, restore, node ops, teardown)

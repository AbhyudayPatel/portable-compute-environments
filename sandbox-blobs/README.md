# sandbox-blobs - Content-addressed blob store (CAS)

> One service for every large byte stream the platform produces: image
> build contexts, checkpoint layers, network cassettes, big outputs,
> screenshots, dataset exports. Addressed by sha256 (deduplicated by
> construction), **streamed** (a 300 MB upload peaks at ~45 MB of service
> memory), kept alive by **references and pins**, deleted only by a GC
> that cannot race a new reference.

```
you / sandbox-checkpoint / sandbox-egress --> http://localhost:9400
                                                  |
                                   /blobs/sha256/ab/abcdef...   (files = truth)
                                   /blobs/index.db              (refs, pins, sizes)
```

## Quick start

```powershell
powershell -ExecutionPolicy Bypass -File scripts\start.ps1
curl.exe -T somefile http://localhost:9400/blobs
bash scripts/verify.sh        # 44 assertions, ~2 min
```

## API

| Method | Path | Notes |
|---|---|---|
| PUT | `/blobs` | body = bytes, streamed; `?owner=` also registers a reference; 201 new / 200 existed |
| GET | `/blobs/{hash}` | bytes; `Range` supported (206); `ETag` = hash |
| HEAD | `/blobs/{hash}` | `Content-Length` |
| GET | `/blobs/{hash}/meta` | size, timestamps, refs, pins |
| GET | `/blobs/{hash}/verify` | re-hash the file |
| DELETE | `/blobs/{hash}` | 204, or 409 while referenced/pinned |
| POST | `/refs` | `{owner, hashes[]}`; 404 for unknown blobs |
| DELETE | `/refs/{owner}` | drop every reference of an owner |
| GET | `/refs?hash=` `?owner=` | who references what |
| PUT/DELETE/GET | `/pins/{name}` `/pins` | named pins survive GC |
| POST | `/gc?grace=60&dry_run=0` | delete unreferenced, unpinned blobs older than grace |
| GET | `/stats` `/healthz` | counts, bytes, disk |

## The GC contract (read this before writing a client)

A blob survives while it has a reference or a pin, and never gets
collected within `GC_GRACE_SECONDS` (default 60) of being written. So:
**upload, then reference** - or do both at once with `PUT /blobs?owner=X`.
Owners drop all their references with one call when they go away
(`DELETE /refs/X`). That is how checkpoints (owner `checkpoint:<id>`) and
cassettes (owner `cassette:<id>`) manage lifetime without ever deleting
bytes themselves.

## Docs

- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) - addressing, streaming,
  refs/pins/GC and the race it closes, crash recovery.
- [`docs/EDGE-CASES.md`](docs/EDGE-CASES.md) - every case mapped to its
  verify.sh assertion.
- [`docs/IMPLEMENTATION.md`](docs/IMPLEMENTATION.md) - files and the bugs
  found.
- [`docs/EXAMPLES.md`](docs/EXAMPLES.md) - exercises.
- [`docs/SECURITY.md`](docs/SECURITY.md) - trust posture.

## Reset

`scripts\stop.ps1` keeps the volume; `scripts\reset.ps1` wipes it.

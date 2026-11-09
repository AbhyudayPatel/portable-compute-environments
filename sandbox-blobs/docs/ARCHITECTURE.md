# sandbox-blobs - Architecture

## The one-sentence idea

Every large byte stream in the platform (image build contexts, checkpoint
layers, network cassettes, big outputs, screenshots, dataset exports) is
stored exactly once under the sha256 of its content, streamed in and out
without ever being held in memory, kept alive by references and pins, and
deleted only by an explicit garbage collection that cannot race a new
reference.

```
 client  --PUT /blobs (stream)-->  temp file + sha256 on the fly
                                    |  rename (atomic) -> /blobs/sha256/ab/abcdef...
                                    v
                              +------------+      +--------------------+
                              | files      |      | SQLite index       |
                              | (truth of  |      | blobs(size,times)  |
                              |  existence)|      | refs(owner,hash)   |
                              +------------+      | pins(name,hash)    |
                                                  +--------------------+
 client  --GET /blobs/<hash>-->  FileResponse / Range stream
 owner   --POST /refs {owner, hashes}-->  keeps blobs alive
 admin   --POST /gc?grace=60-->  unlink unreferenced, unpinned, old enough
```

## Content addressing

- Address = hex sha256 of the bytes. Two uploads of the same bytes are one
  blob (`existed: true` on the second).
- Layout `sha256/<first two hex>/<hash>` keeps directories small.
- Uploads write to `tmp/` under the same filesystem and `os.replace()`
  into place: a blob is either complete or absent, never partial. Interrupted
  uploads leave only temp files, which boot cleanup removes.
- `GET /blobs/{hash}/verify` re-hashes the file - bit rot or tampering is
  detectable at any time.

## Streaming

`PUT` consumes `request.stream()` chunk by chunk, hashing and writing as it
goes; `GET` uses `FileResponse` (sendfile) or a chunked generator for Range
requests. The compose file caps the service at 256 MB of memory and
verify.sh uploads 300 MB while sampling `docker stats`: the peak stays far
below the cap (section 9).

## References, pins, garbage collection

| Concept | Who uses it | Lifetime |
|---|---|---|
| **ref** `(owner, hash)` | a checkpoint manifest, a cassette, a dataset export - anything that embeds blob hashes | dropped by `DELETE /refs/{owner}` when the owner is deleted |
| **pin** `name -> hash` | humans / tooling marking "golden" blobs | until `DELETE /pins/{name}` |
| **grace** | GC | a blob younger than `grace` seconds is never collected |

GC deletes a blob iff: no refs **and** no pins **and** `mtime` older than
`grace`. The race that must not happen - "GC decides to delete X while an
uploader is about to reference X" - is closed twice: the uploader's
`POST /refs` (or `PUT ?owner=`) and GC's scan+unlink both take the same
process lock, and even if a client references a blob it uploaded a while
ago, the `touch` on re-upload refreshes `mtime` so the grace window
restarts. The documented contract for writers: upload, then reference
within the grace window (or use `?owner=` to do both atomically).

`DELETE /blobs/{hash}` is the manual path: refused (409) while referenced
or pinned.

## Crash recovery

- Files are the truth. Boot `reconcile()` drops index rows whose file is
  missing (a crash between unlink and the index update), and startup
  removes leftover temp files.
- Index corruption is survivable: delete `index.db`, restart, re-register
  refs from the owners (the manifests that embed hashes are the
  authoritative record).

## Limits

- Single process, local filesystem. Scale-out means sharding by hash
  prefix or putting an object store behind the same API - the API is
  deliberately S3-shaped (PUT/GET/HEAD/Range/ETag).
- No authentication (local lab); see SECURITY.md.

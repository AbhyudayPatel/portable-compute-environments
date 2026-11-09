# sandbox-blobs - Implementation Notes

## Files

```
sandbox-blobs/
+-- docker-compose.yml     one service, `blob-data` volume, 256 MB memory limit (streaming proof)
+-- .env.example           BLOBS_PORT, MAX_BLOB_BYTES, GC_GRACE_SECONDS
+-- api/
|   +-- Dockerfile         python:3.12-slim + fastapi/uvicorn
|   +-- app/main.py        routes: put/get/head/meta/verify/delete, refs, pins, gc, stats
|   +-- app/store.py       SQLite index: blobs, refs, pins; unreferenced(); reconcile()
+-- scripts/  start/stop/reset.ps1, verify.sh (44 assertions)
+-- docs/     ARCHITECTURE . EDGE-CASES . EXAMPLES . SECURITY . this file
```

## Key implementation points

- **Streaming upload**: `async for chunk in request.stream()` into a
  temp file opened with `os.fdopen`, `sha256.update` per chunk, `fsync`,
  then `os.replace` into the final path under the GC lock. The 413 check
  happens per chunk so an oversized body is rejected early.
- **Downloads** use Starlette's `FileResponse` (zero-copy where the OS
  allows) and a chunked generator for `Range`.
- **One lock** (`_gc_lock`) guards rename-into-place, ref/pin writes, GC
  and manual delete. SQLite has its own lock inside `Store`.
- **Reconcile at boot** keeps the index honest when files vanish.

## Bugs found while building

1. **Git Bash path mangling, again.** verify.sh's crash-recovery section
   ran `docker exec ... rm -f /blobs/sha256/xx/<hash>`; Git Bash rewrote
   `/blobs/...` into `C:/Program Files/Git/blobs/...`, the file survived,
   and the test reported the index "did not drop" it. Fix:
   `export MSYS_NO_PATHCONV=1` at the top of the script. Every verify
   script in this repo that passes absolute container paths now sets it.

## Verified

- [x] `bash scripts/verify.sh` - **44/44** (dedup, ranges, ETag, verify,
      refs/pins/GC semantics incl. grace and dry-run, 409 on referenced
      delete, 300 MB streaming under a 256 MB limit with a ~45 MB peak,
      20 concurrent uploads, boot reconcile after a file vanished, 413 cap)

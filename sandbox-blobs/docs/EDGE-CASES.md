# sandbox-blobs - Edge Cases

All **VERIFIED** by `scripts/verify.sh` (44 assertions) unless marked.

| # | Edge case | Handling | Test |
|---|---|---|---|
| 1 | Duplicate upload | address = sha256; second PUT returns 200 `existed:true`, store count unchanged | 2 |
| 2 | Partial upload (client dies, service dies) | written to `tmp/` then `os.replace()`; temp files removed at boot; a blob is complete or absent | by construction; crash drill in EXAMPLES level 5 (EXPECTED) |
| 3 | Hash spoofing | the server computes the hash; clients cannot name a blob | by construction |
| 4 | Bad hash in path | 64-hex validation -> 400 | 4 |
| 5 | Unknown blob | 404 on GET/HEAD/meta/verify | 4 |
| 6 | Range requests | `bytes=a-b` -> 206 with `Content-Range`; out of range -> 416 | 3 |
| 7 | Bit rot / tampering | `/verify` re-hashes | 4 (ok), EXAMPLES level 1 (corrupted -> false) |
| 8 | Reference to unknown blob | 404, nothing recorded | 5 |
| 9 | Upload + reference atomically | `PUT ?owner=` inside the same lock as the rename | 5 |
| 10 | GC vs young blob | `mtime` younger than grace -> `kept_young`, never deleted | 7 |
| 11 | GC vs referenced / pinned | excluded by the SQL `NOT EXISTS` | 7 |
| 12 | GC dry run | reports, deletes nothing | 7 |
| 13 | GC vs concurrent ref | ref insert and GC scan+unlink share one lock | by construction (sim-level proof would need the DST harness; EXPECTED) |
| 14 | Manual delete while referenced | 409 | 8 |
| 15 | Memory under large uploads | streamed hashing+writing; 256 MB container limit; 300 MB upload peaks ~45 MB | 9 |
| 16 | Concurrent uploads | per-request temp files, atomic renames, SQLite under a lock | 10 (20 parallel) |
| 17 | File removed behind the API | boot `reconcile()` drops the index row and its refs | 11 |
| 18 | Oversized upload | `MAX_BLOB_BYTES` -> 413, temp file removed | 12 |
| 19 | Disk full | write fails -> 500, temp removed; no partial blob | EXPECTED |
| 20 | Same blob referenced by many owners | one file, N `(owner,hash)` rows; survives until the last owner drops it | 5 + 7 |

## Known limits

- No quota per owner, no auth (SECURITY.md).
- Single node; the API is S3-shaped so an object store can replace it.

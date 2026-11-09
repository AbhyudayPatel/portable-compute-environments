# sandbox-blobs - Security Notes

- **No authentication.** Anyone reaching `localhost:9400` can read any
  blob by hash, upload, add references and run GC. Local lab only. In a
  shared deployment: bearer tokens per owner, read scoping by owner, and
  GC restricted to an admin role.
- **Hashes are capabilities.** Knowing a sha256 lets you fetch the bytes.
  Do not log hashes of secret material in places less protected than the
  store.
- **Integrity.** The address is the content hash, so a tampered file no
  longer matches its name; `GET /blobs/{hash}/verify` detects it. The
  index is not integrity-protected (SQLite file); treat the volume as
  trusted storage.
- **Resource exhaustion.** `MAX_BLOB_BYTES` (default 8 GiB) caps a single
  upload; there is no global quota - add one before exposing the service.
  Streaming means an upload cannot exhaust memory (256 MB limit in compose
  proves it); it can exhaust disk.
- **Path safety.** Hashes are validated as 64 hex characters before being
  used in paths; nothing user-controlled reaches the filesystem otherwise.
- **GC safety.** A reference cannot be added to a blob GC is deleting
  (same lock), and young blobs are never collected (grace window), so a
  correct writer never loses data to GC.

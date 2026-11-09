# sandbox-blobs - Exercises

API at http://localhost:9400 (`scripts\start.ps1`). Examples use Git Bash.

## Level 0 - Upload, fetch, dedup (3 min)

```bash
echo hello > a.txt
curl -s -T a.txt localhost:9400/blobs            # 201 {"hash":"...","size":6,"existed":false}
curl -s -T a.txt localhost:9400/blobs            # 200 ... "existed":true
sha256sum a.txt                                  # same hash
curl -s localhost:9400/blobs/<hash>              # hello
curl -s localhost:9400/stats
```
**Teaches:** the name of a blob is its content; uploading twice stores once.

## Level 1 - Ranges and integrity (5 min)

```bash
curl -s -I localhost:9400/blobs/<hash>                     # Content-Length, ETag
curl -s -H 'Range: bytes=0-1' localhost:9400/blobs/<hash>  # "he", status 206
curl -s localhost:9400/blobs/<hash>/verify                 # {"ok": true}
```
Corrupt it on purpose: `docker exec sandbox-blobs-blobs-1 sh -c 'echo x >> /blobs/sha256/<ab>/<hash>'`
then verify again -> `ok: false`.

## Level 2 - Keep things alive (10 min)

```bash
curl -s -X POST localhost:9400/refs -H 'content-type: application/json' \
  -d '{"owner":"checkpoint:demo","hashes":["<hash>"]}'
curl -s -X POST "localhost:9400/gc?grace=0"      # deleted: 0 - it is referenced
curl -s -X DELETE localhost:9400/refs/checkpoint:demo
curl -s -X POST "localhost:9400/gc?grace=0&dry_run=1"   # would delete 1
curl -s -X POST "localhost:9400/gc?grace=0"              # gone
```
Now pins: upload again, `PUT /pins/golden {"hash":...}`, GC, still there.
**Teaches:** refs are owned (drop them all at once when the owner dies);
pins are for humans.

## Level 3 - The grace window (5 min)

Upload something, immediately `POST /gc` (default grace 60): `kept_young: 1`.
Wait a minute (or pass `grace=0`): collected. **Teaches:** why an uploader
that references a blob "soon after" PUT is safe, and why `PUT ?owner=` is
safer still.

## Level 4 - Big and parallel (10 min)

```bash
python -c "import os; open('big.bin','wb').write(os.urandom(300<<20))"
docker stats --no-stream sandbox-blobs-blobs-1     # watch memory
curl -s -T big.bin "localhost:9400/blobs?owner=big"
```
Memory stays at tens of MB while 300 MB stream through. Fire 20 uploads in
parallel with `&`; `GET /refs?owner=...` lists them all.

## Level 5 - Expert: crash recovery (10 min)

Delete a blob file behind the API's back, restart the container, `GET` it:
404, and its refs are gone (boot reconcile). Then kill the container in
the middle of a large upload (`docker kill` while curl runs): restart,
`ls /blobs/tmp` is empty, the partial blob never appeared.
**Teaches:** files are the truth; temp + atomic rename makes partial blobs
impossible.

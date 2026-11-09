# sandbox-checkpoint - Security Notes

- **Powers.** Through sandbox-api this service can pause any sandbox, copy
  its data, remount its volumes and write files into `/var/lib/sbx` on the
  engine node (root). Those node operations are deliberately *not* a
  generic exec: sandbox-api restricts them to the sandbox state area
  (`/var/lib/sbx/...`, deletes only under `/ckpt/`), so a compromised
  checkpoint service can damage checkpoints and sandbox volumes, not the
  engine.
- **No authentication** between the three services (local lab); in a
  shared deployment the checkpoint service holds a token that authorises
  the volume and node routes on sandbox-api, and the blob owner namespace
  `checkpoint:*` is reserved for it.
- **Checkpoint contents are the sandbox's secrets.** A layer tar in the
  blob store contains the database files and the workspace; anyone who can
  read the blob store can read them. Encrypt at rest or scope blob reads
  by owner before multi-tenant use.
- **Fork = clone of secrets.** A fork starts with every file the parent
  had at checkpoint time, including credentials an agent may have written
  to the workspace. Treat forks as the same trust domain as the parent.
- **PITR writes `postgresql.auto.conf`** into the data directory with a
  `restore_command`. The command is fixed (`cp /wal/%f %p`); user input
  only supplies the LSN, validated as `hi/lo` hex.
- **Deletion safety.** `DELETE /checkpoints/{cid}` refuses while anything
  depends on it; `force=1` is for cleanup scripts and should be
  admin-only.

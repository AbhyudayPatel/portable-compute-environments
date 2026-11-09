# cluster-in-a-box - Security Notes

- **Workers are privileged containers inside the outer engine.** A
  nested dockerd needs `--privileged`; the template is the only one with
  `allow_privileged`, and only the `worker` template's container gets the
  flag (checked in the engine). A compromise inside a worker is a
  compromise of the outer dind - which is already privileged on the
  Docker Desktop VM. This is a lab topology, not a tenancy boundary.
- **Worker APIs are published to the host** (`9340+k`) without
  authentication, like the outer API. Bind to localhost / add auth before
  sharing the machine.
- **Node tokens** inside workers are a fixed string (`worker-node-token`)
  set by the control plane through template env; rotate per worker in a
  real deployment.
- **Data migration copies files in the clear** between workers over the
  compose network through the two APIs. Fine locally; TLS between nodes
  otherwise.
- **Fencing is best-effort**: the control plane purges stale copies when
  a worker returns, but a partitioned worker keeps running its copy until
  it reconnects. A placement that must never run twice needs a lease the
  worker itself enforces (EXPECTED).

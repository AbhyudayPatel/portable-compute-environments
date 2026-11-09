# sandbox-sim - Security Notes

The simulator runs no containers, opens no ports and touches no engine.
It imports the control plane's core package and executes it in-process
against fakes. Its security relevance is indirect but real:

- **What it proves:** liveness and safety properties of the lifecycle
  logic under faults - no leaked sandboxes, no port double-allocation, no
  over-capacity, no orphaned creates, TTLs that fire exactly once. These
  are the properties an attacker would abuse for resource exhaustion or
  for keeping a sandbox alive past its TTL.
- **What it does not prove:** anything about isolation (that is gVisor /
  the engine), authentication (there is none on the API yet), or the
  agent protocol.
- **Supply chain:** the image is `python:3.12-slim` + `pytest`; the core
  is copied from the sibling directory at build time, never fetched.
- **Determinism as a security tool:** a reported bug comes with a seed;
  reviewers can replay the exact sequence rather than trust a
  description.

Run it in CI on every change to `sandbox-api/api/app`; a failing seed is
a blocking finding.

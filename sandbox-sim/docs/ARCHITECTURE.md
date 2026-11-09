# sandbox-sim - Architecture

## The idea in one paragraph

Take the real lifecycle code, replace everything non-deterministic around
it (the Docker engine, the clock, random ids, threads) with objects the
test controls, and drive it from a seeded random number generator. Every
decision the scenario makes - which operation next, when faults strike,
how much time passes - comes from the seed, so the same seed produces the
same run on any machine. After each step, assert properties that must
always hold. When one fails, the seed *is* the bug report.

## Ports (what was swapped out)

| Production | Simulator | Where the swap happens |
|---|---|---|
| `DockerEngine` (dockerd via SDK + agent proxy) | `FakeEngine` (`sim/fake_engine.py`) | `Core(store, engine)` constructor |
| `time.monotonic/time/sleep` | `VirtualClock` (`sim/vclock.py`) | `app.clock.impl` module attribute |
| `uuid4()` ids, `secrets` tokens | seeded counters | `Core(id_factory=, token_factory=)` |
| SQLite file in a volume | SQLite shared-cache in-memory DB per run | `Store("file:simN?mode=memory&cache=shared")` |
| provision **thread** | provision **generator** stepped by the scenario | `Core.provision_steps()` is a generator in production too; `main.py` drives it with a thread |
| informer threads (watch/resync/reaper) | scenario steps `informer`, `resync`, `reaper` | the threads only call core methods |

Nothing in `core.py` knows it is being simulated.

## FakeEngine

A dict of sandboxes -> containers -> status, plus the same method surface
the core calls on `DockerEngine`:
`ping, info, has_runtime, ensure_image, create_sandbox_resources,
wait_ready, destroy_sandbox_resources, stop_sandbox, start_sandbox,
engine_sandbox_ids, engine_port_map, engine_sandboxes, sandbox_containers`.

Fault knobs:

| Knob | Effect | Real-world equivalent |
|---|---|---|
| `down = True` | every call raises `EngineDown` | dind paused / restarting |
| `fail_create_at = k` | the k-th container of the next create raises; the fake rolls back what it made (like the real engine's rollback closure) | image missing, OOM at start |
| `kill(sid, name, oom)` | container -> exited, queues a `die`/`oom` event | `docker kill`, OOM killer |
| `remove_out_of_band(sid)` | sandbox vanishes, queues `destroy` events | `docker rm -f` inside dind |
| `stop_sandbox` | containers -> exited AND queues `die` events | what dockerd really emits on stop |

Events queue up in `engine.events` and are delivered only when the
scenario chooses an `informer` step - so the window between "it happened"
and "the API knows" is explored, just like reality.

## The scenario driver (`sim/scenario.py`)

```
Scenario(seed, steps)
  .step()        pick one kind by weight, apply it, log it
  .run()         steps x step(); check_invariants after each;
                 then QUIESCE: drain provisions, deliver events, resync;
                 check quiescent invariants
```

Step kinds and weights (per 100): create 14, delete 8, stop 4, start 4,
provision-step 22, reaper 5, informer 8, resync 3, time 10, engine_down 2,
engine_up 3, kill 4, remove 2, fail_next 2, crash 2, skew 1.

`crash` models `kill -9` of the API: in-flight generators and locks vanish,
`in_flight` and `boot_pending` reset, the DB stays, and a "new process"
runs `Core.reconcile()` - unless the engine is down, in which case the
boot reconcile is skipped exactly like production does.

A `SimApi` wraps the core like `main.py` does (create -> start a provision
generator) so the scenario talks to the same surface a REST client sees.

## Invariants (`sim/invariants.py`)

Always (after every step):

| Id | Property |
|---|---|
| I1 | ports unique among live rows, inside the pool, never on FAILED rows |
| I2 | names unique among live rows |
| I3 | engine never holds more than `max_sandboxes` |
| I4 | event sequence numbers contiguous per sandbox |
| I5 | state vocabulary |
| I6 | DELETED rows own no engine resources (engine up, no generator in flight) |
| I7 | a user-stopped sandbox reads STOPPED until started/deleted/lost (the informer must recognise stop's own `die` events) |

At quiescence (everything drained):

| Id | Property |
|---|---|
| Q1 | engine ids == DB live ids; FAILED rows own nothing |
| Q2 | no CREATING left; READY/DEGRADED/STOPPED agree with container statuses |
| Q3 | ports in DB == ports in engine |
| Q4/I8 | every expired TTL is reaped by one sweep (monotonic; and by wall clock when no skew was injected) - these run **last** because they call `reap_expired()` |

## Determinism and replay

- The RNG is `random.Random(seed)`; the clock only moves on `time`/`skew`
  steps or inside fake operations that model latency.
- Ids are `s<seed><counter>`; SQLite ordering ties are broken by id, so
  `random.choice` over live rows is stable.
- `python -m sim.run --seed N --steps M --verbose` prints the log that
  the failing run printed. `tests/test_invariants.py::test_determinism_same_seed_same_log`
  asserts two runs produce identical logs.

## Shrinking

`shrink(seed, steps)` binary-searches the smallest step count that still
fails for that seed (a failure at step k also fails for any prefix >= k,
because invariants are checked after every step). The CLI prints the
replay command.

## Mutation tests (does the harness have teeth?)

`tests/test_bug_injection.py` monkeypatches a safeguard out of the core
and asserts that some seed now fails:

- forget to free the port of a FAILED create -> I1/Q3
- reconcile that never marks vanished rows -> Q1
- no TTL re-anchor after a restart (the v1 bug) -> I8
- informer that marks DEGRADED without re-checking state under the lock -> I7

If one of these starts passing (the sim no longer notices), the harness
lost coverage - that is the signal to fix the *harness*.

## What the simulator is NOT

- It does not test dockerd semantics, networking, the agent, timing of
  real threads, or SQLite under concurrency. `scripts/verify.sh` in
  sandbox-api does (and found the 20 ms window the sim could not).
- It models provisioning at the granularity of engine calls, not Python
  bytecodes; a race *inside* `create_sandbox_resources` is invisible to
  it (the real engine's rollback closure covers that).

## The scheduler simulation (`sim/sched.py`)

| Production | Simulator |
|---|---|
| `upstream.Upstream` (requests to sandbox-api + sandbox-checkpoint) | `SimUpstream`: calls the sandbox-api `Core` in-process; a dict of fake checkpoints; `fork` = create |
| placer/monitor/janitor/cancel threads | scenario steps `placer`, `monitor`, `janitor`, `cancel_tick` |
| provision / preempt worker threads | generators stepped one `prov` / `preempt_step` at a time |
| task execution | `task_done` step runs `core.run_task` with a fake exec result |
| scheduler process | `sched_crash` drops generators and runs `core.reconcile()` |

Both cores share the one `VirtualClock`; every sandbox-api fault
(engine down, kill, remove, api crash) is also available as `api_step`.
Invariants S1-S6 (reservations, tenant caps, state vocabulary, event seqs,
PREEMPTED shape, queue bound with crash slack) run after every step;
SQ1-SQ3 (no in-flight states, no leaked `sched-*` sandboxes) at
quiescence, which also runs sandbox-api's periodic resync each iteration
as production would.

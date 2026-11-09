"""Invariants - the properties that must hold after EVERY simulated step.

Two kinds:
  always     true at any instant (even mid-provision, mid-fault)
  quiescent  true once every in-flight provision has run to completion, all
             pending engine events were delivered and one resync ran

If one fails, the scenario raises AssertionError with the seed, the step
index and the offending rows - that is the reproducer.
"""


def _fail(sc, step, msg, **ctx):
    raise AssertionError(
        f"seed={sc.seed} step={step}: {msg}\n  ctx={ctx}\n  last log:\n    "
        + "\n    ".join(sc.log[-12:]))


def check_invariants(sc, step: int, quiescent: bool = False) -> None:
    rows = sc.store.list_all(include_deleted=True)
    live = [r for r in rows if r["state"] != "DELETED"]
    engine = sc.engine.sandboxes

    # I1  ports: unique among live rows, inside the pool, never on FAILED rows
    ports = [r["port"] for r in live if r["port"] is not None]
    if len(ports) != len(set(ports)):
        _fail(sc, step, "duplicate port among live sandboxes", ports=ports)
    for r in live:
        if r["port"] is not None and not (sc.core.pool_start <= r["port"] <= sc.core.pool_end):
            _fail(sc, step, "port outside pool", row=r)
        if r["state"] == "FAILED" and r["port"] is not None:
            _fail(sc, step, "FAILED sandbox holds a port", row=r)

    # I2  names: unique among live rows
    names = [r["name"] for r in live]
    if len(names) != len(set(names)):
        _fail(sc, step, "duplicate live name", names=names)

    # I3  capacity: the engine never holds more than max_sandboxes
    if len(engine) > sc.core.max_sandboxes:
        _fail(sc, step, "engine over capacity", n=len(engine))

    # I4  event sequences contiguous per sandbox
    for r in rows:
        seqs = [e["seq"] for e in sc.store.events(r["id"])]
        if seqs != list(range(1, len(seqs) + 1)):
            _fail(sc, step, "event seq gap/dup", sid=r["id"], seqs=seqs)

    # I5  state vocabulary
    for r in rows:
        if r["state"] not in ("CREATING", "READY", "DEGRADED", "PAUSED", "STOPPED", "FAILED", "DELETED"):
            _fail(sc, step, "unknown state", row=r)

    # I6  a DELETED/FAILED row never has engine resources... except transiently
    #     while a provision generator is mid-flight (FAILED by crash rollback
    #     races are closed by reconcile). Always-true form: DELETED rows have
    #     no resources unless the engine was down when we tried.
    if not sc.engine.down:
        for r in rows:
            if r["state"] == "DELETED" and r["id"] in engine and r["id"] not in sc.api.provisions:
                _fail(sc, step, "DELETED row still has engine resources", sid=r["id"])

    # I7  user intent wins: a sandbox the user stopped (and did not start,
    #     delete or lose since) must read STOPPED no matter which `die`
    #     events dockerd emitted for the stop. The informer has to recognise
    #     those events as explained by stop() instead of flagging DEGRADED.
    for sid in sc.user_stopped:
        r = sc.store.get(sid)
        if r and r["state"] not in ("STOPPED", "DELETED"):
            _fail(sc, step, "user-stopped sandbox drifted out of STOPPED", row=r)

    if not quiescent:
        return

    # Q1  engine truth == DB truth (FAILED rows must own NOTHING in the engine)
    db_ids = {r["id"] for r in live if r["state"] != "FAILED"}
    eng_ids = set(engine)
    if db_ids != eng_ids:
        _fail(sc, step, "engine/DB divergence at quiescence",
              only_db=sorted(db_ids - eng_ids), only_engine=sorted(eng_ids - db_ids))
    for r in live:
        if r["state"] == "FAILED" and r["id"] in engine:
            _fail(sc, step, "FAILED row still owns engine resources", sid=r["id"])
    # Q2  no CREATING left, states agree with container statuses
    for r in live:
        if r["state"] == "CREATING":
            _fail(sc, step, "CREATING at quiescence", row=r)
        if r["state"] in ("READY", "DEGRADED", "STOPPED", "PAUSED"):
            sts = list(engine[r["id"]]["containers"].values())
            running = sts.count("running")
            paused = sts.count("paused")
            if paused and running + paused == len(sts):
                expect = "PAUSED"
            else:
                expect = "READY" if running == len(sts) else "STOPPED" if running == 0 else "DEGRADED"
            if r["state"] != expect:
                _fail(sc, step, "state disagrees with engine at quiescence",
                      row=r, containers=engine[r["id"]]["containers"])
    # Q3  ports in DB == ports in engine
    eng_ports = {sb["port"] for sb in engine.values() if sb["port"]}
    db_ports = {r["port"] for r in live if r["port"] and r["state"] != "FAILED"}
    if eng_ports != db_ports:
        _fail(sc, step, "port map divergence", engine=eng_ports, db=db_ports)
    _ttl_checks(sc, step)


def _ttl_checks(sc, step: int) -> None:
    """Run LAST: these checks call reap_expired(), which mutates state, so
    every snapshot-based invariant above must already have run."""
    rows = sc.store.list_all(include_deleted=True)
    live = [r for r in rows if r["state"] != "DELETED"]
    now = sc.clock.monotonic()
    # Q4  every expired TTL was reaped (deadlines re-anchored across crashes)
    for r in live:
        if r["deadline_mono"] and r["deadline_mono"] <= now - 1 and r["state"] != "CREATING":
            # one reaper sweep must clear it
            sc.core.reap_expired()
            if sc.store.get(r["id"])["state"] != "DELETED":
                _fail(sc, step, "expired TTL not reaped", row=r, now=now)
    # I8  TTL contract: a sandbox whose wall deadline passed must be reaped by
    #     the next sweep, regardless of restarts (deadlines re-anchored). Wall
    #     time is only trustworthy if the scenario injected no clock skew.
    if not sc.skewed and not sc.engine.down:
        now_w = sc.clock.time()
        for r in live:
            if r["deadline_wall"] and r["deadline_wall"] + 1 < now_w and r["state"] != "CREATING":
                sc.core.reap_expired()
                if sc.store.get(r["id"])["state"] != "DELETED":
                    _fail(sc, step, "expired TTL (wall) not reaped", row=r, now_wall=now_w)

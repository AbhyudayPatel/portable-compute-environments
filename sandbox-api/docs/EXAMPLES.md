# sandbox-api v2 - Practice Exercises

Hands-on, ordered beginner -> expert. Each exercise says what to do, what
to **observe**, and what it *teaches*. Start the stack first
(`scripts\start.ps1`). The API is at http://localhost:9000; the console
at the same address. `curl` examples are for Git Bash / Linux; in
PowerShell use `curl.exe` and single-quote the JSON the same way.

A tiny helper used below (prints one field of a JSON reply):

```bash
j() { python -c "import sys,json; print(json.load(sys.stdin)$1)"; }
# usage:  curl -s localhost:9000/sandboxes/ID | j "['state']"
```

---

## Level 0 - The console (5 min)

0.1 **Read the "how it works" strip.** Four boxes: you -> api -> dind ->
sandbox. The new words are *agent* (PID 1 inside every container) and
*proxy* (the door on the engine). **Observe** the header pills: `node`
and `informer` - the proxy and the event watcher are both part of health
now.

0.2 **Create a `web` sandbox from the form.** Watch the creation log:
`allocated port` -> `container started` -> `all agents answering` ->
`READY`. **Teaches:** readiness now includes the agent inside the
container, not only the app.

0.3 **Open details.** Table shows each container's inner IP and runtime
(`runc`). Click **open shell**: a real pty inside the sandbox. Type
`stty size`, then `ls /www`. Close the drawer and reopen it, click open
shell again - a *new* session; the old one still exists (list via
`GET /sandboxes/{id}/sessions`). **Teaches:** sessions outlive clients.

0.4 **Files + fingerprint.** Set path `/www`, click list, then
fingerprint. Note the root hash. In the exec box run
`echo x >> /www/index.html`, fingerprint again. **Observe:** the root
changed and exactly one entry's hash changed.

0.5 **Watch the live log at the bottom** while you stop and start the
sandbox: events arrive instantly (server-sent events), nothing polls.

---

## Level 1 - Exec and sessions (10 min)

1.1 **One-shot exec with an exit code.**
```bash
curl -s -X POST localhost:9000/sandboxes/$ID/exec -H 'content-type: application/json' \
  -d '{"cmd":["sh","-c","echo hi; exit 3"]}'
```
**Observe:** `exit_code: 3`, `session_id` - the output is also kept in a
session you can fetch later.

1.2 **Timeouts kill.** `{"cmd":["sleep","60"],"timeout":2}` -> after 2 s
`timed_out:true, exit_code:143`. Then exec `ps` - no `sleep 60` left.
**Teaches:** v1 could only abandon a timed-out command; v2 kills its whole
process group (TERM, then KILL after 5 s).

1.3 **A resumable session.**
```bash
S=$(curl -s -X POST localhost:9000/sandboxes/$ID/sessions -H 'content-type: application/json' \
  -d '{"cmd":["sh"],"tty":true}' | j "['id']")
curl -s -X POST localhost:9000/sandboxes/$ID/sessions/$S/stdin --data-binary $'echo one\n'
curl -s "localhost:9000/sandboxes/$ID/sessions/$S/stream?since=0"
```
Note the last `seq`, send `echo two`, then stream with `since=<seq>`:
only the new frames arrive. **Teaches:** the ring buffer + sequence
numbers = reconnect without loss. Try `follow=1&timeout=10` and send
input from another terminal: frames appear as they happen.

1.4 **Resize and signals.** `POST .../resize {"cols":132,"rows":50}` then
`stty size`; `POST .../signal {"signal":"INT"}` to interrupt a running
`sleep 100` inside the shell.

1.5 **Overflow.** Create a session with `"buffer_bytes":2048` running
`head -c 20000 /dev/zero | tr '\0' x`. Stream from `since=0`: the first
line is `{"gap":true,...}`. **Teaches:** the agent tells you when it had
to drop frames instead of handing you a hole.

---

## Level 2 - Files, fingerprints, diffs (10 min)

2.1 **Write and read.**
```bash
curl -s -X PUT "localhost:9000/sandboxes/$ID/fs/write?path=/www/note.txt&mode=0600" --data-binary 'hello'
curl -s "localhost:9000/sandboxes/$ID/fs/read?path=/www/note.txt"
curl -s "localhost:9000/sandboxes/$ID/fs?path=/www" | j "['entries']"
```

2.2 **Tar round trip.** `GET .../fs/tar?path=/www > www.tar`, then
`PUT .../fs/tar?path=/restore --data-binary @www.tar`. List `/restore`.

2.3 **Try to escape.** Build a tar whose entry is `../../etc/evil` and
upload it to `/up`. Read `/etc/evil` -> 404; read `/up/etc/evil` -> there
it is. **Teaches:** uploads are confined to their destination.

2.4 **Diff two sandboxes.** Create `web` sandboxes `a` and `b`, then
`GET /diff?a=ID_A&b=ID_B&path=/www`. **Observe:** `index.html` is
`modified` (it embeds each sandbox's name and port). Diff `a` against
itself: `identical: true`.

2.5 **Database fingerprint.** Create a `coreapp` (first time builds three
images inside dind, ~1-2 min; afterwards seconds). Then:
```bash
curl -s localhost:9000/sandboxes/$CID/fingerprint/db | j ""
curl -s -X POST localhost:$PORT/api/tasks -H 'content-type: application/json' -d '{"title":"x"}'
curl -s localhost:9000/sandboxes/$CID/fingerprint/db | j "['tables']['tasks']"
```
**Observe:** rows 3 -> 4 and a new table hash. `GET /diff?a=&b=` between
two coreapps reports which rows differ.

---

## Level 3 - The informer and recovery (15 min)

3.1 **Kill a container behind the API's back.**
```bash
docker exec sandbox-api-dind-1 docker kill sbx-$ID-web
curl -s localhost:9000/sandboxes/$ID | j "['state'],['detail']"
```
**Observe:** `DEGRADED` within a second, `detail.dead` names the
container with its exit code. `POST .../start` -> `READY` and the page
answers again. **Teaches:** the API *watches* the engine; v1 would have
believed READY until an exec failed.

3.2 **Remove it entirely.** `docker exec sandbox-api-dind-1 docker rm -f
sbx-$ID-web`. The sandbox becomes `DELETED` and its port returns to the
pool (`GET /status`). Its events remain readable.

3.3 **TTL across a restart.** Create `blank` with `ttl_seconds: 40`, note
`deadline_wall`, then `docker compose restart api`. After the restart the
deadline is unchanged and the sandbox is reaped on time. **Teaches:** the
monotonic deadline is re-derived from the wall deadline at boot.

3.4 **Engine restart.** `docker compose restart dind` (takes ~20 s). All
sandboxes reconcile to `STOPPED` (their containers stopped with the
daemon). `start` them. **Teaches:** the DB caches *metadata*; the engine
is the truth about *existence*.

3.5 **Stop the engine while creating.** Start a `coreapp` create and
immediately `docker pause sandbox-api-dind-1` for 10 s, then unpause.
**Observe:** the create fails cleanly (FAILED with the error), the port
is freed, nothing leaks (`docker exec sandbox-api-dind-1 docker ps -a`).

---

## Level 4 - Isolation (10 min)

4.1 **gVisor.**
```bash
curl -s -X POST localhost:9000/sandboxes -H 'content-type: application/json' \
  -d '{"name":"gv","template":"web","isolation":"gvisor"}'
curl -s -X POST localhost:9000/sandboxes/$GID/exec -H 'content-type: application/json' -d '{"cmd":["uname","-a"]}'
curl -s -X POST localhost:9000/sandboxes/$GID/exec -H 'content-type: application/json' -d '{"cmd":["dmesg"]}'
```
**Observe:** kernel `4.19.0-gvisor`, a dmesg full of gVisor's jokes, and
the page still served through the identity port. `containers` shows
`runtime: runsc`. **Teaches:** the workload now talks to a userspace
kernel; the host kernel sees a narrow, seccomp-filtered process.

4.2 **Compare.** Time `coreapp` creation with `container` vs `gvisor`
(second create of each, images cached). Expect roughly 2x under gVisor.
`GET /sandboxes/{id}/stats` shows the cgroup view in both cases.

4.3 **Not everything fits.** `desktop` with `isolation: gvisor` -> 422;
templates declare what they support.

---

## Level 5 - The desktop and the GUI API (15 min)

5.1 **Create** `{"name":"d1","template":"desktop"}`. First time: the
3.9 GB base image is pulled into dind and a layer with Chromium/xdotool is
built (several minutes). Open the URL: a full XFCE desktop in your tab.

5.2 **Look.** `GET /sandboxes/$D/gui/display`, `GET .../gui/windows`,
`GET .../screenshot > shot.png`.

5.3 **Act.** Launch a browser as the desktop user through a session:
```bash
curl -s -X POST localhost:9000/sandboxes/$D/sessions -H 'content-type: application/json' \
  -d '{"cmd":["su","abc","-c","DISPLAY=:1 HOME=/config /usr/local/bin/chromium-browser about:blank"]}'
```
then drive it:
```bash
curl -s -X POST localhost:9000/sandboxes/$D/gui -H 'content-type: application/json' -d '{"type":"key","keys":"ctrl+l"}'
curl -s -X POST localhost:9000/sandboxes/$D/gui -H 'content-type: application/json' -d '{"type":"type","text":"http://example.com/"}'
curl -s -X POST localhost:9000/sandboxes/$D/gui -H 'content-type: application/json' -d '{"type":"key","keys":"Return"}'
curl -s -o shot2.png localhost:9000/sandboxes/$D/screenshot
```
**Observe:** the window title in `gui/windows` becomes `Example Domain -
Chromium`; the screenshot shows the page; every action is in the
sandbox's event log. **Teaches:** this is the whole loop a computer-use
agent needs - look, act, look again - exposed as four HTTP calls.

5.4 **Click by coordinates.** Use `gui/windows` geometry to compute a
point, `{"type":"click","x":..,"y":..}`, screenshot. Try `scroll`,
`drag`, `rightclick`.

---

## Level 6 - Expert: break it and watch it hold (20 min)

6.1 **Herd.** Submit 12 creates in a loop; the 13th gets 429 `capacity
reached` (counts engine sandboxes *plus* rows still CREATING - the
simulator found the version that did not).

6.2 **Idempotency under retry.** POST the same create 5 times in parallel
(`&`): one 201, the rest 200 with the same id - or 409 if you change the
template in one of them.

6.3 **The 20 ms window.** Read EDGE-CASES #17 and IMPLEMENTATION bug 8,
then try to reproduce it on the *old* behaviour: comment out the
`in_flight.add` in `create()`, rebuild, create 9 sandboxes back to back
while a resync runs (set `RESYNC_INTERVAL=1`). Watch one get "rolled back
interrupted create" in its events. Restore the line.

6.4 **Run the simulator instead.** `cd ../sandbox-sim && scripts\run.ps1
-Seeds 1000`. Then inject a bug of your own into `core.py` (say, skip
`set_port(sid, None)` on failure) and run again: a seed fails within
seconds with a replayable log. **Teaches:** why the core is written
without threads or clocks.

6.5 **SSE resume.** `curl -N "localhost:9000/events?cursor=0&follow=1"`,
note an `id:` line, kill curl, reconnect with `cursor=<id>`: only newer
events. The console's live log does exactly this.

// sbx-agent - the in-sandbox runtime agent.
//
// One static binary, three modes:
//
//	init   PID 1 inside a sandbox container. Supervises the real workload
//	       (forwards signals, reaps zombies, exits with its code) AND serves
//	       the agent HTTP API on :7700. This is the default injection mode.
//	serve  Serve the agent API only (no child). Used when PID 1 must stay the
//	       image's own init (e.g. s6-overlay desktops): the API starts it with
//	       `docker exec -d`.
//	proxy  Engine-side relay, runs INSIDE the dind container. Reverse-proxies
//	       /sbx/<inner-ip>/... to the agent of that container (the dind netns
//	       can reach every inner bridge directly) and offers privileged node
//	       operations (/node/exec, /node/info) behind a token.
//
// Everything the control plane does to a sandbox at runtime (exec sessions,
// files, stats, fingerprints, GUI actions) goes through this agent, never
// through `docker exec`. That is what makes the API runtime-agnostic: a
// microVM backend only has to expose the same HTTP surface over vsock.
package main

import (
	"fmt"
	"os"
)

const version = "1.0.0"

func usage() {
	fmt.Fprintf(os.Stderr, `sbx-agent %s
usage:
  sbx-agent init  [-- cmd args...]   run as PID 1: serve API + supervise cmd
  sbx-agent serve                    serve API only
  sbx-agent proxy                    engine-side relay + node ops
  sbx-agent version

env (agent modes):
  SBX_AGENT_ADDR         listen address            (default 0.0.0.0:7700)
  SBX_AGENT_TOKEN        bearer token, empty=open  (default empty)
  SBX_AGENT_BUFFER       per-session ring buffer   (default 1048576 bytes)
  SBX_AGENT_SESSION_TTL  keep exited sessions, s   (default 3600)
  SBX_AGENT_MAX_SESSIONS                           (default 64)
  SBX_AGENT_FS_ROOT      jail for /fs endpoints    (default / = no jail)
  SBX_DISPLAY            X display for /gui        (default :1)
env (proxy mode):
  SBX_NODE_ADDR          listen address            (default 0.0.0.0:7799)
  SBX_NODE_TOKEN         token for /node/*         (required)
`, version)
	os.Exit(2)
}

func main() {
	if len(os.Args) < 2 {
		usage()
	}
	switch os.Args[1] {
	case "init":
		runInit(os.Args[2:])
	case "serve":
		runServe()
	case "proxy":
		runProxy()
	case "version":
		fmt.Println(version)
	default:
		usage()
	}
}

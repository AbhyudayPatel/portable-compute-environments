package main

import (
	"bytes"
	"context"
	"log"
	"net"
	"net/http"
	"net/http/httputil"
	"net/url"
	"os"
	"os/exec"
	"strings"
	"syscall"
	"time"
)

// ---------------------------------------------------------------------------
// Proxy mode: runs inside the dind container (the "node").
//
//   /sbx/<inner-ip>/<rest>   reverse-proxy to http://<inner-ip>:7700/<rest>
//                            The dind netns hosts every inner bridge, so it
//                            can dial any sandbox container directly. The
//                            control plane only ever needs ONE published
//                            engine port (7799) to reach every agent.
//   /node/exec               run a command in the dind namespace (mount,
//                            iptables, cp - the privileged building blocks
//                            for checkpoints and egress policy)
//   /node/info               kernel / cgroup / runtimes / disk facts
//
// /node/* requires X-Node-Token. The relay passes the caller's
// Authorization header through to the sandbox agent unchanged.
// ---------------------------------------------------------------------------

func runProxy() {
	addr := envOr("SBX_NODE_ADDR", "0.0.0.0:7799")
	token := os.Getenv("SBX_NODE_TOKEN")
	if token == "" {
		log.Fatal("sbx-agent proxy: SBX_NODE_TOKEN is required")
	}
	nodeAuth := func(next http.HandlerFunc) http.HandlerFunc {
		return func(w http.ResponseWriter, r *http.Request) {
			if r.Header.Get("X-Node-Token") != token {
				errJSON(w, 401, "bad or missing X-Node-Token")
				return
			}
			next(w, r)
		}
	}
	transport := &http.Transport{
		DialContext:           (&net.Dialer{Timeout: 3 * time.Second}).DialContext,
		ResponseHeaderTimeout: 0, // follow streams may wait for a long time
		MaxIdleConnsPerHost:   16,
		IdleConnTimeout:       60 * time.Second,
	}
	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, 200, map[string]any{"ok": true, "mode": "proxy", "version": version})
	})
	mux.HandleFunc("/sbx/", func(w http.ResponseWriter, r *http.Request) {
		// /sbx/<ip>/<rest...>
		rest := strings.TrimPrefix(r.URL.Path, "/sbx/")
		ip, path, _ := strings.Cut(rest, "/")
		if net.ParseIP(ip) == nil {
			errJSON(w, 400, "bad inner ip in path")
			return
		}
		target := &url.URL{Scheme: "http", Host: ip + ":7700"}
		rp := &httputil.ReverseProxy{
			Director: func(req *http.Request) {
				req.URL.Scheme = target.Scheme
				req.URL.Host = target.Host
				req.URL.Path = "/" + path
				req.URL.RawQuery = r.URL.RawQuery
				req.Host = target.Host
			},
			Transport:     transport,
			FlushInterval: -1, // stream every write immediately (NDJSON follow)
			ErrorHandler: func(w http.ResponseWriter, req *http.Request, err error) {
				errJSON(w, 502, "agent unreachable at "+ip+": "+err.Error())
			},
		}
		rp.ServeHTTP(w, r)
	})
	mux.HandleFunc("POST /node/exec", nodeAuth(handleNodeExec))
	mux.HandleFunc("GET /node/info", nodeAuth(handleNodeInfo))
	mux.HandleFunc("POST /node/tarstream", nodeAuth(handleNodeTarStream))
	mux.HandleFunc("PUT /node/untarstream", nodeAuth(handleNodeUntarStream))
	log.Printf("sbx-agent %s proxy: serving on %s", version, addr)
	log.Fatal(http.ListenAndServe(addr, mux))
}

func handleNodeExec(w http.ResponseWriter, r *http.Request) {
	var req struct {
		Cmd     []string `json:"cmd"`
		Stdin   string   `json:"stdin"`
		Timeout int      `json:"timeout_seconds"`
	}
	if err := readJSON(r, &req); err != nil || len(req.Cmd) == 0 {
		errJSON(w, 400, "cmd required")
		return
	}
	if req.Timeout <= 0 {
		req.Timeout = 60
	}
	ctx, cancel := context.WithTimeout(context.Background(), time.Duration(req.Timeout)*time.Second)
	defer cancel()
	cmd := exec.CommandContext(ctx, req.Cmd[0], req.Cmd[1:]...)
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	if req.Stdin != "" {
		cmd.Stdin = strings.NewReader(req.Stdin)
	}
	var out, errb bytes.Buffer
	cmd.Stdout = &capWriter{buf: &out, max: 1 << 20}
	cmd.Stderr = &capWriter{buf: &errb, max: 1 << 20}
	err := cmd.Run()
	code := 0
	if err != nil {
		if ee, ok := err.(*exec.ExitError); ok {
			code = ee.ExitCode()
		} else {
			code = 127
			errb.WriteString(err.Error())
		}
	}
	writeJSON(w, 200, map[string]any{
		"exit_code": code, "timed_out": ctx.Err() == context.DeadlineExceeded,
		"stdout": out.String(), "stderr": errb.String(),
	})
}

type capWriter struct {
	buf *bytes.Buffer
	max int
}

func (c *capWriter) Write(p []byte) (int, error) {
	room := c.max - c.buf.Len()
	if room > 0 {
		if len(p) > room {
			c.buf.Write(p[:room])
		} else {
			c.buf.Write(p)
		}
	}
	return len(p), nil
}

func handleNodeInfo(w http.ResponseWriter, r *http.Request) {
	var uts syscall.Utsname
	_ = syscall.Uname(&uts)
	cg := 1
	if _, err := os.Stat("/sys/fs/cgroup/cgroup.controllers"); err == nil {
		cg = 2
	}
	which := func(name string) string {
		p, err := exec.LookPath(name)
		if err != nil {
			return ""
		}
		return p
	}
	fsTypes := ""
	if b, err := os.ReadFile("/proc/filesystems"); err == nil {
		fsTypes = string(b)
	}
	writeJSON(w, 200, map[string]any{
		"kernel":        utsStr(uts.Release),
		"machine":       utsStr(uts.Machine),
		"cgroup":        cg,
		"runsc":         which("runsc"),
		"iptables":      which("iptables"),
		"overlay":       strings.Contains(fsTypes, "overlay"),
		"btrfs":         strings.Contains(fsTypes, "btrfs"),
		"xfs":           strings.Contains(fsTypes, "xfs"),
		"disk":          diskUsage("/var/lib/docker"),
		"agent_version": version,
	})
}

func utsStr(f [65]int8) string {
	b := make([]byte, 0, 65)
	for _, c := range f {
		if c == 0 {
			break
		}
		b = append(b, byte(c))
	}
	return string(b)
}

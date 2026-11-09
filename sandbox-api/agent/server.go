package main

import (
	"context"
	"encoding/json"
	"io"
	"log"
	"net/http"
	"os"
	"strconv"
	"strings"
	"syscall"
	"time"
)

// ---------------------------------------------------------------------------
// The agent HTTP API (modes init/serve). Plain HTTP/1.1 + JSON, streams are
// newline-delimited JSON over chunked responses, so any language can talk
// to it with nothing but an HTTP client.
// ---------------------------------------------------------------------------

type server struct {
	mux    *http.ServeMux
	srv    *http.Server
	sm     *sessionManager
	token  string
	fsRoot string
	start  time.Time
}

func envOr(k, def string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return def
}

func envInt(k string, def int) int {
	if v := os.Getenv(k); v != "" {
		if n, err := strconv.Atoi(v); err == nil {
			return n
		}
	}
	return def
}

func newServer() *server {
	s := &server{
		mux:    http.NewServeMux(),
		token:  os.Getenv("SBX_AGENT_TOKEN"),
		fsRoot: envOr("SBX_AGENT_FS_ROOT", "/"),
		start:  time.Now(),
	}
	s.sm = newSessionManager(
		envInt("SBX_AGENT_MAX_SESSIONS", 64),
		envInt("SBX_AGENT_BUFFER", 1<<20),
		time.Duration(envInt("SBX_AGENT_SESSION_TTL", 3600))*time.Second,
	)
	s.routes()
	s.srv = &http.Server{
		Addr:    envOr("SBX_AGENT_ADDR", "0.0.0.0:7700"),
		Handler: s.mux,
		// no ReadTimeout/WriteTimeout: streams are long-lived by design
	}
	return s
}

func (s *server) routes() {
	m := s.mux
	a := s.auth
	m.HandleFunc("GET /healthz", s.handleHealth)
	m.HandleFunc("GET /sessions", a(s.handleSessionsList))
	m.HandleFunc("POST /sessions", a(s.handleSessionCreate))
	m.HandleFunc("GET /sessions/{id}", a(s.handleSessionGet))
	m.HandleFunc("GET /sessions/{id}/stream", a(s.handleSessionStream))
	m.HandleFunc("POST /sessions/{id}/stdin", a(s.handleSessionStdin))
	m.HandleFunc("POST /sessions/{id}/signal", a(s.handleSessionSignal))
	m.HandleFunc("POST /sessions/{id}/resize", a(s.handleSessionResize))
	m.HandleFunc("DELETE /sessions/{id}", a(s.handleSessionDelete))
	m.HandleFunc("POST /exec", a(s.handleExec))
	m.HandleFunc("GET /ps", a(s.handlePs))
	m.HandleFunc("GET /stats", a(s.handleStats))
	m.HandleFunc("GET /fs/list", a(s.handleFsList))
	m.HandleFunc("GET /fs/stat", a(s.handleFsStat))
	m.HandleFunc("GET /fs/read", a(s.handleFsRead))
	m.HandleFunc("PUT /fs/file", a(s.handleFsWrite))
	m.HandleFunc("POST /fs/mkdir", a(s.handleFsMkdir))
	m.HandleFunc("DELETE /fs", a(s.handleFsDelete))
	m.HandleFunc("GET /fs/get", a(s.handleFsGet))
	m.HandleFunc("PUT /fs/put", a(s.handleFsPut))
	m.HandleFunc("GET /fingerprint", a(s.handleFingerprint))
	m.HandleFunc("GET /gui/display", a(s.handleGuiDisplay))
	m.HandleFunc("POST /gui/screenshot", a(s.handleGuiScreenshot))
	m.HandleFunc("GET /gui/screenshot", a(s.handleGuiScreenshot))
	m.HandleFunc("POST /gui/action", a(s.handleGuiAction))
	m.HandleFunc("GET /gui/windows", a(s.handleGuiWindows))
}

func (s *server) auth(next http.HandlerFunc) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if s.token != "" {
			got := strings.TrimPrefix(r.Header.Get("Authorization"), "Bearer ")
			if got != s.token {
				errJSON(w, 401, "bad or missing bearer token")
				return
			}
		}
		next(w, r)
	}
}

func (s *server) listenAndServe() {
	log.Printf("sbx-agent %s: serving on %s (fs root %s, token %v)",
		version, s.srv.Addr, s.fsRoot, s.token != "")
	if err := s.srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
		log.Printf("sbx-agent: listen failed: %v", err)
	}
}

func (s *server) shutdown() {
	s.sm.killAll()
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	_ = s.srv.Shutdown(ctx)
}

// -- helpers -----------------------------------------------------------------

func writeJSON(w http.ResponseWriter, code int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(v)
}

func errJSON(w http.ResponseWriter, code int, msg string) {
	writeJSON(w, code, map[string]any{"error": msg})
}

func readJSON(r *http.Request, v any) error {
	defer r.Body.Close()
	return json.NewDecoder(io.LimitReader(r.Body, 4<<20)).Decode(v)
}

func qInt(r *http.Request, k string, def int) int {
	if v := r.URL.Query().Get(k); v != "" {
		if n, err := strconv.Atoi(v); err == nil {
			return n
		}
	}
	return def
}

// -- handlers ----------------------------------------------------------------

func (s *server) handleHealth(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, 200, map[string]any{
		"ok": true, "version": version, "pid": os.Getpid(), "pid1": os.Getpid() == 1,
		"uptime_seconds": time.Since(s.start).Seconds(), "fs_root": s.fsRoot,
		"sessions": len(s.sm.list()),
	})
}

func (s *server) handleSessionsList(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, 200, map[string]any{"sessions": s.sm.list()})
}

func (s *server) handleSessionCreate(w http.ResponseWriter, r *http.Request) {
	var req sessionReq
	if err := readJSON(r, &req); err != nil {
		errJSON(w, 400, "bad json: "+err.Error())
		return
	}
	sess, err := s.sm.start(req)
	if err != nil {
		errJSON(w, 400, err.Error())
		return
	}
	writeJSON(w, 201, sess.snapshot())
}

func (s *server) lookup(w http.ResponseWriter, r *http.Request) (*session, bool) {
	sess, ok := s.sm.get(r.PathValue("id"))
	if !ok {
		errJSON(w, 404, "no such session")
		return nil, false
	}
	return sess, true
}

func (s *server) handleSessionGet(w http.ResponseWriter, r *http.Request) {
	if sess, ok := s.lookup(w, r); ok {
		writeJSON(w, 200, sess.snapshot())
	}
}

// GET /sessions/{id}/stream?since=N&follow=1&timeout=30
//
// Emits NDJSON frames with seq > since. With follow=1 it keeps the
// connection open until the exit frame has been sent or `timeout` seconds
// pass without the session ending (the client simply calls again with the
// last seq it saw). Without follow it returns what is buffered and closes.
func (s *server) handleSessionStream(w http.ResponseWriter, r *http.Request) {
	sess, ok := s.lookup(w, r)
	if !ok {
		return
	}
	since := uint64(qInt(r, "since", 0))
	follow := r.URL.Query().Get("follow") == "1"
	timeout := time.Duration(qInt(r, "timeout", 30)) * time.Second
	flusher, _ := w.(http.Flusher)
	w.Header().Set("Content-Type", "application/x-ndjson")
	w.Header().Set("Cache-Control", "no-cache")
	w.Header().Set("X-Accel-Buffering", "no")
	w.WriteHeader(200)
	enc := json.NewEncoder(w)
	deadline := time.Now().Add(timeout)
	first := true
	for {
		frames, gap, oldest, changed := sess.ring.since(since)
		if first && gap {
			_ = enc.Encode(map[string]any{"gap": true, "oldest": oldest, "since": since})
		}
		first = false
		sawExit := false
		for _, f := range frames {
			_ = enc.Encode(f)
			since = f.Seq
			if f.Stream == "x" {
				sawExit = true
			}
		}
		if flusher != nil {
			flusher.Flush()
		}
		if !follow || sawExit {
			return
		}
		if sess.isExited() && since >= sess.ring.lastSeq() {
			return
		}
		remaining := time.Until(deadline)
		if remaining <= 0 {
			return
		}
		select {
		case <-changed:
		case <-time.After(remaining):
			return
		case <-r.Context().Done():
			return
		}
	}
}

func (s *server) handleSessionStdin(w http.ResponseWriter, r *http.Request) {
	sess, ok := s.lookup(w, r)
	if !ok {
		return
	}
	data, err := io.ReadAll(io.LimitReader(r.Body, 4<<20))
	if err != nil {
		errJSON(w, 400, err.Error())
		return
	}
	if len(data) > 0 {
		if err := sess.writeStdin(data); err != nil {
			errJSON(w, 409, err.Error())
			return
		}
	}
	if r.URL.Query().Get("eof") == "1" {
		_ = sess.closeStdin()
	}
	writeJSON(w, 200, map[string]any{"written": len(data)})
}

var signalNames = map[string]syscall.Signal{
	"TERM": syscall.SIGTERM, "KILL": syscall.SIGKILL, "INT": syscall.SIGINT,
	"HUP": syscall.SIGHUP, "QUIT": syscall.SIGQUIT, "USR1": syscall.SIGUSR1,
	"USR2": syscall.SIGUSR2, "STOP": syscall.SIGSTOP, "CONT": syscall.SIGCONT,
}

func (s *server) handleSessionSignal(w http.ResponseWriter, r *http.Request) {
	sess, ok := s.lookup(w, r)
	if !ok {
		return
	}
	var req struct {
		Signal string `json:"signal"`
	}
	_ = readJSON(r, &req)
	name := strings.ToUpper(strings.TrimPrefix(req.Signal, "SIG"))
	sig, ok := signalNames[name]
	if !ok {
		if n, err := strconv.Atoi(name); err == nil {
			sig = syscall.Signal(n)
		} else {
			errJSON(w, 400, "unknown signal "+req.Signal)
			return
		}
	}
	if sess.isExited() {
		errJSON(w, 409, "session already exited")
		return
	}
	if err := sess.signal(sig); err != nil {
		errJSON(w, 500, err.Error())
		return
	}
	writeJSON(w, 200, map[string]any{"signalled": name})
}

func (s *server) handleSessionResize(w http.ResponseWriter, r *http.Request) {
	sess, ok := s.lookup(w, r)
	if !ok {
		return
	}
	var req struct {
		Cols int `json:"cols"`
		Rows int `json:"rows"`
	}
	if err := readJSON(r, &req); err != nil {
		errJSON(w, 400, err.Error())
		return
	}
	if err := sess.resize(req.Cols, req.Rows); err != nil {
		errJSON(w, 409, err.Error())
		return
	}
	writeJSON(w, 200, map[string]any{"cols": req.Cols, "rows": req.Rows})
}

func (s *server) handleSessionDelete(w http.ResponseWriter, r *http.Request) {
	if !s.sm.remove(r.PathValue("id")) {
		errJSON(w, 404, "no such session")
		return
	}
	w.WriteHeader(204)
}

// POST /exec {cmd, env, cwd, timeout_seconds, max_bytes}
//
// Convenience: start a session, wait for it to finish, return the result
// in one shot. The session stays retrievable afterwards (full output in
// its ring), so "truncated" here only refers to this response.
func (s *server) handleExec(w http.ResponseWriter, r *http.Request) {
	var req struct {
		sessionReq
		MaxBytes int `json:"max_bytes"`
	}
	if err := readJSON(r, &req); err != nil {
		errJSON(w, 400, "bad json: "+err.Error())
		return
	}
	if req.MaxBytes <= 0 {
		req.MaxBytes = 64 * 1024
	}
	sess, err := s.sm.start(req.sessionReq)
	if err != nil {
		errJSON(w, 400, err.Error())
		return
	}
	var out, errb []byte
	truncated := false
	var since uint64
	for {
		frames, _, _, changed := sess.ring.since(since)
		done := false
		for _, f := range frames {
			since = f.Seq
			switch f.Stream {
			case "o":
				out, truncated = appendCapped(out, f.Data, req.MaxBytes, truncated)
			case "e":
				errb, truncated = appendCapped(errb, f.Data, req.MaxBytes, truncated)
			case "x":
				done = true
			}
		}
		if done {
			break
		}
		select {
		case <-changed:
		case <-r.Context().Done():
			return
		}
	}
	snap := sess.snapshot()
	writeJSON(w, 200, map[string]any{
		"session_id": snap.ID, "exit_code": snap.ExitCode, "timed_out": snap.TimedOut,
		"truncated": truncated, "stdout": string(out), "stderr": string(errb),
		"bytes_total": snap.BytesOut,
	})
}

func appendCapped(dst, data []byte, max int, truncated bool) ([]byte, bool) {
	room := max - len(dst)
	if room <= 0 {
		return dst, true
	}
	if len(data) > room {
		return append(dst, data[:room]...), true
	}
	return append(dst, data...), truncated
}

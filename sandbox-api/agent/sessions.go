package main

import (
	"crypto/rand"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"sync"
	"sync/atomic"
	"syscall"
	"time"

	"github.com/creack/pty"
)

// ---------------------------------------------------------------------------
// Frames and the per-session ring buffer
//
// Every byte a session produces becomes a frame with a monotonically
// increasing sequence number. Frames live in a bounded ring (default 1 MiB
// per session). A client that disconnects - or a control plane that
// restarts - comes back with `?since=<last seq it saw>` and continues
// exactly where it left off. If the ring had to evict, the client is told
// there is a gap instead of silently getting a hole.
// ---------------------------------------------------------------------------

type frame struct {
	Seq    uint64 `json:"seq"`
	Stream string `json:"s"`              // "o" stdout, "e" stderr, "x" exit
	Data   []byte `json:"d,omitempty"`    // JSON encodes []byte as base64
	Code   *int   `json:"code,omitempty"` // only on "x" frames
}

type ring struct {
	mu      sync.Mutex
	frames  []frame
	bytes   int
	capB    int
	nextSeq uint64
	dropped uint64
	changed chan struct{}
}

func newRing(capB int) *ring {
	return &ring{capB: capB, nextSeq: 1, changed: make(chan struct{})}
}

func (r *ring) append(f frame) uint64 {
	r.mu.Lock()
	f.Seq = r.nextSeq
	r.nextSeq++
	r.frames = append(r.frames, f)
	r.bytes += len(f.Data)
	for r.bytes > r.capB && len(r.frames) > 1 {
		r.bytes -= len(r.frames[0].Data)
		r.frames[0].Data = nil
		r.frames = r.frames[1:]
		r.dropped++
	}
	ch := r.changed
	r.changed = make(chan struct{})
	r.mu.Unlock()
	close(ch) // wake every waiter
	return f.Seq
}

// since returns the frames with seq > since, whether frames between
// `since` and the oldest retained one were evicted, and a channel that
// closes on the next append (for follow-mode waiting).
func (r *ring) since(since uint64) (out []frame, gap bool, oldest uint64, changed <-chan struct{}) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if len(r.frames) > 0 {
		oldest = r.frames[0].Seq
		if since+1 < oldest {
			gap = true
		}
	}
	for _, f := range r.frames {
		if f.Seq > since {
			out = append(out, f)
		}
	}
	return out, gap, oldest, r.changed
}

func (r *ring) lastSeq() uint64 {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.nextSeq - 1
}

func (r *ring) droppedCount() uint64 {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.dropped
}

// ---------------------------------------------------------------------------
// Sessions
// ---------------------------------------------------------------------------

type session struct {
	ID        string     `json:"id"`
	Cmd       []string   `json:"cmd"`
	TTY       bool       `json:"tty"`
	Pid       int        `json:"pid"`
	State     string     `json:"state"` // running | exited
	ExitCode  *int       `json:"exit_code"`
	TimedOut  bool       `json:"timed_out"`
	StartedAt time.Time  `json:"started_at"`
	EndedAt   *time.Time `json:"ended_at,omitempty"`
	LastSeq   uint64     `json:"last_seq"`
	Dropped   uint64     `json:"dropped_frames"`
	BytesOut  int64      `json:"bytes_total"`
	Timeout   int        `json:"timeout_seconds"`

	mu     sync.Mutex
	ring   *ring
	proc   *os.Process
	stdin  io.WriteCloser
	ptmx   *os.File
	done   chan struct{}
	pumps  sync.WaitGroup
	closeO sync.Once
}

type sessionReq struct {
	Cmd     []string          `json:"cmd"`
	Env     map[string]string `json:"env"`
	Cwd     string            `json:"cwd"`
	TTY     bool              `json:"tty"`
	Cols    int               `json:"cols"`
	Rows    int               `json:"rows"`
	Timeout int               `json:"timeout_seconds"` // 0 = none
	Buffer  int               `json:"buffer_bytes"`    // 0 = default
	Stdin   bool              `json:"stdin"`           // keep stdin writable (non-tty)
}

type sessionManager struct {
	mu       sync.Mutex
	sessions map[string]*session
	maxN     int
	defBuf   int
	ttl      time.Duration
}

func newSessionManager(maxN, defBuf int, ttl time.Duration) *sessionManager {
	return &sessionManager{sessions: map[string]*session{}, maxN: maxN, defBuf: defBuf, ttl: ttl}
}

func randID() string {
	b := make([]byte, 6)
	_, _ = rand.Read(b)
	return hex.EncodeToString(b)
}

func (m *sessionManager) start(req sessionReq) (*session, error) {
	if len(req.Cmd) == 0 {
		return nil, errors.New("cmd required")
	}
	m.mu.Lock()
	m.gcLocked()
	if len(m.sessions) >= m.maxN {
		m.mu.Unlock()
		return nil, fmt.Errorf("too many sessions (max %d); delete finished ones", m.maxN)
	}
	m.mu.Unlock()

	buf := req.Buffer
	if buf <= 0 {
		buf = m.defBuf
	}
	s := &session{
		ID: randID(), Cmd: req.Cmd, TTY: req.TTY, State: "running",
		StartedAt: time.Now(), Timeout: req.Timeout,
		ring: newRing(buf), done: make(chan struct{}),
	}

	cmd := exec.Command(req.Cmd[0], req.Cmd[1:]...)
	cmd.Env = os.Environ()
	for k, v := range req.Env {
		cmd.Env = append(cmd.Env, k+"="+v)
	}
	if req.Cwd != "" {
		cmd.Dir = req.Cwd
	}

	if req.TTY {
		cols, rows := req.Cols, req.Rows
		if cols <= 0 {
			cols = 120
		}
		if rows <= 0 {
			rows = 40
		}
		cmd.Env = append(cmd.Env, "TERM=xterm-256color")
		// pty.StartWithSize sets Setsid+Setctty: the child becomes a
		// session leader, so kill(-pid) reaches its whole group.
		ptmx, err := pty.StartWithSize(cmd, &pty.Winsize{Cols: uint16(cols), Rows: uint16(rows)})
		if err != nil {
			return nil, err
		}
		s.ptmx, s.stdin = ptmx, ptmx
		s.pumps.Add(1)
		go s.pump(ptmx, "o")
	} else {
		cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
		stdout, err := cmd.StdoutPipe()
		if err != nil {
			return nil, err
		}
		stderr, err := cmd.StderrPipe()
		if err != nil {
			return nil, err
		}
		var stdin io.WriteCloser
		if req.Stdin {
			if stdin, err = cmd.StdinPipe(); err != nil {
				return nil, err
			}
		}
		if err := cmd.Start(); err != nil {
			return nil, err
		}
		s.stdin = stdin
		s.pumps.Add(2)
		go s.pump(stdout, "o")
		go s.pump(stderr, "e")
	}
	s.proc = cmd.Process
	s.Pid = cmd.Process.Pid
	// NOTE: we never call cmd.Wait(); the reaper owns wait4() (see reaper.go)
	exited := theReaper.register(s.Pid)

	m.mu.Lock()
	m.sessions[s.ID] = s
	m.mu.Unlock()

	go s.waitExit(exited)
	if req.Timeout > 0 {
		go s.enforceTimeout(time.Duration(req.Timeout) * time.Second)
	}
	return s, nil
}

func (s *session) pump(r io.Reader, stream string) {
	defer s.pumps.Done()
	buf := make([]byte, 32*1024)
	for {
		n, err := r.Read(buf)
		if n > 0 {
			data := make([]byte, n)
			copy(data, buf[:n])
			s.ring.append(frame{Stream: stream, Data: data})
			atomic.AddInt64(&s.BytesOut, int64(n))
		}
		if err != nil {
			return // EOF, or EIO from a pty whose child is gone - both normal
		}
	}
}

func (s *session) waitExit(exited <-chan int) {
	code := <-exited
	// Let the pumps drain what the process wrote right before exiting. A
	// background grandchild can keep the pipe open forever, so bound it.
	drained := make(chan struct{})
	go func() { s.pumps.Wait(); close(drained) }()
	select {
	case <-drained:
	case <-time.After(1500 * time.Millisecond):
	}
	now := time.Now()
	s.mu.Lock()
	s.State = "exited"
	s.ExitCode = &code
	s.EndedAt = &now
	s.mu.Unlock()
	s.ring.append(frame{Stream: "x", Code: &code})
	close(s.done)
	s.closeIO()
}

func (s *session) closeIO() {
	s.closeO.Do(func() {
		if s.ptmx != nil {
			_ = s.ptmx.Close()
		} else if s.stdin != nil {
			_ = s.stdin.Close()
		}
	})
}

func (s *session) enforceTimeout(d time.Duration) {
	select {
	case <-s.done:
		return
	case <-time.After(d):
	}
	s.mu.Lock()
	s.TimedOut = true
	s.mu.Unlock()
	_ = s.signal(syscall.SIGTERM)
	select {
	case <-s.done:
		return
	case <-time.After(5 * time.Second):
	}
	_ = s.signal(syscall.SIGKILL)
}

// signal targets the whole process group so pipelines and children die
// with the leader (this is what `docker exec` could never do for us).
func (s *session) signal(sig syscall.Signal) error {
	if err := syscall.Kill(-s.Pid, sig); err != nil {
		return syscall.Kill(s.Pid, sig)
	}
	return nil
}

func (s *session) writeStdin(data []byte) error {
	if s.stdin == nil {
		return errors.New("session has no stdin (create with stdin:true or tty:true)")
	}
	_, err := s.stdin.Write(data)
	return err
}

func (s *session) closeStdin() error {
	if s.stdin == nil {
		return nil
	}
	if s.TTY {
		_, err := s.stdin.Write([]byte{4}) // Ctrl-D
		return err
	}
	return s.stdin.Close()
}

func (s *session) resize(cols, rows int) error {
	if s.ptmx == nil {
		return errors.New("not a tty session")
	}
	return pty.Setsize(s.ptmx, &pty.Winsize{Cols: uint16(cols), Rows: uint16(rows)})
}

// snapshot returns a copy safe to JSON-encode.
func (s *session) snapshot() session {
	s.mu.Lock()
	defer s.mu.Unlock()
	return session{
		ID: s.ID, Cmd: s.Cmd, TTY: s.TTY, Pid: s.Pid, State: s.State,
		ExitCode: s.ExitCode, TimedOut: s.TimedOut, StartedAt: s.StartedAt,
		EndedAt: s.EndedAt, Timeout: s.Timeout,
		LastSeq: s.ring.lastSeq(), Dropped: s.ring.droppedCount(),
		BytesOut: atomic.LoadInt64(&s.BytesOut),
	}
}

func (s *session) isExited() bool {
	select {
	case <-s.done:
		return true
	default:
		return false
	}
}

func (m *sessionManager) get(id string) (*session, bool) {
	m.mu.Lock()
	defer m.mu.Unlock()
	s, ok := m.sessions[id]
	return s, ok
}

func (m *sessionManager) list() []session {
	m.mu.Lock()
	m.gcLocked()
	out := make([]*session, 0, len(m.sessions))
	for _, s := range m.sessions {
		out = append(out, s)
	}
	m.mu.Unlock()
	res := make([]session, 0, len(out))
	for _, s := range out {
		res = append(res, s.snapshot())
	}
	return res
}

func (m *sessionManager) remove(id string) bool {
	m.mu.Lock()
	s, ok := m.sessions[id]
	if ok {
		delete(m.sessions, id)
	}
	m.mu.Unlock()
	if !ok {
		return false
	}
	if !s.isExited() {
		_ = s.signal(syscall.SIGKILL)
	}
	return true
}

// gcLocked forgets sessions that exited more than ttl ago. Caller holds mu.
func (m *sessionManager) gcLocked() {
	cutoff := time.Now().Add(-m.ttl)
	for id, s := range m.sessions {
		s.mu.Lock()
		old := s.State == "exited" && s.EndedAt != nil && s.EndedAt.Before(cutoff)
		s.mu.Unlock()
		if old {
			delete(m.sessions, id)
		}
	}
}

// killAll is used at shutdown: no session may outlive the agent.
func (m *sessionManager) killAll() {
	m.mu.Lock()
	all := make([]*session, 0, len(m.sessions))
	for _, s := range m.sessions {
		all = append(all, s)
	}
	m.mu.Unlock()
	for _, s := range all {
		if !s.isExited() {
			_ = s.signal(syscall.SIGKILL)
		}
	}
}

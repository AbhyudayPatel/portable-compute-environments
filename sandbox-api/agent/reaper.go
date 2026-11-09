package main

import (
	"os"
	"os/signal"
	"sync"
	"syscall"
)

// reaper is the single place that calls wait4(). As PID 1 every orphaned
// process in the container is re-parented to us, so SOMEONE must collect
// exit statuses or they stay zombies forever. But Go's exec.Cmd.Wait()
// also calls wait4(pid) - two waiters race and one gets ECHILD. The fix is
// the classic init pattern: one goroutine reaps everything with
// wait4(-1, WNOHANG) on every SIGCHLD and hands each status to whoever
// registered interest in that pid.
type reaper struct {
	mu        sync.Mutex
	waiters   map[int]chan int // pid -> receives exit code once
	unclaimed map[int]int      // exited before anyone registered
}

var theReaper = &reaper{
	waiters:   map[int]chan int{},
	unclaimed: map[int]int{},
}

// start must be called before the first child is spawned so no SIGCHLD is
// missed. Safe to call when not PID 1 (wait4(-1) still only sees our kids).
func (r *reaper) start() {
	sigchld := make(chan os.Signal, 16)
	signal.Notify(sigchld, syscall.SIGCHLD)
	go func() {
		for range sigchld {
			r.reapAll()
		}
	}()
}

func (r *reaper) reapAll() {
	for {
		var ws syscall.WaitStatus
		pid, err := syscall.Wait4(-1, &ws, syscall.WNOHANG, nil)
		if err == syscall.EINTR {
			continue
		}
		if pid <= 0 || err != nil {
			return
		}
		r.dispatch(pid, exitCodeOf(ws))
	}
}

func exitCodeOf(ws syscall.WaitStatus) int {
	switch {
	case ws.Exited():
		return ws.ExitStatus()
	case ws.Signaled():
		return 128 + int(ws.Signal())
	default:
		return -1
	}
}

func (r *reaper) dispatch(pid, code int) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if ch, ok := r.waiters[pid]; ok {
		ch <- code
		delete(r.waiters, pid)
		return
	}
	r.unclaimed[pid] = code
}

// register returns a channel that yields the exit code of pid exactly once.
// Handles the race where the process already exited.
func (r *reaper) register(pid int) <-chan int {
	ch := make(chan int, 1)
	r.mu.Lock()
	defer r.mu.Unlock()
	if code, ok := r.unclaimed[pid]; ok {
		delete(r.unclaimed, pid)
		ch <- code
		return ch
	}
	r.waiters[pid] = ch
	return ch
}

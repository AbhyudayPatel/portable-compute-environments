package main

import (
	"log"
	"os"
	"os/exec"
	"os/signal"
	"syscall"
	"time"
)

// runInit is the PID-1 path. The workload is started as a normal child with
// the container's stdio, every signal docker sends us (SIGTERM on stop,
// SIGINT, SIGHUP, ...) is forwarded to it, and when it exits the container
// exits with the same code - so wrapping an image with the agent changes
// nothing observable about its lifecycle, it only adds the API.
func runInit(args []string) {
	// accept both "init -- cmd" and "init cmd"
	if len(args) > 0 && args[0] == "--" {
		args = args[1:]
	}
	theReaper.start()
	srv := newServer()
	go srv.listenAndServe()

	if len(args) == 0 {
		// No workload: the container IS the agent (blank/exec-target sandboxes).
		log.Printf("sbx-agent init: no workload, serving API only")
		waitForTermination()
		srv.shutdown()
		os.Exit(0)
	}

	child := exec.Command(args[0], args[1:]...)
	child.Stdin, child.Stdout, child.Stderr = os.Stdin, os.Stdout, os.Stderr
	child.Env = os.Environ()
	if err := child.Start(); err != nil {
		log.Printf("sbx-agent init: cannot start workload %v: %v", args, err)
		os.Exit(127)
	}
	exited := theReaper.register(child.Process.Pid)
	log.Printf("sbx-agent init: workload pid %d started: %v", child.Process.Pid, args)

	sigs := make(chan os.Signal, 8)
	signal.Notify(sigs, syscall.SIGTERM, syscall.SIGINT, syscall.SIGHUP,
		syscall.SIGQUIT, syscall.SIGUSR1, syscall.SIGUSR2, syscall.SIGWINCH)
	for {
		select {
		case s := <-sigs:
			// forward to the workload only; sessions are the API's business
			_ = child.Process.Signal(s)
		case code := <-exited:
			log.Printf("sbx-agent init: workload exited with %d; shutting down", code)
			srv.shutdown()
			os.Exit(code)
		}
	}
}

// runServe serves the API without supervising anything. Used when the
// image's own init must stay PID 1; started via `docker exec -d`.
func runServe() {
	theReaper.start()
	srv := newServer()
	go srv.listenAndServe()
	waitForTermination()
	srv.shutdown()
}

func waitForTermination() {
	sigs := make(chan os.Signal, 2)
	signal.Notify(sigs, syscall.SIGTERM, syscall.SIGINT)
	<-sigs
	time.Sleep(50 * time.Millisecond)
}

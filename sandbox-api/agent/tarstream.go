package main

import (
	"net/http"
	"os/exec"
	"strings"
)

// POST /node/tarstream {path}  (proxy mode) - stream `tar -C path -c .` so
// checkpoint layers can be shipped to the blob store without staging a
// file. Restricted to the sandbox state area.
func handleNodeTarStream(w http.ResponseWriter, r *http.Request) {
	var req struct {
		Path string `json:"path"`
	}
	if err := readJSON(r, &req); err != nil || req.Path == "" {
		errJSON(w, 400, "path required")
		return
	}
	if !strings.HasPrefix(req.Path, "/var/lib/sbx/") || strings.Contains(req.Path, "..") {
		errJSON(w, 400, "path must be under /var/lib/sbx/")
		return
	}
	cmd := exec.Command("tar", "-C", req.Path, "-c", ".")
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		errJSON(w, 500, err.Error())
		return
	}
	if err := cmd.Start(); err != nil {
		errJSON(w, 500, err.Error())
		return
	}
	w.Header().Set("Content-Type", "application/x-tar")
	w.WriteHeader(200)
	flusher, _ := w.(http.Flusher)
	buf := make([]byte, 64*1024)
	for {
		n, rerr := stdout.Read(buf)
		if n > 0 {
			if _, werr := w.Write(buf[:n]); werr != nil {
				break
			}
			if flusher != nil {
				flusher.Flush()
			}
		}
		if rerr != nil {
			break
		}
	}
	_ = cmd.Wait()
}

// PUT /node/untarstream?path=  (proxy mode) - body is a raw tar extracted
// into path (created if missing). Streams through `tar -x`, so layers of
// any size can be re-materialised from the blob store.
func handleNodeUntarStream(w http.ResponseWriter, r *http.Request) {
	path := r.URL.Query().Get("path")
	if path == "" || !strings.HasPrefix(path, "/var/lib/sbx/") || strings.Contains(path, "..") {
		errJSON(w, 400, "path must be under /var/lib/sbx/")
		return
	}
	if err := exec.Command("mkdir", "-p", path).Run(); err != nil {
		errJSON(w, 500, "mkdir: "+err.Error())
		return
	}
	cmd := exec.Command("tar", "-x", "-C", path)
	cmd.Stdin = r.Body
	out, err := cmd.CombinedOutput()
	if err != nil {
		errJSON(w, 500, "tar -x failed: "+err.Error()+" "+string(out))
		return
	}
	writeJSON(w, 200, map[string]any{"path": path, "ok": true})
}

package main

import (
	"archive/tar"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"
)

// ---------------------------------------------------------------------------
// File API. Everything is path-based and jailed under SBX_AGENT_FS_ROOT
// (default "/" = the whole container). Symlinks are resolved before the
// jail check so a link such as /workspace/evil pointing at /etc cannot
// escape the root.
// ---------------------------------------------------------------------------

type fsEntry struct {
	Name  string `json:"name"`
	Type  string `json:"type"` // file | dir | symlink | other
	Size  int64  `json:"size"`
	Mode  string `json:"mode"`
	MTime string `json:"mtime"`
	Link  string `json:"link,omitempty"`
}

func entryOf(name string, info os.FileInfo, full string) fsEntry {
	e := fsEntry{Name: name, Size: info.Size(), Mode: fmt.Sprintf("%04o", info.Mode().Perm()),
		MTime: info.ModTime().UTC().Format(time.RFC3339)}
	switch {
	case info.Mode()&os.ModeSymlink != 0:
		e.Type = "symlink"
		if l, err := os.Readlink(full); err == nil {
			e.Link = l
		}
	case info.IsDir():
		e.Type = "dir"
	case info.Mode().IsRegular():
		e.Type = "file"
	default:
		e.Type = "other"
	}
	return e
}

// jail validates p against the configured root. It returns the cleaned
// absolute path (not symlink-resolved, so new files can be created) after
// proving that its deepest existing ancestor resolves inside the root.
func (s *server) jail(p string) (string, error) {
	if p == "" {
		return "", errors.New("path required")
	}
	if !filepath.IsAbs(p) {
		return "", errors.New("path must be absolute")
	}
	clean := filepath.Clean(p)
	if s.fsRoot == "/" {
		return clean, nil
	}
	root := filepath.Clean(s.fsRoot)
	inside := func(x string) bool { return x == root || strings.HasPrefix(x, root+"/") }
	if !inside(clean) {
		return "", fmt.Errorf("path outside allowed root %s", root)
	}
	probe := clean
	for {
		if _, err := os.Lstat(probe); err == nil {
			break
		}
		parent := filepath.Dir(probe)
		if parent == probe {
			break
		}
		probe = parent
	}
	real, err := filepath.EvalSymlinks(probe)
	if err != nil {
		return "", err
	}
	if !inside(real) {
		return "", fmt.Errorf("path resolves outside allowed root %s", root)
	}
	return clean, nil
}

func (s *server) handleFsList(w http.ResponseWriter, r *http.Request) {
	p, err := s.jail(r.URL.Query().Get("path"))
	if err != nil {
		errJSON(w, 400, err.Error())
		return
	}
	ents, err := os.ReadDir(p)
	if err != nil {
		errJSON(w, statusOf(err), err.Error())
		return
	}
	out := make([]fsEntry, 0, len(ents))
	for _, d := range ents {
		info, err := d.Info()
		if err != nil {
			continue
		}
		out = append(out, entryOf(d.Name(), info, filepath.Join(p, d.Name())))
	}
	writeJSON(w, 200, map[string]any{"path": p, "entries": out})
}

func (s *server) handleFsStat(w http.ResponseWriter, r *http.Request) {
	p, err := s.jail(r.URL.Query().Get("path"))
	if err != nil {
		errJSON(w, 400, err.Error())
		return
	}
	info, err := os.Lstat(p)
	if err != nil {
		errJSON(w, statusOf(err), err.Error())
		return
	}
	writeJSON(w, 200, map[string]any{"path": p, "entry": entryOf(filepath.Base(p), info, p)})
}

func (s *server) handleFsRead(w http.ResponseWriter, r *http.Request) {
	p, err := s.jail(r.URL.Query().Get("path"))
	if err != nil {
		errJSON(w, 400, err.Error())
		return
	}
	max := int64(1 << 20)
	if v := r.URL.Query().Get("max"); v != "" {
		max, _ = strconv.ParseInt(v, 10, 64)
	}
	info, err := os.Stat(p)
	if err != nil {
		errJSON(w, statusOf(err), err.Error())
		return
	}
	if info.IsDir() {
		errJSON(w, 400, "is a directory; use /fs/list or /fs/get")
		return
	}
	if max > 0 && info.Size() > max {
		errJSON(w, 413, fmt.Sprintf("file is %d bytes, larger than max=%d; use /fs/get for a stream", info.Size(), max))
		return
	}
	f, err := os.Open(p)
	if err != nil {
		errJSON(w, statusOf(err), err.Error())
		return
	}
	defer f.Close()
	w.Header().Set("Content-Type", "application/octet-stream")
	w.Header().Set("X-Size", strconv.FormatInt(info.Size(), 10))
	_, _ = io.Copy(w, f)
}

// PUT /fs/file?path=&mode=0644  body = raw bytes
func (s *server) handleFsWrite(w http.ResponseWriter, r *http.Request) {
	p, err := s.jail(r.URL.Query().Get("path"))
	if err != nil {
		errJSON(w, 400, err.Error())
		return
	}
	mode := os.FileMode(0o644)
	if v := r.URL.Query().Get("mode"); v != "" {
		if m, err := strconv.ParseUint(v, 8, 32); err == nil {
			mode = os.FileMode(m)
		}
	}
	if err := os.MkdirAll(filepath.Dir(p), 0o755); err != nil {
		errJSON(w, 500, err.Error())
		return
	}
	f, err := os.OpenFile(p, os.O_CREATE|os.O_WRONLY|os.O_TRUNC, mode)
	if err != nil {
		errJSON(w, statusOf(err), err.Error())
		return
	}
	n, err := io.Copy(f, r.Body)
	cerr := f.Close()
	if err == nil {
		err = cerr
	}
	if err != nil {
		errJSON(w, 500, err.Error())
		return
	}
	_ = os.Chmod(p, mode)
	writeJSON(w, 200, map[string]any{"path": p, "bytes": n})
}

func (s *server) handleFsMkdir(w http.ResponseWriter, r *http.Request) {
	p, err := s.jail(r.URL.Query().Get("path"))
	if err != nil {
		errJSON(w, 400, err.Error())
		return
	}
	if err := os.MkdirAll(p, 0o755); err != nil {
		errJSON(w, 500, err.Error())
		return
	}
	writeJSON(w, 200, map[string]any{"path": p})
}

func (s *server) handleFsDelete(w http.ResponseWriter, r *http.Request) {
	p, err := s.jail(r.URL.Query().Get("path"))
	if err != nil {
		errJSON(w, 400, err.Error())
		return
	}
	if p == "/" {
		errJSON(w, 400, "refusing to delete /")
		return
	}
	if err := os.RemoveAll(p); err != nil {
		errJSON(w, 500, err.Error())
		return
	}
	writeJSON(w, 200, map[string]any{"path": p, "deleted": true})
}

// GET /fs/get?path=  -> tar stream (a file or a whole directory tree)
func (s *server) handleFsGet(w http.ResponseWriter, r *http.Request) {
	p, err := s.jail(r.URL.Query().Get("path"))
	if err != nil {
		errJSON(w, 400, err.Error())
		return
	}
	info, err := os.Lstat(p)
	if err != nil {
		errJSON(w, statusOf(err), err.Error())
		return
	}
	w.Header().Set("Content-Type", "application/x-tar")
	tw := tar.NewWriter(w)
	defer tw.Close()
	base := filepath.Dir(p)
	if info.IsDir() {
		base = p
	}
	_ = filepath.Walk(p, func(path string, fi os.FileInfo, werr error) error {
		if werr != nil {
			return nil // skip unreadable entries
		}
		rel, _ := filepath.Rel(base, path)
		if rel == "." {
			return nil
		}
		link := ""
		if fi.Mode()&os.ModeSymlink != 0 {
			link, _ = os.Readlink(path)
		}
		hdr, err := tar.FileInfoHeader(fi, link)
		if err != nil {
			return nil
		}
		hdr.Name = filepath.ToSlash(rel)
		if fi.IsDir() {
			hdr.Name += "/"
		}
		if err := tw.WriteHeader(hdr); err != nil {
			return err
		}
		if fi.Mode().IsRegular() {
			f, err := os.Open(path)
			if err != nil {
				return nil
			}
			defer f.Close()
			_, _ = io.Copy(tw, f)
		}
		return nil
	})
}

// PUT /fs/put?path=  body = tar stream, extracted UNDER path
func (s *server) handleFsPut(w http.ResponseWriter, r *http.Request) {
	dest, err := s.jail(r.URL.Query().Get("path"))
	if err != nil {
		errJSON(w, 400, err.Error())
		return
	}
	if err := os.MkdirAll(dest, 0o755); err != nil {
		errJSON(w, 500, err.Error())
		return
	}
	tr := tar.NewReader(r.Body)
	files, total := 0, int64(0)
	for {
		hdr, err := tr.Next()
		if err == io.EOF {
			break
		}
		if err != nil {
			errJSON(w, 400, "bad tar: "+err.Error())
			return
		}
		name := filepath.Clean("/" + hdr.Name) // neutralises ../ tricks
		target := filepath.Join(dest, name)
		if !strings.HasPrefix(target, dest) {
			errJSON(w, 400, "tar entry escapes destination: "+hdr.Name)
			return
		}
		switch hdr.Typeflag {
		case tar.TypeDir:
			_ = os.MkdirAll(target, os.FileMode(hdr.Mode)|0o700)
		case tar.TypeReg:
			_ = os.MkdirAll(filepath.Dir(target), 0o755)
			f, err := os.OpenFile(target, os.O_CREATE|os.O_WRONLY|os.O_TRUNC, os.FileMode(hdr.Mode))
			if err != nil {
				errJSON(w, 500, err.Error())
				return
			}
			n, _ := io.Copy(f, tr)
			f.Close()
			files++
			total += n
		case tar.TypeSymlink:
			_ = os.MkdirAll(filepath.Dir(target), 0o755)
			_ = os.Remove(target)
			_ = os.Symlink(hdr.Linkname, target)
		}
	}
	writeJSON(w, 200, map[string]any{"path": dest, "files": files, "bytes": total})
}

func statusOf(err error) int {
	switch {
	case errors.Is(err, os.ErrNotExist):
		return 404
	case errors.Is(err, os.ErrPermission):
		return 403
	default:
		return 500
	}
}

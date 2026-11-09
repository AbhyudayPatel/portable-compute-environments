package main

import (
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"sort"
	"strings"
)

// ---------------------------------------------------------------------------
// Merkle fingerprint of a directory tree.
//
//   file hash    = sha256(content)
//   symlink hash = sha256("link:" + target)
//   dir hash     = sha256( for each child sorted by name:
//                          name NUL type NUL mode NUL childhash LF )
//
// Two trees with the same root hash are byte-identical in content, layout
// and permissions (mtimes are deliberately ignored - they change on every
// checkout and tell nothing about state). `detail=1` returns every entry
// so the control plane can diff two sandboxes file by file.
// ---------------------------------------------------------------------------

type fpEntry struct {
	Path string `json:"path"` // relative to the fingerprinted root
	Type string `json:"type"` // f | d | l
	Hash string `json:"hash"`
	Size int64  `json:"size"`
	Mode string `json:"mode"`
}

type fingerprint struct {
	Root    string    `json:"root"`
	Path    string    `json:"path"`
	Files   int       `json:"files"`
	Dirs    int       `json:"dirs"`
	Links   int       `json:"links"`
	Bytes   int64     `json:"bytes"`
	Entries []fpEntry `json:"entries,omitempty"`
}

type fpWalker struct {
	root     string
	exclude  []string
	detail   bool
	fp       *fingerprint
	skipped  int
}

func (wk *fpWalker) excluded(rel string) bool {
	if rel == "." {
		return false
	}
	parts := strings.Split(filepath.ToSlash(rel), "/")
	for _, ex := range wk.exclude {
		ex = strings.TrimSuffix(ex, "/")
		if ex == "" {
			continue
		}
		if strings.Contains(ex, "/") {
			// path prefix pattern
			if rel == ex || strings.HasPrefix(filepath.ToSlash(rel), ex+"/") {
				return true
			}
			continue
		}
		for _, p := range parts {
			if p == ex {
				return true
			}
		}
	}
	return false
}

func hashFile(path string) (string, error) {
	f, err := os.Open(path)
	if err != nil {
		return "", err
	}
	defer f.Close()
	h := sha256.New()
	if _, err := io.Copy(h, f); err != nil {
		return "", err
	}
	return hex.EncodeToString(h.Sum(nil)), nil
}

// hashDir returns the dir hash and records entries. Deterministic order.
func (wk *fpWalker) hashDir(abs, rel string) (string, error) {
	ents, err := os.ReadDir(abs)
	if err != nil {
		return "", err
	}
	sort.Slice(ents, func(i, j int) bool { return ents[i].Name() < ents[j].Name() })
	h := sha256.New()
	for _, d := range ents {
		name := d.Name()
		childRel := name
		if rel != "." {
			childRel = rel + "/" + name
		}
		if wk.excluded(childRel) {
			wk.skipped++
			continue
		}
		childAbs := filepath.Join(abs, name)
		info, err := os.Lstat(childAbs)
		if err != nil {
			continue
		}
		mode := fmt.Sprintf("%04o", info.Mode().Perm())
		var typ, hash string
		var size int64
		switch {
		case info.Mode()&os.ModeSymlink != 0:
			typ = "l"
			target, _ := os.Readlink(childAbs)
			sum := sha256.Sum256([]byte("link:" + target))
			hash = hex.EncodeToString(sum[:])
			wk.fp.Links++
		case info.IsDir():
			typ = "d"
			hash, err = wk.hashDir(childAbs, childRel)
			if err != nil {
				continue
			}
			wk.fp.Dirs++
		case info.Mode().IsRegular():
			typ = "f"
			hash, err = hashFile(childAbs)
			if err != nil {
				continue
			}
			size = info.Size()
			wk.fp.Files++
			wk.fp.Bytes += size
		default:
			continue // sockets, devices: not state
		}
		fmt.Fprintf(h, "%s\x00%s\x00%s\x00%s\n", name, typ, mode, hash)
		if wk.detail {
			wk.fp.Entries = append(wk.fp.Entries, fpEntry{Path: childRel, Type: typ, Hash: hash, Size: size, Mode: mode})
		}
	}
	return hex.EncodeToString(h.Sum(nil)), nil
}

func (s *server) handleFingerprint(w http.ResponseWriter, r *http.Request) {
	q := r.URL.Query()
	p, err := s.jail(q.Get("path"))
	if err != nil {
		errJSON(w, 400, err.Error())
		return
	}
	info, err := os.Stat(p)
	if err != nil {
		errJSON(w, statusOf(err), err.Error())
		return
	}
	var exclude []string
	if v := q.Get("exclude"); v != "" {
		exclude = strings.Split(v, ",")
	}
	wk := &fpWalker{root: p, exclude: exclude, detail: q.Get("detail") == "1", fp: &fingerprint{Path: p}}
	if info.IsDir() {
		root, err := wk.hashDir(p, ".")
		if err != nil {
			errJSON(w, 500, err.Error())
			return
		}
		wk.fp.Root = root
	} else {
		root, err := hashFile(p)
		if err != nil {
			errJSON(w, 500, err.Error())
			return
		}
		wk.fp.Root = root
		wk.fp.Files = 1
		wk.fp.Bytes = info.Size()
	}
	if wk.fp.Entries == nil && wk.detail {
		wk.fp.Entries = []fpEntry{}
	}
	writeJSON(w, 200, wk.fp)
}

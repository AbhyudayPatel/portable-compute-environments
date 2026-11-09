package main

import (
	"bufio"
	"net/http"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
)

// ---------------------------------------------------------------------------
// Resource stats straight from the cgroup the container lives in, plus a
// /proc-based process list. cgroup v2 is the primary path (Docker Desktop /
// WSL2 and every modern distro); a few v1 files are read as fallback.
// ---------------------------------------------------------------------------

func readFile(p string) (string, bool) {
	b, err := os.ReadFile(p)
	if err != nil {
		return "", false
	}
	return strings.TrimSpace(string(b)), true
}

// "key value" lines -> map; values parsed as int64 when possible
func readKV(p string) map[string]any {
	out := map[string]any{}
	txt, ok := readFile(p)
	if !ok {
		return nil
	}
	for _, line := range strings.Split(txt, "\n") {
		f := strings.Fields(line)
		if len(f) < 2 {
			continue
		}
		if n, err := strconv.ParseInt(f[1], 10, 64); err == nil {
			out[f[0]] = n
		} else {
			out[f[0]] = f[1]
		}
	}
	return out
}

func readNum(p string) any {
	txt, ok := readFile(p)
	if !ok {
		return nil
	}
	if n, err := strconv.ParseInt(txt, 10, 64); err == nil {
		return n
	}
	return txt // e.g. "max"
}

// PSI: "some avg10=0.00 avg60=0.00 avg300=0.00 total=0\nfull ..."
func readPSI(p string) map[string]map[string]float64 {
	txt, ok := readFile(p)
	if !ok {
		return nil
	}
	out := map[string]map[string]float64{}
	for _, line := range strings.Split(txt, "\n") {
		f := strings.Fields(line)
		if len(f) < 2 {
			continue
		}
		m := map[string]float64{}
		for _, kv := range f[1:] {
			if i := strings.IndexByte(kv, '='); i > 0 {
				v, _ := strconv.ParseFloat(kv[i+1:], 64)
				m[kv[:i]] = v
			}
		}
		out[f[0]] = m
	}
	return out
}

func cgroupStats() map[string]any {
	const v2 = "/sys/fs/cgroup"
	if _, err := os.Stat(filepath.Join(v2, "cgroup.controllers")); err == nil {
		return map[string]any{
			"version": 2,
			"cpu": map[string]any{
				"stat":     readKV(filepath.Join(v2, "cpu.stat")),
				"max":      readNum(filepath.Join(v2, "cpu.max")),
				"weight":   readNum(filepath.Join(v2, "cpu.weight")),
				"pressure": readPSI(filepath.Join(v2, "cpu.pressure")),
			},
			"memory": map[string]any{
				"current":  readNum(filepath.Join(v2, "memory.current")),
				"max":      readNum(filepath.Join(v2, "memory.max")),
				"high":     readNum(filepath.Join(v2, "memory.high")),
				"peak":     readNum(filepath.Join(v2, "memory.peak")),
				"swap":     readNum(filepath.Join(v2, "memory.swap.current")),
				"stat":     readKV(filepath.Join(v2, "memory.stat")),
				"events":   readKV(filepath.Join(v2, "memory.events")),
				"pressure": readPSI(filepath.Join(v2, "memory.pressure")),
			},
			"pids": map[string]any{
				"current": readNum(filepath.Join(v2, "pids.current")),
				"max":     readNum(filepath.Join(v2, "pids.max")),
			},
			"io": map[string]any{
				"stat":     readFileOrNil(filepath.Join(v2, "io.stat")),
				"pressure": readPSI(filepath.Join(v2, "io.pressure")),
			},
		}
	}
	// cgroup v1 fallback (best effort)
	return map[string]any{
		"version": 1,
		"cpu": map[string]any{
			"usage_ns": readNum("/sys/fs/cgroup/cpuacct/cpuacct.usage"),
			"stat":     readKV("/sys/fs/cgroup/cpu/cpu.stat"),
		},
		"memory": map[string]any{
			"current": readNum("/sys/fs/cgroup/memory/memory.usage_in_bytes"),
			"max":     readNum("/sys/fs/cgroup/memory/memory.limit_in_bytes"),
			"stat":    readKV("/sys/fs/cgroup/memory/memory.stat"),
		},
		"pids": map[string]any{
			"current": readNum("/sys/fs/cgroup/pids/pids.current"),
			"max":     readNum("/sys/fs/cgroup/pids/pids.max"),
		},
	}
}

func readFileOrNil(p string) any {
	if s, ok := readFile(p); ok {
		return s
	}
	return nil
}

type procInfo struct {
	Pid   int    `json:"pid"`
	PPid  int    `json:"ppid"`
	State string `json:"state"`
	RSS   int64  `json:"rss_bytes"`
	Cmd   string `json:"cmd"`
}

func processList() []procInfo {
	out := []procInfo{}
	ents, err := os.ReadDir("/proc")
	if err != nil {
		return out
	}
	pageSize := int64(os.Getpagesize())
	for _, e := range ents {
		pid, err := strconv.Atoi(e.Name())
		if err != nil {
			continue
		}
		stat, ok := readFile(filepath.Join("/proc", e.Name(), "stat"))
		if !ok {
			continue
		}
		// pid (comm) state ppid ...  - comm may contain spaces, split after ')'
		rp := strings.LastIndexByte(stat, ')')
		if rp < 0 {
			continue
		}
		fields := strings.Fields(stat[rp+1:])
		if len(fields) < 2 {
			continue
		}
		ppid, _ := strconv.Atoi(fields[1])
		pi := procInfo{Pid: pid, PPid: ppid, State: fields[0]}
		if statm, ok := readFile(filepath.Join("/proc", e.Name(), "statm")); ok {
			f := strings.Fields(statm)
			if len(f) >= 2 {
				if rss, err := strconv.ParseInt(f[1], 10, 64); err == nil {
					pi.RSS = rss * pageSize
				}
			}
		}
		if b, err := os.ReadFile(filepath.Join("/proc", e.Name(), "cmdline")); err == nil && len(b) > 0 {
			pi.Cmd = strings.TrimRight(strings.ReplaceAll(string(b), "\x00", " "), " ")
		} else {
			pi.Cmd = "[" + stat[strings.IndexByte(stat, '(')+1:rp] + "]"
		}
		out = append(out, pi)
	}
	return out
}

func loadAvg() []float64 {
	txt, ok := readFile("/proc/loadavg")
	if !ok {
		return nil
	}
	f := strings.Fields(txt)
	out := []float64{}
	for i := 0; i < 3 && i < len(f); i++ {
		v, _ := strconv.ParseFloat(f[i], 64)
		out = append(out, v)
	}
	return out
}

func diskUsage(path string) map[string]any {
	var st syscall.Statfs_t
	if err := syscall.Statfs(path, &st); err != nil {
		return nil
	}
	total := int64(st.Blocks) * int64(st.Bsize)
	free := int64(st.Bavail) * int64(st.Bsize)
	return map[string]any{"path": path, "total_bytes": total, "free_bytes": free, "used_bytes": total - free}
}

func (s *server) handleStats(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, 200, map[string]any{
		"cgroup":  cgroupStats(),
		"loadavg": loadAvg(),
		"disk":    []any{diskUsage("/"), diskUsage(s.fsRoot)},
		"uptime_seconds": uptimeSeconds(),
	})
}

func uptimeSeconds() float64 {
	txt, ok := readFile("/proc/uptime")
	if !ok {
		return 0
	}
	f := strings.Fields(txt)
	if len(f) == 0 {
		return 0
	}
	v, _ := strconv.ParseFloat(f[0], 64)
	return v
}

func (s *server) handlePs(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, 200, map[string]any{"processes": processList()})
}

// keep bufio imported for future line scanners
var _ = bufio.NewScanner

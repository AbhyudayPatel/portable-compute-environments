package main

import (
	"bytes"
	"context"
	"fmt"
	"net/http"
	"os"
	"os/exec"
	"strconv"
	"strings"
	"time"
)

// ---------------------------------------------------------------------------
// GUI endpoints (desktop template). Thin, well-defined wrappers around
// xdotool / scrot so a computer-use agent gets: look (screenshot), act
// (click/type/key/scroll/drag) and orient (display size, window list).
// All commands run against SBX_DISPLAY (default :1) with a 15 s cap.
// ---------------------------------------------------------------------------

func (s *server) guiEnv() []string {
	env := os.Environ()
	env = append(env, "DISPLAY="+envOr("SBX_DISPLAY", ":1"))
	if xa := os.Getenv("SBX_XAUTHORITY"); xa != "" {
		env = append(env, "XAUTHORITY="+xa)
	}
	return env
}

func (s *server) guiRun(args ...string) (string, string, error) {
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	cmd := exec.CommandContext(ctx, args[0], args[1:]...)
	cmd.Env = s.guiEnv()
	var out, errb bytes.Buffer
	cmd.Stdout, cmd.Stderr = &out, &errb
	err := cmd.Run()
	return out.String(), errb.String(), err
}

func (s *server) handleGuiDisplay(w http.ResponseWriter, r *http.Request) {
	out, errb, err := s.guiRun("xdotool", "getdisplaygeometry")
	if err != nil {
		errJSON(w, 500, "xdotool getdisplaygeometry: "+err.Error()+" "+errb)
		return
	}
	f := strings.Fields(out)
	if len(f) < 2 {
		errJSON(w, 500, "unexpected output: "+out)
		return
	}
	wd, _ := strconv.Atoi(f[0])
	ht, _ := strconv.Atoi(f[1])
	writeJSON(w, 200, map[string]any{"width": wd, "height": ht, "display": envOr("SBX_DISPLAY", ":1")})
}

// screenshot: scrot if present, else ImageMagick import, else xwd+convert.
func (s *server) handleGuiScreenshot(w http.ResponseWriter, r *http.Request) {
	tmp := fmt.Sprintf("/tmp/sbx-shot-%d.png", time.Now().UnixNano())
	defer os.Remove(tmp)
	var err error
	var errb string
	if _, lerr := exec.LookPath("scrot"); lerr == nil {
		_, errb, err = s.guiRun("scrot", "-o", tmp)
	} else if _, lerr := exec.LookPath("import"); lerr == nil {
		_, errb, err = s.guiRun("import", "-window", "root", tmp)
	} else {
		_, errb, err = s.guiRun("sh", "-c", "xwd -root -silent | convert xwd:- png:"+tmp)
	}
	if err != nil {
		errJSON(w, 500, "screenshot failed: "+err.Error()+" "+errb)
		return
	}
	b, err := os.ReadFile(tmp)
	if err != nil {
		errJSON(w, 500, err.Error())
		return
	}
	w.Header().Set("Content-Type", "image/png")
	w.Header().Set("X-Size", strconv.Itoa(len(b)))
	_, _ = w.Write(b)
}

type guiAction struct {
	Type   string `json:"type"` // click | dblclick | rightclick | move | type | key | scroll | drag
	X      int    `json:"x"`
	Y      int    `json:"y"`
	X2     int    `json:"x2"`
	Y2     int    `json:"y2"`
	Button int    `json:"button"`
	Text   string `json:"text"`
	Keys   string `json:"keys"` // xdotool key syntax: "ctrl+l", "Return", "alt+F4 Tab"
	Count  int    `json:"count"`
	Dir    string `json:"direction"` // up | down (scroll)
}

func (s *server) handleGuiAction(w http.ResponseWriter, r *http.Request) {
	var a guiAction
	if err := readJSON(r, &a); err != nil {
		errJSON(w, 400, "bad json: "+err.Error())
		return
	}
	if a.Button <= 0 {
		a.Button = 1
	}
	if a.Count <= 0 {
		a.Count = 1
	}
	xy := func(x, y int) []string { return []string{"mousemove", "--sync", strconv.Itoa(x), strconv.Itoa(y)} }
	var args []string
	switch a.Type {
	case "move":
		args = xy(a.X, a.Y)
	case "click":
		args = append(xy(a.X, a.Y), "click", "--repeat", strconv.Itoa(a.Count), strconv.Itoa(a.Button))
	case "dblclick":
		args = append(xy(a.X, a.Y), "click", "--repeat", "2", "--delay", "80", "1")
	case "rightclick":
		args = append(xy(a.X, a.Y), "click", "3")
	case "type":
		if a.Text == "" {
			errJSON(w, 400, "text required")
			return
		}
		args = []string{"type", "--delay", "20", "--", a.Text}
	case "key":
		if a.Keys == "" {
			errJSON(w, 400, "keys required")
			return
		}
		args = append([]string{"key", "--delay", "40", "--"}, strings.Fields(a.Keys)...)
	case "scroll":
		btn := "5"
		if a.Dir == "up" {
			btn = "4"
		}
		args = append(xy(a.X, a.Y), "click", "--repeat", strconv.Itoa(a.Count), "--delay", "30", btn)
	case "drag":
		args = append(xy(a.X, a.Y), "mousedown", strconv.Itoa(a.Button))
		args = append(args, xy(a.X2, a.Y2)...)
		args = append(args, "mouseup", strconv.Itoa(a.Button))
	default:
		errJSON(w, 400, "unknown action type "+a.Type)
		return
	}
	out, errb, err := s.guiRun(append([]string{"xdotool"}, args...)...)
	if err != nil {
		errJSON(w, 500, "xdotool "+strings.Join(args, " ")+": "+err.Error()+" "+errb)
		return
	}
	writeJSON(w, 200, map[string]any{"ok": true, "action": a.Type, "xdotool": args, "stdout": out})
}

func (s *server) handleGuiWindows(w http.ResponseWriter, r *http.Request) {
	out, errb, err := s.guiRun("xdotool", "search", "--onlyvisible", "--name", ".")
	if err != nil && strings.TrimSpace(out) == "" {
		errJSON(w, 500, "xdotool search: "+err.Error()+" "+errb)
		return
	}
	active, _, _ := s.guiRun("xdotool", "getactivewindow")
	type win struct {
		ID     string `json:"id"`
		Name   string `json:"name"`
		X      int    `json:"x"`
		Y      int    `json:"y"`
		Width  int    `json:"width"`
		Height int    `json:"height"`
		Active bool   `json:"active"`
	}
	wins := []win{}
	for _, id := range strings.Fields(out) {
		name, _, _ := s.guiRun("xdotool", "getwindowname", id)
		geo, _, _ := s.guiRun("xdotool", "getwindowgeometry", "--shell", id)
		wn := win{ID: id, Name: strings.TrimSpace(name), Active: strings.TrimSpace(active) == id}
		for _, line := range strings.Split(geo, "\n") {
			k, v, ok := strings.Cut(strings.TrimSpace(line), "=")
			if !ok {
				continue
			}
			n, _ := strconv.Atoi(v)
			switch k {
			case "X":
				wn.X = n
			case "Y":
				wn.Y = n
			case "WIDTH":
				wn.Width = n
			case "HEIGHT":
				wn.Height = n
			}
		}
		wins = append(wins, wn)
	}
	writeJSON(w, 200, map[string]any{"windows": wins})
}

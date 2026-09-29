package main

import (
	"fmt"
	"os"
	"path/filepath"
	"testing"
)

func TestRunningClaude(t *testing.T) {
	t.Setenv("CLAUDE_CONFIG_DIR", t.TempDir())
	dir := filepath.Join(claudeDir(), "sessions")
	must(t, os.MkdirAll(dir, 0o700))
	register := func(name string, pid int, start, tmux string) {
		body := fmt.Sprintf(`{"pid":%d,"sessionId":%q,"procStart":%q,"tmux":%q,"status":"idle"}`, pid, name, start, tmux)
		must(t, os.WriteFile(filepath.Join(dir, fmt.Sprint(pid)+".json"), []byte(body), 0o600))
	}
	me := os.Getpid()
	start, err := procStart(me)
	must(t, err)
	register("live", me, start, "work:@3.%12")
	register("reused-pid", os.Getppid(), "1", "work:@4.%13") // pid alive, but a different process
	register("dead", 1<<22+7, "5", "")

	got, err := RunningClaude()
	must(t, err)
	if r, ok := got["live"]; !ok || r.TmuxPane != "%12" || r.PID != me || r.Status != "idle" {
		t.Errorf("live = %+v, %v", r, ok)
	}
	for _, id := range []string{"reused-pid", "dead"} {
		if _, ok := got[id]; ok {
			t.Errorf("%s counted as running", id)
		}
	}
}

func TestResolveMarksRunning(t *testing.T) {
	opt := defaults
	opt.Running = map[string]Running{"on": {PID: 1, TmuxPane: "%4"}}
	got := Resolve([]Entry{
		entry(t, Session{ID: "on", Cwd: "/r"}, "otlp"),
		entry(t, Session{ID: "off", Cwd: "/r"}, "otlp"),
	}, "otlp", opt)
	for _, s := range got[0].Sessions {
		if (s.Running != nil) != (s.ID == "on") {
			t.Errorf("%s running = %+v", s.ID, s.Running)
		}
	}
}

func TestRunningClaudeUnreadableIsAnError(t *testing.T) {
	if os.Getuid() == 0 {
		t.Skip("root reads anything")
	}
	t.Setenv("CLAUDE_CONFIG_DIR", t.TempDir())
	dir := filepath.Join(claudeDir(), "sessions")
	must(t, os.MkdirAll(dir, 0o000))
	defer os.Chmod(dir, 0o700)
	if _, err := RunningClaude(); err == nil {
		t.Errorf("unreadable registry reported as nothing running")
	}
	// So is a registration that doesn't parse: it could be a live session.
	must(t, os.Chmod(dir, 0o700))
	for _, body := range []string{`{"pid":9,"sess`, `{"pid":9,"sessionId":"s"}`, `{"sessionId":"s","procStart":"5"}`} {
		must(t, os.WriteFile(filepath.Join(dir, "9.json"), []byte(body), 0o600))
		if _, err := RunningClaude(); err == nil {
			t.Errorf("registration %s reported as nothing running", body)
		}
	}
	must(t, os.Remove(filepath.Join(dir, "9.json")))

	// No registry at all is a real answer: nothing running.
	must(t, os.Chmod(dir, 0o700))
	must(t, os.Remove(dir))
	if got, err := RunningClaude(); err != nil || len(got) != 0 {
		t.Errorf("missing registry = %v, %v", got, err)
	}
}

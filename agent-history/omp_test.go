package main

import (
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"syscall"
	"testing"
	"time"
)

const ompID = "01a0fe96-e98d-7000-8e48-b362b12d6e56"

// The shapes omp writes: a title slot rewritten in place, the session header, what the
// human typed, and the user-role notifications omp injects, which must stay out.
var ompMain = `{"type":"title","v":1,"title":"Renamed by hand","source":"user","updatedAt":"2026-10-02T21:50:00Z","pad":"   "}
{"type":"session","version":3,"id":"` + ompID + `","timestamp":"2026-10-02T21:48:18Z","cwd":"/src/my repo","title":"First name"}
{"type":"message","id":"a","timestamp":"2026-10-02T21:48:20Z","message":{"role":"user","attribution":"user","content":[{"type":"text","text":"fix live mode"}]}}
{"type":"message","id":"b","timestamp":"2026-10-02T21:48:21Z","message":{"role":"user","synthetic":true,"content":"<system-notification>"}}
{"type":"message","id":"c","timestamp":"2026-10-02T21:48:22Z","message":{"role":"user","attribution":"agent","content":"subagent finished"}}
{"type":"message","id":"d","timestamp":"2026-10-02T21:48:23Z","message":{"role":"user","content":"legacy, unattributed"}}
{"type":"message","id":"e","timestamp":"2026-10-02T21:48:30Z","message":{"role":"assistant","content":[{"type":"text","text":"On it."}]}}
{"type":"message","id":"f","timest`

var ompSub = `{"type":"title","v":1,"title":"","updatedAt":"2026-10-02T21:49:03Z","pad":"  "}
{"type":"session","version":3,"id":"01a0fe97-993c-7000-b85a-272b4b8b7d9d","timestamp":"2026-10-02T21:49:03Z","cwd":"/src/my repo","parentSession":"x"}
{"type":"session_init","timestamp":"2026-10-02T21:49:03Z","systemPrompt":"you are a subagent","task":"count the lines in README.md"}
{"type":"message","id":"a","timestamp":"2026-10-02T21:49:04Z","message":{"role":"user","attribution":"agent","content":"count the lines in README.md"}}
`

// writeOmp lays out a session and one subagent as omp does, returning their paths.
func writeOmp(t *testing.T) (main, sub string) {
	t.Helper()
	t.Setenv("PI_CODING_AGENT_DIR", t.TempDir())
	main = filepath.Join(ompSessionsDir(), "-src-my-repo", "2026-10-02T21-48-18-189Z_"+ompID+".jsonl")
	sub = filepath.Join(strings.TrimSuffix(main, ".jsonl"), "CountReadmeLines.jsonl")
	must(t, os.MkdirAll(filepath.Dir(sub), 0o700))
	must(t, os.WriteFile(main, []byte(ompMain), 0o600))
	must(t, os.WriteFile(sub, []byte(ompSub), 0o600))
	must(t, os.WriteFile(filepath.Join(filepath.Dir(sub), "1.bash-original.log"), []byte("tool output"), 0o600))
	return main, sub
}

func TestReadOmp(t *testing.T) {
	main, sub := writeOmp(t)
	s, err := ReadOmp(main)
	must(t, err)
	want := []Message{{"2026-10-02T21:48:20Z", "user", "fix live mode"}, {"2026-10-02T21:48:23Z", "user", "legacy, unattributed"}}
	if fmt.Sprint(s.Messages) != fmt.Sprint(want) {
		t.Errorf("messages = %+v, want %+v", s.Messages, want)
	}
	check(t, "id", s.ID, ompID)
	check(t, "title (the slot, not the header)", s.Title, "Renamed by hand")
	check(t, "started", s.Started, "2026-10-02T21:48:18Z")
	check(t, "last_active", s.LastActive, "2026-10-02T21:48:30Z")
	check(t, "resume", ResumeLine(s.Cwd, s.ResumeArgv), "cd '/src/my repo' && 'omp' '--resume' '"+ompID+"'")

	s, err = ReadOmp(sub)
	must(t, err)
	check(t, "subagent", s.ID+" under "+s.Parent+" titled "+s.Title, "CountReadmeLines under "+ompID+" titled CountReadmeLines")
	want = []Message{{"2026-10-02T21:49:03Z", "prompt", "count the lines in README.md"}}
	if fmt.Sprint(s.Messages) != fmt.Sprint(want) || s.ResumeArgv != nil {
		t.Errorf("subagent = %+v, want its task and no resume", s)
	}
}

func TestReconcileIndexesOmp(t *testing.T) {
	t.Setenv("AGENT_HISTORY_DIR", t.TempDir())
	writeOmp(t)
	if !reconcileAll(false) {
		t.Fatal("reconcile failed")
	}
	entries, err := indexEntries()
	must(t, err)
	if len(entries) != 2 {
		t.Errorf("entries = %v, want the session and its subagent", entries)
	}
	e, err := ReadEntry(indexPath("omp", "", ompID))
	must(t, err)
	if e.Harness != "omp" || strings.Contains(e.body, "notification") || strings.Contains(e.body, "on it") {
		t.Errorf("entry = %+v, body %q", e, e.body)
	}
	_, err = ReadEntry(indexPath("omp", ompID, "CountReadmeLines"))
	must(t, err)
}

// omp is found live the way Codex is (TestRunningCodex covers the rules); a held
// subagent transcript means its parent session is the one running.
func TestRunningOmp(t *testing.T) {
	_, sub := writeOmp(t)
	omp := filepath.Join(t.TempDir(), "omp")
	must(t, os.Symlink("/bin/sh", omp))
	cmd := exec.Command(omp, "-c", `exec 3<"$1"; read -r _`, "sh", sub)
	stdin, err := cmd.StdinPipe()
	must(t, err)
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	must(t, cmd.Start())
	t.Cleanup(func() { stdin.Close(); syscall.Kill(-cmd.Process.Pid, syscall.SIGKILL); cmd.Wait() })
	for deadline := time.Now().Add(5 * time.Second); time.Now().Before(deadline); time.Sleep(20 * time.Millisecond) {
		if _, err := os.Readlink(fmt.Sprintf("/proc/%d/fd/3", cmd.Process.Pid)); err == nil {
			break
		}
	}
	got, err := RunningOmp()
	must(t, err)
	if r, ok := got[ompID]; !ok || r.PID != cmd.Process.Pid || len(got) != 1 {
		t.Errorf("running = %+v, want only %s in pid %d", got, ompID, cmd.Process.Pid)
	}
}

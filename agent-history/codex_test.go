package main

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

// No test may read the real ~/.codex; each one that needs Codex data writes its own.
func TestMain(m *testing.M) {
	dir, err := os.MkdirTemp("", "codex-home")
	if err != nil {
		panic(err)
	}
	os.Setenv("CODEX_HOME", dir)
	code := m.Run()
	os.RemoveAll(dir)
	os.Exit(code)
}

const codexID = "0000aaaa-0000-7000-8000-000000000001"

// A thread started, then resumed into a second rollout file; both user-message shapes
// Codex has written, and model/tool traffic that must not reach the index.
var codexRollouts = map[string]string{
	"2026/09/01/rollout-2026-09-01T10-00-00-" + codexID + ".jsonl": `{"timestamp":"2026-09-01T10:00:00Z","type":"session_meta","payload":{"id":"` + codexID + `","parent_thread_id":null,"cwd":"/src/my repo","source":"cli","git":{"branch":"main"}}}
{"timestamp":"2026-09-01T10:00:01Z","type":"response_item","payload":{"type":"message","role":"user","content":[{"type":"input_text","text":"# AGENTS.md instructions"}]}}
{"timestamp":"2026-09-01T10:00:02Z","type":"event_msg","payload":{"type":"item_completed","item":{"type":"UserMessage","content":[{"type":"text","text":"fix live mode"}]}}}
{"timestamp":"2026-09-01T10:00:03Z","type":"response_item","payload":{"type":"function_call_output","output":"{\"type\":\"UserMessage\",\"secret\":\"tool output\"}"}}
{"timestamp":"2026-09-01T10:00:04Z","type":"event_msg","payload":{"type":"agent_message","message":"On it."}}
`,
	"2026/09/02/rollout-2026-09-02T09-00-00-" + codexID + "_0000bbbb-0000-7000-8000-000000000002.jsonl": `{"timestamp":"2026-09-02T09:00:00Z","type":"session_meta","payload":{"id":"` + codexID + `","parent_thread_id":null,"cwd":"/src/my repo","source":"cli","git":{"branch":"feat/x"}}}
{"timestamp":"2026-09-02T09:00:05Z","type":"event_msg","payload":{"type":"user_message","message":"also the tests"}}
{"timestamp":"2026-09-02T09:00:09Z","type":"event_msg","payload":{"type":"task_complete"}}
`,
	// An approval review Codex ran as a subagent: not indexed.
	"2026/09/01/rollout-2026-09-01T10-00-03-0000cccc-0000-7000-8000-000000000003.jsonl": `{"timestamp":"2026-09-01T10:00:03Z","type":"session_meta","payload":{"id":"0000cccc-0000-7000-8000-000000000003","parent_thread_id":"` + codexID + `","cwd":"/src/my repo","source":{"subagent":{"other":"guardian"}}}}
{"timestamp":"2026-09-01T10:00:03Z","type":"event_msg","payload":{"type":"user_message","message":"review this transcript"}}
`,
}

func writeCodex(t *testing.T) {
	t.Helper()
	t.Setenv("CODEX_HOME", t.TempDir())
	for name, body := range codexRollouts {
		path := filepath.Join(codexSessionsDir(), name)
		must(t, os.MkdirAll(filepath.Dir(path), 0o700))
		must(t, os.WriteFile(path, []byte(body), 0o600))
	}
	names := `{"id":"` + codexID + `","thread_name":"First name"}
{"id":"someone-else","thread_name":"Not this one"}
{"id":"` + codexID + `","thread_name":"live mode fix"}
`
	must(t, os.WriteFile(filepath.Join(codexDir(), "session_index.jsonl"), []byte(names), 0o600))
}

func TestReadCodex(t *testing.T) {
	writeCodex(t)
	sessions := sessionsOf(t)
	if len(sessions) != 2 || len(sessions[0]) != 2 {
		t.Fatalf("sessions = %v, want the resumed thread's two files together, then the review", sessions)
	}
	s, err := ReadCodex(sessions[0])
	must(t, err)
	want := []Message{{"2026-09-01T10:00:02Z", "user", "fix live mode"}, {"2026-09-02T09:00:05Z", "user", "also the tests"}}
	if len(s.Messages) != len(want) || s.Messages[0] != want[0] || s.Messages[1] != want[1] {
		t.Errorf("messages = %+v, want %+v", s.Messages, want)
	}
	check(t, "id", s.ID, codexID)
	check(t, "title (last rename wins)", s.Title, "live mode fix")
	check(t, "branches", strings.Join(s.Branches, ","), "main,feat/x")
	check(t, "entrypoint", s.Entrypoint, "cli")
	check(t, "started", s.Started, "2026-09-01T10:00:00Z")
	check(t, "last_active", s.LastActive, "2026-09-02T09:00:09Z")
	check(t, "resume", ResumeLine(s.Cwd, s.ResumeArgv), "cd '/src/my repo' && 'codex' 'resume' '"+codexID+"'")
}

func TestReconcileIndexesCodex(t *testing.T) {
	t.Setenv("AGENT_HISTORY_DIR", t.TempDir())
	t.Setenv("CLAUDE_CONFIG_DIR", t.TempDir())
	writeCodex(t)
	if !reconcileAll(false) {
		t.Fatal("reconcile failed")
	}
	e, err := ReadEntry(indexPath("codex", "", codexID))
	must(t, err)
	check(t, "harness", e.Harness, "codex")
	if strings.Contains(e.body, "tool output") || strings.Contains(e.body, "agents md") || strings.Contains(e.body, "on it") {
		t.Errorf("entry leaked agent traffic: %q", e.body)
	}
	entries, err := indexEntries()
	must(t, err)
	if len(entries) != 1 {
		t.Errorf("entries = %v, want only the top-level thread", entries)
	}
	// An entry is fresh while no file of its thread is newer; resuming writes one.
	info, err := os.Stat(e.Path)
	must(t, err)
	must(t, os.WriteFile(e.Path, []byte("sentinel"), 0o600))
	must(t, os.Chtimes(e.Path, info.ModTime(), info.ModTime()))
	reconcileAll(false)
	if data, _ := os.ReadFile(e.Path); string(data) != "sentinel" {
		t.Errorf("up-to-date entry was rewritten")
	}
	later := info.ModTime().Add(time.Minute)
	must(t, os.Chtimes(sessionsOf(t)[0][1], later, later))
	reconcileAll(false)
	if data, _ := os.ReadFile(e.Path); string(data) == "sentinel" {
		t.Errorf("entry not rebuilt when its thread was resumed")
	}

	// Renaming an idle thread touches only Codex's name log; that rebuilds it too.
	names := filepath.Join(codexDir(), "session_index.jsonl")
	f, err := os.OpenFile(names, os.O_APPEND|os.O_WRONLY, 0)
	must(t, err)
	_, err = f.WriteString(`{"id":"` + codexID + `","thread_name":"renamed later","updated_at":"` + later.Add(time.Hour).Format(time.RFC3339Nano) + `"}` + "\n")
	must(t, err)
	must(t, f.Close())
	reconcileAll(false)
	e, err = ReadEntry(e.Path)
	must(t, err)
	check(t, "title after rename", e.Title, "renamed later")

	// Retention deletes a thread's oldest file first; the entry still has a source.
	must(t, os.Remove(sessionsOf(t)[0][0]))
	reconcileAll(false)
	e, err = ReadEntry(e.Path)
	must(t, err)
	if e.SourceMissing || e.ResumeArgv == nil {
		t.Errorf("thread with a surviving rollout marked missing: %+v", e)
	}
}

func sessionsOf(t *testing.T) [][]string {
	t.Helper()
	sessions, err := codexSessions()
	must(t, err)
	return sessions
}

func TestRunningCodex(t *testing.T) {
	writeCodex(t)
	// CODEX_HOME reached through a symlink: open files still show the real path.
	link := filepath.Join(t.TempDir(), "codex-link")
	must(t, os.Symlink(codexDir(), link))
	t.Setenv("CODEX_HOME", link)
	var name string
	for n := range codexRollouts {
		name = n
	}
	f, err := os.Open(filepath.Join(codexSessionsDir(), name))
	must(t, err)
	id, _ := codexIdentity(name)
	got, err := RunningCodex()
	must(t, err)
	// A process with no terminal (like the app-server daemon, or this test under CI)
	// is in no pane, whatever its environment says.
	pane := os.Getenv("TMUX_PANE")
	if tty, _ := procStat(os.Getpid(), 7); tty == "0" {
		pane = ""
	}
	if r, ok := got[id]; !ok || r.PID != os.Getpid() || r.TmuxPane != pane {
		t.Errorf("open rollout = %+v, %v", r, ok)
	}
	f.Close()
	got, err = RunningCodex()
	must(t, err)
	if _, ok := got[id]; ok {
		t.Errorf("closed rollout counted as running")
	}
}

package main

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"syscall"
	"testing"
	"time"
)

// A transcript exercising each record kind the reader has to tell apart.
const transcript = `{"type":"user","timestamp":"2026-09-01T10:00:00Z","cwd":"/src/my repo","gitBranch":"main","entrypoint":"cli","origin":{"kind":"human"},"promptSource":"typed","message":{"content":"fix live mode"}}
{"type":"assistant","timestamp":"2026-09-01T10:00:05Z","message":{"content":[{"type":"text","text":"On it."}]}}
{"type":"user","timestamp":"2026-09-01T10:00:06Z","message":{"content":[{"type":"tool_result","content":"secret file contents"}]}}
{"type":"user","timestamp":"2026-09-01T10:00:07Z","isMeta":true,"message":{"content":"<system-reminder>not the human</system-reminder>"}}
{"type":"user","timestamp":"2026-09-01T10:00:08Z","origin":{"kind":"task-notification"},"message":{"content":"<task-notification>done</task-notification>"}}
{"type":"user","timestamp":"2026-09-01T10:01:00Z","gitBranch":"feat/x","origin":{"kind":"human"},"promptSource":"queued","message":{"content":[{"type":"text","text":"also the tests"},{"type":"image","source":{}}]}}
{"type":"user","timestamp":"2026-09-01T10:02:00Z","origin":{"kind":"human"},"message":{"content":"<command-name>/model</command-name>\n<command-message>model</command-message>\n<command-args>opus</command-args>"}}
{"type":"ai-title","aiTitle":"Generated title"}
{"type":"custom-title","customTitle":"live mode fix"}
{"type":"ai-title","aiTitle":"Later generated title"}
{"type":"pr-link","prUrl":"https://github.com/o/r/pull/1"}
{"type":"pr-link","prUrl":"https://github.com/o/r/pull/1"}
`

const subagentTranscript = `{"type":"user","timestamp":"2026-09-01T10:03:00Z","cwd":"/src/my repo","isSidechain":true,"message":{"content":"Research the judge"}}
{"type":"user","timestamp":"2026-09-01T10:03:05Z","isSidechain":true,"message":{"content":"follow-up from parent"}}
`

func writeSession(t *testing.T) (dir, path string) {
	t.Helper()
	dir = t.TempDir()
	dir = filepath.Join(dir, "[glob]?")
	path = filepath.Join(dir, "sess-1.jsonl")
	sub := filepath.Join(dir, "sess-1", "subagents")
	must(t, os.MkdirAll(sub, 0o700))
	must(t, os.WriteFile(path, []byte(transcript), 0o600))
	must(t, os.WriteFile(filepath.Join(sub, "agent-a1.jsonl"), []byte(subagentTranscript), 0o600))
	must(t, os.WriteFile(filepath.Join(sub, "agent-a1.meta.json"), []byte(`{"description":"Judge persona"}`), 0o600))
	return dir, path
}

func TestReadClaude(t *testing.T) {
	_, path := writeSession(t)
	s, err := ReadClaude(path)
	must(t, err)

	want := []Message{
		{"2026-09-01T10:00:00Z", "typed", "fix live mode"},
		{"2026-09-01T10:01:00Z", "queued", "also the tests"},
		{"2026-09-01T10:02:00Z", "", "/model opus"},
	}
	if len(s.Messages) != len(want) {
		t.Fatalf("messages = %+v, want %+v", s.Messages, want)
	}
	for i := range want {
		if s.Messages[i] != want[i] {
			t.Errorf("message %d = %+v, want %+v", i, s.Messages[i], want[i])
		}
	}
	check(t, "id", s.ID, "sess-1")
	check(t, "title (custom beats ai)", s.Title, "live mode fix")
	check(t, "branches", strings.Join(s.Branches, ","), "main,feat/x")
	check(t, "prs (deduped)", strings.Join(s.PRs, ","), "https://github.com/o/r/pull/1")
	check(t, "started", s.Started, "2026-09-01T10:00:00Z")
	check(t, "last_active", s.LastActive, "2026-09-01T10:02:00Z")
	check(t, "resume", ResumeLine(s.Cwd, s.ResumeArgv), "cd '/src/my repo' && 'claude' '--resume' 'sess-1'")
}

// Tool results are skipped by a byte match before decoding. Text inside a message is
// JSON-escaped, so a human quoting a tool result can never trip that match.
func TestReadClaudeKeepsQuotedToolResult(t *testing.T) {
	path := filepath.Join(t.TempDir(), "q.jsonl")
	line, _ := json.Marshal(map[string]any{
		"type": "user", "origin": map[string]string{"kind": "human"},
		"message": map[string]string{"content": `why is "type":"tool_result" here?`},
	})
	must(t, os.WriteFile(path, append(line, '\n'), 0o600))
	s, err := ReadClaude(path)
	must(t, err)
	if len(s.Messages) != 1 {
		t.Errorf("messages = %+v, want the quoted message kept", s.Messages)
	}
}

func TestReadClaudeSubagent(t *testing.T) {
	dir, _ := writeSession(t)
	s, err := ReadClaude(filepath.Join(dir, "sess-1", "subagents", "agent-a1.jsonl"))
	must(t, err)
	check(t, "parent", s.Parent, "sess-1")
	check(t, "title", s.Title, "Judge persona")
	check(t, "resume_argv", strings.Join(s.ResumeArgv, " "), "")
	if len(s.Messages) != 1 || s.Messages[0].Kind != "prompt" || s.Messages[0].Text != "Research the judge" {
		t.Errorf("messages = %+v, want only the parent's task as a prompt", s.Messages)
	}
}

func TestReadClaudeHeadless(t *testing.T) {
	path := filepath.Join(t.TempDir(), "h.jsonl")
	must(t, os.WriteFile(path, []byte(`{"type":"user","timestamp":"t","entrypoint":"sdk-cli","promptSource":"sdk","message":{"content":"reply with just: ok"}}`+"\n"), 0o600))
	s, err := ReadClaude(path)
	must(t, err)
	if len(s.Messages) != 1 || s.Messages[0].Kind != "sdk" || s.Messages[0].Text != "reply with just: ok" {
		t.Errorf("messages = %+v, want the headless prompt", s.Messages)
	}
}

func TestIndexTranscriptNeverWritten(t *testing.T) {
	t.Setenv("AGENT_HISTORY_DIR", t.TempDir())
	must(t, IndexTranscript(filepath.Join(t.TempDir(), "no-persistence.jsonl")))
}

func TestIndexTranscript(t *testing.T) {
	t.Setenv("AGENT_HISTORY_DIR", t.TempDir())
	_, path := writeSession(t)
	must(t, IndexTranscript(path))

	entry := indexPath("claude", "", "sess-1")
	data, err := os.ReadFile(entry)
	must(t, err)
	for _, line := range []string{
		`title: "live mode fix"`,
		`cwd: "/src/my repo"`,
		`branches: ["main","feat/x"]`,
		"## 2026-09-01T10:00:00Z · typed\n\nfix live mode\n",
	} {
		if !strings.Contains(string(data), line) {
			t.Errorf("entry missing %q:\n%s", line, data)
		}
	}
	if strings.Contains(string(data), "secret file contents") || strings.Contains(string(data), "On it.") {
		t.Errorf("entry leaked agent output:\n%s", data)
	}
	if _, err := os.Stat(indexPath("claude", "sess-1", "agent-a1")); err != nil {
		t.Errorf("subagent not indexed: %v", err)
	}

	// An entry built from the transcript's current version is not rewritten.
	src, err := os.Stat(path)
	must(t, err)
	must(t, os.WriteFile(entry, []byte("sentinel"), 0o600))
	must(t, os.Chtimes(entry, src.ModTime(), src.ModTime()))
	must(t, IndexTranscript(path))
	if data, _ := os.ReadFile(entry); string(data) != "sentinel" {
		t.Errorf("up-to-date entry was rewritten")
	}

	// An entry built from any other version of the transcript is rebuilt.
	old := src.ModTime().Add(-time.Minute)
	must(t, os.Chtimes(entry, old, old))
	must(t, IndexTranscript(path))
	if data, _ := os.ReadFile(entry); string(data) == "sentinel" {
		t.Errorf("stale entry was not rebuilt")
	}
}

func TestResumeQuotesCwd(t *testing.T) {
	path := filepath.Join(t.TempDir(), "s.jsonl")
	must(t, os.WriteFile(path, []byte(`{"type":"user","cwd":"/w; touch /tmp/pwned 'x'"}`+"\n"), 0o600))
	s, err := ReadClaude(path)
	must(t, err)
	check(t, "resume", ResumeLine(s.Cwd, s.ResumeArgv), `cd '/w; touch /tmp/pwned '\''x'\''' && 'claude' '--resume' 's'`)
}

func TestReconcileRecordsOnlyCompletedRuns(t *testing.T) {
	// Configured roots containing glob syntax are still taken literally.
	t.Setenv("AGENT_HISTORY_DIR", filepath.Join(t.TempDir(), "[h]?"))
	t.Setenv("CLAUDE_CONFIG_DIR", filepath.Join(t.TempDir(), "[c]*"))
	must(t, os.MkdirAll(filepath.Dir(stateFile()), 0o700))

	// While another run holds the lock, this one does nothing and records nothing.
	lock, err := os.OpenFile(filepath.Join(Root(), "state", "reconcile.lock"), os.O_CREATE|os.O_RDWR, 0o600)
	must(t, err)
	must(t, syscall.Flock(int(lock.Fd()), syscall.LOCK_EX))
	Reconcile()
	if _, err := os.Stat(stateFile()); err == nil {
		t.Fatalf("a skipped run was recorded as completed")
	}
	lock.Close()

	// A run that could not handle every transcript is not recorded either.
	project := filepath.Join(claudeDir(), "projects", "p")
	must(t, os.MkdirAll(project, 0o700))
	unreadable := filepath.Join(project, "s.jsonl")
	must(t, os.WriteFile(unreadable, nil, 0o000))
	Reconcile()
	if _, err := os.Stat(stateFile()); err == nil && os.Getuid() != 0 {
		t.Fatalf("a failed run was recorded as completed")
	}

	// Nor is one whose projects directory can't be read, which a glob would hide.
	must(t, os.Chmod(unreadable, 0o600))
	projects := filepath.Dir(project)
	must(t, os.Chmod(projects, 0o000))
	Reconcile()
	must(t, os.Chmod(projects, 0o700))
	if _, err := os.Stat(stateFile()); err == nil && os.Getuid() != 0 {
		t.Fatalf("a run that couldn't read the projects directory was recorded")
	}

	Reconcile()
	if _, err := os.Stat(stateFile()); err != nil {
		t.Errorf("a completed run was not recorded: %v", err)
	}
	if _, err := os.Stat(indexPath("claude", "", "s")); err != nil {
		t.Errorf("transcript under a glob-shaped root not indexed: %v", err)
	}

	// A subagent whose parent transcript is gone is still indexed.
	orphan := filepath.Join(project, "gone", "subagents")
	must(t, os.MkdirAll(orphan, 0o700))
	must(t, os.WriteFile(filepath.Join(orphan, "agent-o.jsonl"), []byte(subagentTranscript), 0o600))
	Reconcile()
	if _, err := os.Stat(indexPath("claude", "gone", "agent-o")); err != nil {
		t.Errorf("orphaned subagent not indexed: %v", err)
	}
}

func TestMarkMissing(t *testing.T) {
	t.Setenv("AGENT_HISTORY_DIR", t.TempDir())
	_, path := writeSession(t)
	must(t, IndexTranscript(path))
	entry := indexPath("claude", "", "sess-1")

	// A message that happens to contain the marker must not count as the marker.
	body, err := os.ReadFile(entry)
	must(t, err)
	must(t, os.WriteFile(entry, append(body, "\nsource_missing: true\n"...), 0o600))

	must(t, MarkMissing(entry))
	if data, _ := os.ReadFile(entry); strings.Count(string(data), "source_missing") != 1 {
		t.Fatalf("marked missing while the transcript exists")
	}
	must(t, os.Remove(path))
	must(t, MarkMissing(entry))
	must(t, MarkMissing(entry)) // idempotent
	data, _ := os.ReadFile(entry)
	header, _, _ := strings.Cut(string(data)[4:], "\n---\n")
	if strings.Count(header, "\nsource_missing: true") != 1 || !strings.Contains(string(data), "fix live mode") {
		t.Errorf("entry after deletion:\n%s", data)
	}

	// Restoring the transcript, even with its original mtime, clears the flag.
	must(t, os.WriteFile(path, []byte(transcript), 0o600))
	must(t, os.Chtimes(path, time.Unix(1e9, 0), time.Unix(1e9, 0)))
	must(t, IndexTranscript(path))
	if data, _ := os.ReadFile(entry); strings.Contains(string(data), "source_missing") {
		t.Errorf("restored entry still marked missing:\n%s", data)
	}
}

func must(t *testing.T, err error) {
	t.Helper()
	if err != nil {
		t.Fatal(err)
	}
}

func check(t *testing.T, name, got, want string) {
	t.Helper()
	if got != want {
		t.Errorf("%s = %q, want %q", name, got, want)
	}
}

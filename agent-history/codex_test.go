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

// No test may read the real ~/.codex; each one that needs Codex data writes its own.
func TestMain(m *testing.M) {
	dir, err := os.MkdirTemp("", "codex-home")
	if err != nil {
		panic(err)
	}
	os.Setenv("CODEX_HOME", dir)
	os.Setenv("CLAUDE_CONFIG_DIR", dir)
	os.Setenv("HOME", dir)
	os.Setenv("PI_CONFIG_DIR", ".omp")
	os.Setenv("PI_CODING_AGENT_DIR", filepath.Join(dir, "omp-agent"))
	os.Setenv("PI_CODING_AGENT_SESSION_DIR", filepath.Join(dir, "omp-custom"))
	os.Setenv("OMP_PROFILE", "")
	os.Setenv("PI_PROFILE", "")
	os.Setenv("XDG_DATA_HOME", filepath.Join(dir, "xdg-data"))
	os.Setenv("XDG_STATE_HOME", filepath.Join(dir, "xdg-state"))
	noDetach = true
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

{"timestamp":"2026-09-02T09:00:11Z","type":"event_msg","payl`, // cut short by a crash

	// Work the thread delegated to a subagent: indexed under it.
	"2026/09/01/rollout-2026-09-01T10-00-05-0000dddd-0000-7000-8000-000000000004.jsonl": `{"timestamp":"2026-09-01T10:00:05Z","type":"session_meta","payload":{"id":"0000dddd-0000-7000-8000-000000000004","parent_thread_id":"` + codexID + `","cwd":"/src/my repo","source":{"subagent":{"thread_spawn":{"parent_thread_id":"` + codexID + `","agent_path":"/root/menu_grounding"}}}}}
{"timestamp":"2026-09-01T10:00:06Z","type":"response_item","payload":{"type":"agent_message","content":[{"type":"encrypted_content","encrypted_content":"x"}]}}
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
	if len(sessions) != 3 || len(sessions[0]) != 2 {
		t.Fatalf("sessions = %v, want the resumed thread's two files together, then its subagents", sessions)
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
	check(t, "last_active", s.LastActive, "2026-09-02T09:00:11Z")
	check(t, "resume", ResumeLine(s.Cwd, s.ResumeArgv), "cd '/src/my repo' && 'codex' 'resume' '"+codexID+"'")

	// An unreadable name log is an error, not an untitled thread, and isn't cached.
	if os.Getuid() != 0 {
		names := filepath.Join(codexDir(), "session_index.jsonl")
		nameLog.path = ""
		must(t, os.Chmod(names, 0o000))
		if _, err := ReadCodex(sessions[0]); err == nil {
			t.Errorf("unreadable name log read as no names")
		}
		must(t, os.Chmod(names, 0o600))
		s, err = ReadCodex(sessions[0])
		must(t, err)
		check(t, "title after the log is readable", s.Title, "live mode fix")
	}
	// A thread nobody named stays untitled.
	must(t, os.Remove(filepath.Join(codexDir(), "session_index.jsonl")))
	s, err = ReadCodex(sessions[0])
	must(t, err)
	check(t, "unnamed title", s.Title, "")
}

func TestReconcileIndexesCodex(t *testing.T) {
	t.Setenv("AGENT_HISTORY_DIR", t.TempDir())
	t.Setenv("CLAUDE_CONFIG_DIR", t.TempDir())
	writeCodex(t)
	if !reconcileAll(false, false) {
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
	if len(entries) != 2 {
		t.Errorf("entries = %v, want the thread and its delegated subagent, not the review", entries)
	}
	sub, err := ReadEntry(indexPath("codex", codexID, "0000dddd-0000-7000-8000-000000000004"))
	must(t, err)
	if sub.Parent != codexID || sub.Title != "menu_grounding" || sub.ResumeArgv != nil {
		t.Errorf("delegated subagent entry = %+v", sub)
	}
	// An entry is fresh while no file of its thread is newer; resuming writes one.
	info, err := os.Stat(e.Path)
	must(t, err)
	must(t, os.WriteFile(e.Path, []byte("sentinel"), 0o600))
	must(t, os.Chtimes(e.Path, info.ModTime(), info.ModTime()))
	reconcileAll(false, false)
	if data, _ := os.ReadFile(e.Path); string(data) != "sentinel" {
		t.Errorf("up-to-date entry was rewritten")
	}
	later := info.ModTime().Add(time.Minute)
	must(t, os.Chtimes(sessionsOf(t)[0][1], later, later))
	reconcileAll(false, false)
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
	reconcileAll(false, false)
	e, err = ReadEntry(e.Path)
	must(t, err)
	check(t, "title after rename", e.Title, "renamed later")

	// Retention deletes a thread's oldest file first; the entry still has a source.
	must(t, os.Remove(sessionsOf(t)[0][0]))
	reconcileAll(false, false)
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
	path := filepath.Join(codexSessionsDir(), name)
	id := codexThreadID(name)
	running := func() (Running, bool) {
		got, err := RunningCodex()
		must(t, err)
		r, ok := got[id]
		return r, ok
	}

	// Holding a rollout open doesn't make a process the session: this test isn't codex.
	f, err := os.Open(path)
	must(t, err)
	defer f.Close()
	if r, ok := running(); ok {
		t.Errorf("a non-codex process holding the rollout counted: %+v", r)
	}

	// A process named codex (a shell under that name) holding it open does, unless it
	// is a sandbox helper: those run under the name codex too.
	codex := filepath.Join(t.TempDir(), "codex")
	must(t, os.Symlink("/bin/sh", codex))
	hold := func(argv0 string) *exec.Cmd {
		// read is a builtin: the shell itself waits, holding the file (a trailing
		// external command would be exec'd in its place).
		cmd := exec.Command(codex, "-c", `exec 3<"$1"; read -r _`, "sh", path)
		cmd.Args[0] = argv0
		stdin, err := cmd.StdinPipe()
		must(t, err)
		t.Cleanup(func() { stdin.Close() })
		cmd.Env = append(os.Environ(), "TMUX_PANE=%99")
		cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
		must(t, cmd.Start())
		for deadline := time.Now().Add(5 * time.Second); time.Now().Before(deadline); time.Sleep(20 * time.Millisecond) {
			if _, err := os.Readlink(fmt.Sprintf("/proc/%d/fd/3", cmd.Process.Pid)); err == nil {
				break
			}
		}
		return cmd
	}
	stop := func(cmd *exec.Cmd) {
		syscall.Kill(-cmd.Process.Pid, syscall.SIGKILL)
		cmd.Wait()
	}
	helper := hold("/tmp/arg0/codex-linux-sandbox")
	if r, ok := running(); ok {
		t.Errorf("a sandbox helper holding the rollout counted: %+v", r)
	}
	stop(helper)
	cmd := hold(codex)
	// A process with no terminal (like the app-server daemon, or tests under CI) is in
	// no pane, whatever its environment says.
	pane := "%99"
	if tty, _ := procStat(os.Getpid(), 7); tty == "0" {
		pane = ""
	}
	if r, ok := running(); !ok || r.PID != cmd.Process.Pid || r.TmuxPane != pane {
		t.Errorf("codex holding the rollout = %+v, %v", r, ok)
	}
	stop(cmd)
	if r, ok := running(); ok {
		t.Errorf("exited codex counted as running: %+v", r)
	}
}

// IDs from transcript data name index paths, so anything but a plain name is refused.
func TestIndexRefusesPathShapedIDs(t *testing.T) {
	t.Setenv("AGENT_HISTORY_DIR", t.TempDir())
	writeCodex(t)
	path := filepath.Join(codexSessionsDir(), "2026/09/03/rollout-2026-09-03T10-00-00-0000eeee-0000-7000-8000-000000000005.jsonl")
	must(t, os.MkdirAll(filepath.Dir(path), 0o700))
	must(t, os.WriteFile(path, []byte(`{"timestamp":"t","type":"session_meta","payload":{"parent_thread_id":"../../escape","source":{"subagent":{"thread_spawn":{}}}}}`+"\n"), 0o600))
	if err := indexFile(harnesses[1], []string{path}, false); err == nil {
		t.Error("a parent of ../../escape was indexed")
	}
	if _, err := os.Stat(filepath.Join(Root(), "escape")); err == nil {
		t.Error("an entry was written outside the index")
	}
}

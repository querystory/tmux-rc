package main

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

const codexTranscript = `{"timestamp":"2026-09-28T10:00:00Z","type":"session_meta","payload":{"id":"codex-1","timestamp":"2026-09-28T09:59:00Z","cwd":"REPO","source":"vscode"}}
{"timestamp":"2026-09-28T10:00:01Z","type":"response_item","payload":{"type":"message","role":"user","content":[{"type":"input_text","text":"<environment_context>synthetic envelope must not be copied</environment_context>"}]}}
{"timestamp":"2026-09-28T10:01:00Z","type":"response_item","payload":{"type":"message","role":"user","content":[{"type":"input_text","text":"review landed on PR 4955; please address it"}]}}
{"timestamp":"2026-09-28T10:02:00Z","type":"event_msg","payload":{"type":"task_complete","last_agent_message":"Opened https://github.com/querystory/qs-app/pull/5001#issuecomment-1"}}
`

func codexFixture(t *testing.T) (home, repo, transcript string) {
	t.Helper()
	home = t.TempDir()
	repo = filepath.Join(t.TempDir(), "qs-app")
	writeGitRepo(t, repo, "git@github.com:querystory/qs-app.git")
	dir := filepath.Join(home, "sessions", "2026", "09", "28")
	must(t, os.MkdirAll(dir, 0o700))
	transcript = filepath.Join(dir, "rollout-codex-1.jsonl")
	body := strings.Replace(codexTranscript, "REPO", repo, 1)
	must(t, os.WriteFile(transcript, []byte(body), 0o600))
	must(t, os.WriteFile(filepath.Join(home, "session_index.jsonl"), []byte(
		`{"id":"codex-1","thread_name":"Old title"}`+"\n"+
			`{"id":"other","thread_name":"Nope"}`+"\n"+
			`{"id":"codex-1","thread_name":"Review 4955 comments"}`+"\n"), 0o600))
	return home, repo, transcript
}

func TestReadCodexTracksIdentityMessagesAndPulls(t *testing.T) {
	_, repo, path := codexFixture(t)
	s, err := ReadCodex(path)
	must(t, err)

	check(t, "harness", s.Harness, "codex")
	check(t, "id", s.ID, "codex-1")
	check(t, "cwd", s.Cwd, repo)
	check(t, "entrypoint", s.Entrypoint, "vscode")
	check(t, "title", s.Title, "Review 4955 comments")
	check(t, "started", s.Started, "2026-09-28T09:59:00Z")
	check(t, "last active", s.LastActive, "2026-09-28T10:02:00Z")
	if len(s.Messages) != 1 || s.Messages[0].Text != "review landed on PR 4955; please address it" {
		t.Errorf("messages = %+v", s.Messages)
	}
	want := "https://github.com/querystory/qs-app/pull/4955," +
		"https://github.com/querystory/qs-app/pull/5001"
	check(t, "pulls", strings.Join(s.PRs, ","), want)
}

func TestCodexSubagentKeepsParent(t *testing.T) {
	_, _, path := codexFixture(t)
	data, err := os.ReadFile(path)
	must(t, err)
	data = []byte(strings.Replace(string(data), `"id":"codex-1"`,
		`"id":"child-1","parent_thread_id":"codex-1"`, 1))
	must(t, os.WriteFile(path, data, 0o600))
	s, err := ReadCodex(path)
	must(t, err)
	check(t, "id", s.ID, "child-1")
	check(t, "parent", s.Parent, "codex-1")
}

func TestIndexCodexAndReconcile(t *testing.T) {
	history := t.TempDir()
	home, _, path := codexFixture(t)
	t.Setenv("AGENT_HISTORY_DIR", history)
	t.Setenv("CODEX_HOME", home)
	t.Setenv("CLAUDE_CONFIG_DIR", filepath.Join(t.TempDir(), "missing"))

	must(t, IndexTranscript(path))
	entry := indexPath("codex", "", "codex-1")
	data, err := os.ReadFile(entry)
	must(t, err)
	for _, want := range []string{
		`harness: "codex"`, `title: "Review 4955 comments"`,
		`prs: ["https://github.com/querystory/qs-app/pull/4955","https://github.com/querystory/qs-app/pull/5001"]`,
	} {
		if !strings.Contains(string(data), want) {
			t.Errorf("entry missing %q:\n%s", want, data)
		}
	}
	must(t, os.Remove(entry))
	Reconcile()
	if _, err := os.Stat(entry); err != nil {
		t.Errorf("reconcile did not index Codex rollout: %v", err)
	}
}

func TestFreshCodexEntryDoesNotReparseTranscript(t *testing.T) {
	history := t.TempDir()
	_, _, path := codexFixture(t)
	t.Setenv("AGENT_HISTORY_DIR", history)
	must(t, IndexTranscript(path))
	entry := indexPath("codex", "", "codex-1")
	src, err := os.Stat(path)
	must(t, err)

	// Keep session_meta readable for identity, but corrupt the rest. Matching mtimes
	// mean the existing entry is current, so the large rollout body is never parsed.
	data, err := os.ReadFile(path)
	must(t, err)
	first, _, _ := strings.Cut(string(data), "\n")
	must(t, os.WriteFile(path, []byte(first+"\nnot-json\n"), 0o600))
	must(t, os.Chtimes(path, src.ModTime(), src.ModTime()))
	must(t, os.WriteFile(entry, []byte("sentinel"), 0o600))
	must(t, os.Chtimes(entry, src.ModTime(), src.ModTime()))
	must(t, IndexTranscript(path))
	if data, _ := os.ReadFile(entry); string(data) != "sentinel" {
		t.Errorf("fresh Codex entry was rewritten")
	}
}

func TestPullShorthandNeedsGitHubOrigin(t *testing.T) {
	for _, remote := range []string{"", "git@example.com:o/r.git"} {
		s := Session{Cwd: t.TempDir()}
		if remote != "" {
			writeGitRepo(t, s.Cwd, remote)
		}
		s.addPullRequests("please fix PR 42", true)
		if len(s.PRs) != 0 {
			t.Errorf("remote %q produced pulls %v", remote, s.PRs)
		}
	}
}

func writeGitRepo(t *testing.T, dir, remote string) {
	t.Helper()
	git := filepath.Join(dir, ".git")
	must(t, os.MkdirAll(filepath.Join(git, "objects"), 0o700))
	must(t, os.MkdirAll(filepath.Join(git, "refs", "heads"), 0o700))
	must(t, os.WriteFile(filepath.Join(git, "HEAD"), []byte("ref: refs/heads/main\n"), 0o600))
	config := "[core]\n\trepositoryformatversion = 0\n\tbare = false\n" +
		"[remote \"origin\"]\n\turl = " + remote + "\n"
	must(t, os.WriteFile(filepath.Join(git, "config"), []byte(config), 0o600))
}

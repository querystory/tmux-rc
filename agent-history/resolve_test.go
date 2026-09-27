package main

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

var now = time.Date(2026, 9, 27, 0, 0, 0, 0, time.UTC)

// entry round-trips a session through the on-disk format, so tests exercise ReadEntry
// against exactly what Render writes.
func entry(t *testing.T, s Session, said ...string) Entry {
	t.Helper()
	s.Harness = "claude"
	if s.LastActive == "" {
		s.LastActive = now.Format(time.RFC3339)
	}
	for _, text := range said {
		s.Messages = append(s.Messages, Message{Time: s.LastActive, Kind: "typed", Text: text})
	}
	path := filepath.Join(t.TempDir(), s.ID+".md")
	must(t, os.WriteFile(path, Render(s), 0o600))
	e, err := ReadEntry(path)
	must(t, err)
	return e
}

func ids(p Project) (out []string) {
	for _, s := range p.Sessions {
		out = append(out, s.ID)
	}
	return out
}

var defaults = ResolveOptions{MaxProjects: 5, MaxSessions: 5, Now: now}

func TestReadEntryRoundTrip(t *testing.T) {
	e := entry(t, Session{ID: "s", Cwd: "/r", Title: `quote "and" colon: x`, PRs: []string{"u"},
		ResumeArgv: []string{"claude", "--resume", "s"}}, "hi")
	check(t, "title", e.Title, `quote "and" colon: x`)
	check(t, "resume_argv", e.ResumeArgv[2], "s")
	check(t, "resume", e.Resume, "cd '/r' && 'claude' '--resume' 's'")
}

func TestResolveRanksRareTermsAndPhrases(t *testing.T) {
	var entries []Entry
	for _, id := range []string{"a", "b", "c", "d"} {
		entries = append(entries, entry(t, Session{ID: id, Cwd: "/other"}, "fix the build", "mode switch"))
	}
	entries = append(entries,
		entry(t, Session{ID: "live", Cwd: "/tmux-rc"}, "the Live-Mode audio drops"),
		entry(t, Session{ID: "words", Cwd: "/tmux-rc"}, "live data", "fix"),
	)
	got := Resolve(entries, "fix live mode", defaults)
	check(t, "top repo", got[0].Repo, "/tmux-rc")
	check(t, "top session (phrase beats scattered words)", ids(got[0])[0], "live")
}

func TestResolveWeightsNamesAndRecency(t *testing.T) {
	old := now.Add(-60 * 24 * time.Hour).Format(time.RFC3339)
	got := Resolve([]Entry{
		entry(t, Session{ID: "old", Cwd: "/r", LastActive: old}, "otlp exporter"),
		entry(t, Session{ID: "new", Cwd: "/r"}, "otlp exporter"),
		entry(t, Session{ID: "titled", Cwd: "/r", Title: "otlp grpc", LastActive: old}),
	}, "otlp", defaults)
	check(t, "newer first", ids(got[0])[0], "new")
	if r := recency(now.Add(time.Hour).Format(time.RFC3339), now); r != 1 {
		t.Errorf("future recency = %v, want 1", r)
	}
	if len(got[0].Sessions) != 3 {
		t.Fatalf("sessions = %v", ids(got[0]))
	}
}

func TestResolveEdgeCases(t *testing.T) {
	// MarkMissing's flag: the session is still found, but offers no resume command.
	path := filepath.Join(t.TempDir(), "gone.md")
	data := Render(Session{Harness: "claude", ID: "gone", Source: "/deleted.jsonl", Cwd: "/r", LastActive: now.Format(time.RFC3339),
		ResumeArgv: []string{"claude", "--resume", "gone"}, Messages: []Message{{Text: "otlp"}}})
	must(t, os.WriteFile(path, []byte(strings.Replace(string(data), "\nsource:", "\nsource_missing: true\nsource:", 1)), 0o600))
	gone, err := ReadEntry(path)
	must(t, err)
	got := Resolve([]Entry{gone}, "otlp", defaults)
	if s := got[0].Sessions[0]; len(s.ResumeArgv) != 0 || s.Resume != "" || !s.SourceMissing {
		t.Errorf("deleted session still resumable: %+v", s)
	}

	negative := defaults
	negative.MaxProjects, negative.MaxSessions = -1, -1
	if got := Resolve([]Entry{gone}, "otlp", negative); len(got) != 0 {
		t.Errorf("negative limits = %v", got)
	}
}

func TestResolveFilters(t *testing.T) {
	entries := []Entry{
		entry(t, Session{ID: "human", Cwd: "/r", Entrypoint: "cli"}, "judge"),
		entry(t, Session{ID: "headless", Cwd: "/r", Entrypoint: "sdk-cli"}, "judge"),
		entry(t, Session{ID: "sub", Parent: "human", Cwd: "/r"}, "judge"),
	}
	check(t, "default", strings.Join(ids(Resolve(entries, "judge", defaults)[0]), ","), "human")
	all := defaults
	all.All = true
	if n := len(Resolve(entries, "judge", all)[0].Sessions); n != 3 {
		t.Errorf("-all sessions = %d, want 3", n)
	}
	other := defaults
	other.Harness = "codex"
	if got := Resolve(entries, "judge", other); len(got) != 0 {
		t.Errorf("harness filter kept %v", got)
	}
	if got := Resolve(entries, "nothing matches", defaults); len(got) != 0 {
		t.Errorf("no-match query returned %v", got)
	}
}

func TestRepoOfFoldsWorktrees(t *testing.T) {
	root := t.TempDir()
	repo := filepath.Join(root, "repo")
	wt := filepath.Join(repo, ".claude", "worktrees", "feat")
	must(t, os.MkdirAll(filepath.Join(repo, ".git", "worktrees", "feat"), 0o700))
	must(t, os.MkdirAll(filepath.Join(wt, "sub"), 0o700))
	must(t, os.WriteFile(filepath.Join(wt, ".git"), []byte("gitdir: "+repo+"/.git/worktrees/feat\n"), 0o600))
	must(t, os.WriteFile(filepath.Join(repo, ".git", "HEAD"), []byte("ref: refs/heads/main\n"), 0o600))
	must(t, os.MkdirAll(filepath.Join(root, "plain", ".git"), 0o700)) // stray, no HEAD

	check(t, "repo subdir", repoOf(filepath.Join(repo, "x")), repo)
	check(t, "worktree subdir", repoOf(filepath.Join(wt, "sub")), repo)
	check(t, "removed worktree", repoOf(filepath.Join(repo, ".claude", "worktrees", "gone")), repo)
	check(t, "not a repo", repoOf(filepath.Join(root, "plain")), filepath.Join(root, "plain"))
}

// Command agent-history keeps a small, greppable index of coding-agent sessions.
//
// Harness transcripts stay where the harness wrote them; the index holds identity,
// where the work happened, and what the human said, and points back at the source.
package main

import (
	"encoding/json"
	"fmt"
	"io/fs"
	"os"
	"os/exec"
	"path/filepath"
	"syscall"
	"time"
)

// A full reconcile repairs drift a hook could not see (hard reboot, killed session,
// hooks not installed yet). Hooks trigger one when the last is older than this.
const reconcileEvery = 6 * time.Hour

func main() {
	if len(os.Args) < 2 {
		fmt.Fprintln(os.Stderr, "usage: agent-history hook | index <transcript.jsonl>... | reconcile")
		os.Exit(2)
	}
	switch os.Args[1] {
	case "hook":
		hook()
	case "index":
		withLock("index", true, func() {
			for _, p := range os.Args[2:] {
				report(IndexTranscript(p))
			}
		})
		if since(stateFile()) > reconcileEvery {
			Reconcile()
		}
	case "reconcile":
		Reconcile()
	default:
		fmt.Fprintln(os.Stderr, "unknown command:", os.Args[1])
		os.Exit(2)
	}
}

// hook is the Claude Code hook entry point (Stop, SessionEnd, SubagentStop). It hands
// the transcript to a detached child and returns at once, so a hook never slows or
// breaks the session, and it never writes to stdout, so it never injects context.
func hook() {
	var in struct {
		TranscriptPath string `json:"transcript_path"`
	}
	if json.NewDecoder(os.Stdin).Decode(&in) != nil || in.TranscriptPath == "" {
		return
	}
	self, err := os.Executable()
	if err != nil {
		return
	}
	logPath := filepath.Join(Root(), "state", "agent-history.log")
	os.MkdirAll(filepath.Dir(logPath), 0o700)
	log, err := os.OpenFile(logPath, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o600)
	if err != nil {
		return
	}
	cmd := exec.Command(self, "index", in.TranscriptPath)
	cmd.Stderr = log
	cmd.SysProcAttr = &syscall.SysProcAttr{Setsid: true}
	cmd.Start()
}

// Reconcile rebuilds every out-of-date entry and flags entries whose transcript is
// gone. Only one runs at a time; others return at once. Completion is recorded only
// when every entry was handled, so an interrupted or failed run is retried by the next
// hook instead of being suppressed for reconcileEvery.
func Reconcile() {
	withLock("reconcile", false, func() {
		withLock("index", true, func() {
			if reconcileAll() {
				report(os.WriteFile(stateFile(), nil, 0o600))
			}
		})
	})
}

func reconcileAll() (ok bool) {
	ok = true
	check := func(err error) {
		report(err)
		ok = ok && err == nil
	}
	transcripts, err := find(claudeDir(), "projects/*/*.jsonl")
	check(err)
	for _, t := range transcripts {
		check(IndexTranscript(t))
	}
	entries, err := find(Root(), "index/claude/*.md", "index/claude/*/*.md")
	check(err)
	for _, e := range entries {
		check(MarkMissing(e))
	}
	return ok
}

// find matches patterns under root, which is taken literally: a configured directory
// may itself contain glob syntax.
func find(root string, patterns ...string) ([]string, error) {
	var out []string
	for _, p := range patterns {
		matches, err := fs.Glob(os.DirFS(root), p)
		if err != nil {
			return nil, err
		}
		for _, m := range matches {
			out = append(out, filepath.Join(root, m))
		}
	}
	return out, nil
}

// withLock runs fn holding an exclusive kernel lock, which a killed process releases.
// Every writer of the index holds "index", so a read-modify-write can never replace a
// newer entry. A non-blocking caller skips fn when the lock is taken.
func withLock(name string, wait bool, fn func()) {
	path := filepath.Join(Root(), "state", name+".lock")
	os.MkdirAll(filepath.Dir(path), 0o700)
	f, err := os.OpenFile(path, os.O_CREATE|os.O_RDWR, 0o600)
	if err != nil {
		report(err)
		return
	}
	defer f.Close()
	how := syscall.LOCK_EX
	if !wait {
		how |= syscall.LOCK_NB
	}
	if syscall.Flock(int(f.Fd()), how) == nil {
		fn()
	}
}

func claudeDir() string {
	if dir := os.Getenv("CLAUDE_CONFIG_DIR"); dir != "" {
		return dir
	}
	home, _ := os.UserHomeDir()
	return filepath.Join(home, ".claude")
}

func stateFile() string { return filepath.Join(Root(), "state", "last-reconcile") }

func since(path string) time.Duration {
	info, err := os.Stat(path)
	if err != nil {
		return time.Duration(1<<63 - 1)
	}
	return time.Since(info.ModTime())
}

func report(err error) {
	if err != nil {
		fmt.Fprintln(os.Stderr, time.Now().Format(time.RFC3339), err)
	}
}

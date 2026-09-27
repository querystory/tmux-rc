// Command agent-history keeps a small, greppable index of coding-agent sessions.
//
// Harness transcripts stay where the harness wrote them; the index holds identity,
// where the work happened, and what the human said, and points back at the source.
package main

import (
	"encoding/json"
	"fmt"
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
		for _, p := range os.Args[2:] {
			report(IndexTranscript(p))
		}
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

// Reconcile indexes every Claude transcript newer than its entry and flags entries
// whose transcript is gone. Concurrent runs are skipped via a kernel lock, which a
// killed run releases; completion is recorded only at the end, so an interrupted run
// is retried by the next hook rather than suppressed.
func Reconcile() {
	os.MkdirAll(filepath.Dir(stateFile()), 0o700)
	lock, err := os.OpenFile(stateFile()+".lock", os.O_CREATE|os.O_RDWR, 0o600)
	if err != nil {
		report(err)
		return
	}
	defer lock.Close()
	if syscall.Flock(int(lock.Fd()), syscall.LOCK_EX|syscall.LOCK_NB) != nil {
		return
	}
	transcripts, _ := filepath.Glob(filepath.Join(claudeDir(), "projects", "*", "*.jsonl"))
	for _, t := range transcripts {
		report(IndexTranscript(t))
	}
	entries, _ := filepath.Glob(filepath.Join(Root(), "index", "claude", "*.md"))
	subentries, _ := filepath.Glob(filepath.Join(Root(), "index", "claude", "*", "*.md"))
	for _, e := range append(entries, subentries...) {
		report(MarkMissing(e))
	}
	report(os.WriteFile(stateFile(), nil, 0o600))
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

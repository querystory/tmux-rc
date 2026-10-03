// Command agent-history keeps a small, greppable index of coding-agent sessions.
//
// Harness transcripts stay where the harness wrote them; the index holds identity,
// where the work happened, and what the human said, and points back at the source.
package main

import (
	"cmp"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"slices"
	"strings"
	"syscall"
	"time"
)

// A full reconcile repairs drift a hook could not see (hard reboot, killed session,
// hooks not installed yet). Hooks trigger one when the last is older than this.
const reconcileEvery = 6 * time.Hour

func main() {
	if len(os.Args) < 2 {
		fmt.Fprintln(os.Stderr, "usage: agent-history hook | index <transcript.jsonl>... | reconcile | resolve [flags] <query> | get <session-id>")
		os.Exit(2)
	}
	switch os.Args[1] {
	case "hook":
		hook()
	case "index":
		withLock("index", true, func() {
			for _, p := range os.Args[2:] {
				report(IndexTranscript(p, false))
			}
		})
		if reconcileDue() {
			Reconcile()
		}
	case "reconcile":
		Reconcile()
	case "resolve":
		if err := resolveCmd(os.Args[2:]); err != nil {
			report(err)
			os.Exit(1)
		}
	case "get":
		getCmd(os.Args[2:])
	default:
		fmt.Fprintln(os.Stderr, "unknown command:", os.Args[1])
		os.Exit(2)
	}
}

func resolveCmd(args []string) error {
	// Harnesses without hooks are refreshed by searches too. In the background:
	// a search is on someone's clock, and a first reconcile is not.
	if reconcileDue() {
		detach("reconcile")
	}
	flags := flag.NewFlagSet("resolve", flag.ExitOnError)
	opt := ResolveOptions{Now: time.Now()}
	opt.Running, opt.RunningErr = LiveSessions()
	flags.StringVar(&opt.Harness, "harness", "", "only this harness (claude, codex, omp)")
	flags.BoolVar(&opt.All, "all", false, "include headless runs and subagents")
	flags.IntVar(&opt.MaxProjects, "projects", 3, "max repos")
	flags.IntVar(&opt.MaxSessions, "sessions", 5, "max sessions per repo")
	asJSON := flags.Bool("json", false, "machine-readable output")
	flags.Parse(args)
	query := strings.Join(flags.Args(), " ")
	if query == "" {
		fmt.Fprintln(os.Stderr, "resolve: missing query")
		os.Exit(2)
	}
	paths, err := indexEntries()
	if err != nil {
		return err
	}
	var entries []Entry
	for _, p := range paths {
		e, err := ReadEntry(p)
		if err != nil {
			return fmt.Errorf("read index entry %s: %w", p, err)
		}
		entries = append(entries, e)
	}
	projects := Resolve(entries, query, opt)
	if *asJSON {
		out := json.NewEncoder(os.Stdout)
		out.SetEscapeHTML(false)
		out.SetIndent("", "  ")
		return out.Encode(map[string]any{"query": query, "projects": projects})
	}
	for _, p := range projects {
		fmt.Printf("%s  (score %.2f)\n", p.Repo, p.Score)
		for _, s := range p.Sessions {
			fmt.Printf("  %.10s  %-8.8s  %s\n", s.LastActive, s.ID, cmp.Or(s.Title, "(untitled)"))
			if s.RunningUnknown {
				fmt.Println("      can't tell whether it's running; not offering a resume command")
			} else if s.Running != nil {
				fmt.Printf("      running in tmux pane %s (%s)\n", cmp.Or(s.Running.TmuxPane, "none"), s.Running.Status)
			} else if s.Resume != "" {
				fmt.Printf("      %s\n", s.Resume)
			}
		}
	}
	return nil
}

// getCmd prints one top-level session as JSON, with whether it is running, for a
// caller that already chose it (e.g. from resolve). IDs are checked to be plain
// names so a caller-supplied ID can't reach outside the index.
func getCmd(args []string) {
	if len(args) != 1 || !validID.MatchString(args[0]) {
		fmt.Fprintln(os.Stderr, "usage: agent-history get <session-id>")
		os.Exit(2)
	}
	id := args[0]
	var e Entry
	var err error
	for _, h := range harnesses {
		if e, err = ReadEntry(indexPath(h.name, "", id)); !errors.Is(err, os.ErrNotExist) {
			break
		}
	}
	if err != nil {
		fmt.Fprintln(os.Stderr, "get:", err)
		os.Exit(1)
	}
	out := Scored{Entry: e}
	running, errs := LiveSessions()
	out.RunningUnknown = errs[e.Harness] != nil
	if r, ok := running[id]; ok {
		out.Running = &r
	}
	enc := json.NewEncoder(os.Stdout)
	enc.SetEscapeHTML(false)
	enc.Encode(out)
}

var validID = regexp.MustCompile(`^[A-Za-z0-9_-]{1,128}$`)

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
	detach("index", in.TranscriptPath)
}

// noDetach is set by tests, whose executable is the test binary.
var noDetach bool

// detach starts agent-history with args in its own session, logging its errors, and
// returns without waiting for it.
func detach(args ...string) {
	self, err := os.Executable()
	if err != nil || noDetach {
		return
	}
	logPath := filepath.Join(Root(), "state", "agent-history.log")
	os.MkdirAll(filepath.Dir(logPath), 0o700)
	log, err := os.OpenFile(logPath, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o600)
	if err != nil {
		return
	}
	cmd := exec.Command(self, args...)
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
			format, profile := recordedState()
			context := ompProfileContext()
			if reconcileAll(format != Format, profile != context) {
				report(os.WriteFile(stateFile(), []byte(Format+"\n"+context), 0o600))
			}
		})
	})
}

// A changed omp context is due immediately, even while transcript mtimes match.
func reconcileDue() bool {
	format, profile := recordedState()
	return since(stateFile()) > reconcileEvery || format != Format || profile != ompProfileContext()
}

func recordedState() (string, string) {
	data, _ := os.ReadFile(stateFile())
	format, profile, _ := strings.Cut(string(data), "\n")
	return format, profile
}

func reconcileAll(force, forceOmp bool) (ok bool) {
	ok = true
	check := func(err error) {
		report(err)
		ok = ok && err == nil
	}
	for _, h := range harnesses {
		sessions, err := h.sessions()
		check(err)
		for _, files := range sessions {
			check(indexFile(h, files, force || (h.name == "omp" && forceOmp)))
		}
	}
	entries, err := indexEntries()
	check(err)
	for _, e := range entries {
		check(MarkMissing(e))
	}
	return ok
}

// indexEntries lists every entry: <harness>/<session>.md and, for subagents,
// <harness>/<parent>/<agent>.md.
func indexEntries() ([]string, error) {
	index := filepath.Join(Root(), "index")
	return find(index, ".md", 2, 3)
}

// find lists files ending in ext at the given depths (1 = root's own files,
// 0 = every depth), taking root literally since it may contain glob syntax.
// A missing root is empty; other unreadable directories are errors, so
// reconcile won't record a run that couldn't see everything.
func find(root, ext string, depths ...int) ([]string, error) {
	entries, err := os.ReadDir(root)
	if errors.Is(err, os.ErrNotExist) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	recursive := slices.Contains(depths, 0)
	deeper := depths
	if !recursive {
		deeper = nil
		for _, d := range depths {
			if d > 1 {
				deeper = append(deeper, d-1)
			}
		}
	}
	here := recursive || slices.Contains(depths, 1)
	var out []string
	var errs []error
	for _, e := range entries {
		path := filepath.Join(root, e.Name())
		switch {
		case here && !e.IsDir() && strings.HasSuffix(e.Name(), ext):
			out = append(out, path)
		case len(deeper) > 0 && e.IsDir():
			sub, err := find(path, ext, deeper...)
			out, errs = append(out, sub...), append(errs, err)
		}
	}
	return out, errors.Join(errs...)
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

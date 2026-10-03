package main

import (
	"bytes"
	"cmp"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"
)

// Root is where the index lives; harness directories are only ever read.
func Root() string {
	if dir := os.Getenv("AGENT_HISTORY_DIR"); dir != "" {
		return dir
	}
	home, _ := os.UserHomeDir()
	return filepath.Join(home, "agent-history")
}

func indexPath(harness, parent, id string) string {
	return filepath.Join(Root(), "index", harness, parent, id+".md")
}

// Format versions derived entries. Mtime alone cannot detect a changed reader,
// so reconcile rebuilds everything once when the recorded format differs.
const Format = "6" // 6: skip nested omp artifacts whose canonical parent was lost

// A harness is one coding agent whose sessions are indexed.
type harness struct {
	name     string
	sessions func() ([][]string, error)            // each session's transcript files, oldest first
	identity func(path string) (id, parent string) // from the path alone, so freshness needs no parsing
	read     func(files []string) (Session, error)
	renamed  func(id string) (time.Time, error) // when a name kept outside the transcripts changed
	running  func() (map[string]Running, error) // live sessions by ID; an error means unknown
}

var (
	claude    = harness{"claude", claudeSessions, claudeIdentity, func(f []string) (Session, error) { return ReadClaude(f[0]) }, nil, RunningClaude}
	omp       = harness{"omp", ompSessions, ompIdentity, func(f []string) (Session, error) { return ReadOmp(f[0]) }, nil, RunningOmp}
	harnesses = []harness{claude, {"codex", codexSessions, codexIdentity, ReadCodex, codexRenamed, RunningCodex}, omp}
)

// IndexTranscript indexes a Claude session and its subagents, skipping any whose
// entry is already up to date unless force is set.
func IndexTranscript(path string, force bool) error {
	// ReadDir, not Glob: a session path is literal and may contain glob syntax.
	dir := filepath.Join(strings.TrimSuffix(path, ".jsonl"), "subagents")
	files, err := os.ReadDir(dir)
	if err != nil && !errors.Is(err, os.ErrNotExist) { // most sessions have no subagents
		return err
	}
	paths := []string{path}
	for _, f := range files {
		if strings.HasSuffix(f.Name(), ".jsonl") {
			paths = append(paths, filepath.Join(dir, f.Name()))
		}
	}
	var errs []error
	for _, p := range paths {
		errs = append(errs, indexFile(claude, []string{p}, force, nil))
	}
	return errors.Join(errs...)
}

// claudeSessions lists every transcript, subagents included, so a subagent whose
// parent transcript is gone is still indexed.
func claudeSessions() ([][]string, error) {
	projects := filepath.Join(claudeDir(), "projects")
	paths, err := find(projects, ".jsonl", 2, 4)
	var out [][]string
	for _, p := range paths {
		if filepath.Dir(filepath.Dir(p)) == projects || filepath.Base(filepath.Dir(p)) == "subagents" {
			out = append(out, []string{p})
		}
	}
	return out, err
}

func indexFile(h harness, files []string, force bool, placement *ompPlacement) error {
	// An entry carries the mtime of the newest file it was built from (or of a later
	// rename), so it is fresh exactly when the two match (and a transcript rewritten to
	// an older mtime still gets rebuilt).
	var mtime time.Time
	for _, f := range files {
		src, err := os.Stat(f)
		if errors.Is(err, os.ErrNotExist) {
			continue // sessions run without persistence never write a transcript
		}
		if err != nil {
			return err
		}
		if src.ModTime().After(mtime) {
			mtime = src.ModTime()
		}
	}
	if mtime.IsZero() {
		return nil
	}
	id, parent := h.identity(files[0])
	// Both come from transcript data and name index paths, so they must be plain names.
	if !validID.MatchString(id) || (parent != "" && !validID.MatchString(parent)) {
		return fmt.Errorf("%s: not a plain session ID: %q/%q", files[0], parent, id)
	}
	if h.renamed != nil {
		renamed, err := h.renamed(id)
		if err != nil {
			return err
		}
		if renamed.After(mtime) {
			mtime = renamed
		}
	}
	dst := indexPath(h.name, parent, id)
	if idx, err := os.Stat(dst); !force && err == nil && idx.ModTime().Equal(mtime) {
		if h.name != "omp" {
			return nil
		}
		if placement == nil {
			placement = &ompPlacement{}
		}
		entry, err := readEntry(dst, false, placement)
		if err == nil && !entry.ompResumeChanged {
			return nil
		}
	}
	s, err := h.read(files)
	if errors.Is(err, errNotIndexed) {
		return nil
	}
	if err != nil {
		return fmt.Errorf("%s: %w", files[0], err)
	}
	return writeAtomic(dst, Render(s), mtime)
}

// Render is the index entry format: a front-matter header of JSON-quoted values (valid
// YAML, and one greppable `key: value` line each) followed by the human's messages.
func Render(s Session) []byte {
	var b bytes.Buffer
	b.WriteString("---\n")
	for _, f := range []struct {
		key   string
		value any
	}{
		{"harness", s.Harness}, {"session_id", s.ID}, {"parent_session", s.Parent},
		{"source", s.Source}, {"cwd", s.Cwd}, {"branches", s.Branches},
		{"entrypoint", s.Entrypoint}, {"title", s.Title}, {"started", s.Started},
		{"last_active", s.LastActive}, {"prs", s.PRs}, {"resume_argv", s.ResumeArgv},
		{"resume", ResumeLine(s.Cwd, s.ResumeArgv)},
		{"messages", len(s.Messages)},
	} {
		if v := quote(f.value); v != `""` && v != "null" {
			fmt.Fprintf(&b, "%s: %s\n", f.key, v)
		}
	}
	b.WriteString("---\n\n# " + cmp.Or(s.Title, s.ID) + "\n")
	for _, m := range s.Messages {
		fmt.Fprintf(&b, "\n## %s · %s\n\n%s\n", m.Time, m.Kind, m.Text)
	}
	return b.Bytes()
}

func quote(v any) string {
	var b bytes.Buffer
	e := json.NewEncoder(&b)
	e.SetEscapeHTML(false)
	e.Encode(v)
	return strings.TrimSpace(b.String())
}

func writeAtomic(path string, data []byte, mtime time.Time) error {
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		return err
	}
	tmp, err := os.CreateTemp(filepath.Dir(path), ".tmp-*")
	if err != nil {
		return err
	}
	defer os.Remove(tmp.Name())
	if _, err := tmp.Write(data); err != nil {
		tmp.Close()
		return err
	}
	if err := tmp.Close(); err != nil {
		return err
	}
	if err := os.Chtimes(tmp.Name(), mtime, mtime); err != nil {
		return err
	}
	return os.Rename(tmp.Name(), path)
}

// MarkMissing flags entries whose transcript the harness has deleted. The entry keeps
// working (header, messages, summary); it just can't be grepped for detail or resumed.
func MarkMissing(entry string) error {
	data, err := os.ReadFile(entry)
	if err != nil {
		return err
	}
	// Only the front matter is ours; message bodies can contain any text.
	end := bytes.Index(data, []byte("\n---\n"))
	if end < 0 || bytes.Contains(data[:end+1], []byte("\nsource_missing: true\n")) {
		return nil
	}
	for line := range strings.Lines(string(data[:end+1])) {
		raw, ok := strings.CutPrefix(line, "source: ")
		if !ok {
			continue
		}
		src, err := strconv.Unquote(strings.TrimSpace(raw))
		if err != nil {
			return fmt.Errorf("%s: bad source line: %w", entry, err)
		}
		if _, err := os.Stat(src); !errors.Is(err, os.ErrNotExist) {
			return err // present, or unknown: either way not known to be gone
		}
		// The epoch mtime matches no transcript, so one restored later gets rebuilt.
		return writeAtomic(entry, bytes.Replace(data, []byte(line), []byte(line+"source_missing: true\n"), 1), time.Unix(0, 0))
	}
	return nil
}

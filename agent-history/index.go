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

// IndexTranscript indexes a Claude session and its subagents, skipping any whose
// index entry is already newer than the transcript.
func IndexTranscript(path string) error {
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
		errs = append(errs, indexFile(p))
	}
	return errors.Join(errs...)
}

func indexFile(path string) error {
	src, err := os.Stat(path)
	if errors.Is(err, os.ErrNotExist) {
		return nil // sessions run without persistence never write a transcript
	}
	if err != nil {
		return err
	}
	id, parent := claudeIdentity(path)
	dst := indexPath("claude", parent, id)
	// An entry carries the mtime of the transcript it was built from, so it is fresh
	// exactly when the two match (and a transcript rewritten to an older mtime still
	// gets rebuilt).
	if idx, err := os.Stat(dst); err == nil && idx.ModTime().Equal(src.ModTime()) {
		return nil
	}
	s, err := ReadClaude(path)
	if err != nil {
		return fmt.Errorf("%s: %w", path, err)
	}
	return writeAtomic(dst, Render(s), src.ModTime())
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
		{"last_active", s.LastActive}, {"prs", s.PRs}, {"resume", s.Resume},
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

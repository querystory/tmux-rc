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
	subagents, _ := filepath.Glob(filepath.Join(strings.TrimSuffix(path, ".jsonl"), "subagents", "*.jsonl"))
	var errs []error
	for _, p := range append([]string{path}, subagents...) {
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
	if idx, err := os.Stat(dst); err == nil && !src.ModTime().After(idx.ModTime()) {
		return nil
	}
	s, err := ReadClaude(path)
	if err != nil {
		return fmt.Errorf("%s: %w", path, err)
	}
	return writeAtomic(dst, Render(s))
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

func writeAtomic(path string, data []byte) error {
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
	return os.Rename(tmp.Name(), path)
}

// MarkMissing flags entries whose transcript the harness has deleted. The entry keeps
// working (header, messages, summary); it just can't be grepped for detail or resumed.
func MarkMissing(entry string) error {
	data, err := os.ReadFile(entry)
	if err != nil || bytes.Contains(data, []byte("\nsource_missing: true\n")) {
		return err
	}
	for line := range strings.Lines(string(data)) {
		raw, ok := strings.CutPrefix(line, "source: ")
		if !ok {
			continue
		}
		src, err := strconv.Unquote(strings.TrimSpace(raw))
		if _, statErr := os.Stat(src); err != nil || !errors.Is(statErr, os.ErrNotExist) {
			return nil
		}
		return writeAtomic(entry, bytes.Replace(data, []byte(line), []byte(line+"source_missing: true\n"), 1))
	}
	return nil
}

package main

import (
	"cmp"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
)

// ompRecord holds the few fields we use from an omp (oh-my-pi) session line.
type ompRecord struct {
	Type      string `json:"type"`
	ID        string `json:"id"`
	Timestamp string `json:"timestamp"`
	Cwd       string `json:"cwd"`
	Title     string `json:"title"`
	Task      string `json:"task"` // session_init: the task a parent gave a subagent
	Message   struct {
		Role        string          `json:"role"`
		Attribution string          `json:"attribution"`
		Synthetic   bool            `json:"synthetic"`
		Content     json.RawMessage `json:"content"`
	} `json:"message"`
}

// ReadOmp parses an omp session: <sessions>/<cwd slug>/<started>_<id>.jsonl, with
// its subagents at <that path without .jsonl>/<agent name>.jsonl. The first line is
// a fixed-size title slot omp rewrites in place, so it holds the current title; the
// session header after it has the cwd. role "user" also carries notifications omp
// injects, which it marks synthetic or agent-attributed.
func ReadOmp(path string) (Session, error) {
	s := Session{Harness: "omp", Source: path}
	s.ID, s.Parent = ompIdentity(path)
	subagent := s.Parent != ""
	if subagent {
		s.Title = s.ID // omp names a subagent's transcript after the agent
	}
	err := scanLines(path, func(line []byte) error {
		var r ompRecord
		if json.Unmarshal(line, &r) != nil {
			return nil // a line cut short by a crash
		}
		s.seen(r.Timestamp, "", "")
		m := r.Message
		switch {
		case r.Type == "title" && !subagent:
			s.Title = r.Title
		case r.Type == "session" && s.Cwd == "":
			s.Cwd, s.Title = r.Cwd, cmp.Or(s.Title, r.Title)
		case r.Type == "session_init" && subagent && len(s.Messages) == 0:
			s.Messages = append(s.Messages, Message{r.Timestamp, "prompt", strings.TrimSpace(r.Task)})
		case r.Type == "message" && m.Role == "user" && !m.Synthetic && m.Attribution != "agent" && !subagent:
			s.said(r.Timestamp, "user", m.Content)
		}
		return nil
	})
	if err != nil {
		return Session{}, err
	}
	if !subagent && s.Cwd != "" {
		s.ResumeArgv = []string{"omp", "--resume", s.ID}
	}
	return s, nil
}

// ompIdentity reads the session ID, and a subagent's parent, from the path alone.
func ompIdentity(path string) (id, parent string) {
	name := strings.TrimSuffix(filepath.Base(path), ".jsonl")
	if filepath.Dir(filepath.Dir(path)) == ompSessionsDir() {
		_, id, _ = strings.Cut(name, "_")
		return id, ""
	}
	_, parent, _ = strings.Cut(filepath.Base(filepath.Dir(path)), "_")
	return name, parent
}

// ompSessions lists sessions and their subagents; the logs and images omp keeps
// beside them are not history.
func ompSessions() ([][]string, error) {
	paths, err := find(ompSessionsDir(), ".jsonl", 2, 3)
	var out [][]string
	for _, p := range paths {
		out = append(out, []string{p})
	}
	return out, err
}

// RunningOmp lists live omp sessions: like Codex, an omp process holds its current
// session's transcript open (and omp's idle worker processes hold none).
func RunningOmp() (map[string]Running, error) {
	return openTranscripts("omp", ompSessionsDir(), nil, func(path string) string {
		id, parent := ompIdentity(path)
		return cmp.Or(parent, id) // a subagent runs inside its parent's process
	})
}

func ompSessionsDir() string {
	dir := os.Getenv("PI_CODING_AGENT_DIR")
	if dir == "" {
		home, _ := os.UserHomeDir()
		dir = filepath.Join(home, ".omp", "agent")
	}
	return filepath.Join(dir, "sessions")
}

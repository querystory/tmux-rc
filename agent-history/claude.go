package main

import (
	"bufio"
	"bytes"
	"encoding/json"
	"errors"
	"io"
	"os"
	"path/filepath"
	"regexp"
	"slices"
	"strings"
)

// Session is the harness-neutral view of one transcript: identity, where the work
// happened, and what the human said. Agent output stays in the source file.
type Session struct {
	Harness    string
	ID         string
	Parent     string // subagents only: the session that spawned it
	Source     string
	Cwd        string
	Branches   []string
	Entrypoint string // the harness's word for how it was started; see headless
	Title      string
	Started    string
	LastActive string
	PRs        []string
	ResumeArgv []string // run in Cwd; the structured form tmux-rc builds a pane from
	Messages   []Message
}

type Message struct {
	Time string
	Kind string // how the human sent it (typed, queued, ...), or "prompt" for a subagent's task
	Text string
}

// record holds the few fields we use from a Claude Code JSONL line.
type record struct {
	Type         string `json:"type"`
	Timestamp    string `json:"timestamp"`
	Cwd          string `json:"cwd"`
	GitBranch    string `json:"gitBranch"`
	Entrypoint   string `json:"entrypoint"`
	PromptSource string `json:"promptSource"`
	Origin       struct {
		Kind string `json:"kind"`
	} `json:"origin"`
	Message struct {
		Content json.RawMessage `json:"content"`
	} `json:"message"`
	AITitle     string `json:"aiTitle"`
	CustomTitle string `json:"customTitle"`
	PRURL       string `json:"prUrl"`
}

// Tool output is most of a transcript's bytes and none of its meaning; skipping those
// lines before decoding keeps a Stop hook on an 18MB session fast.
var toolResult = []byte(`"type":"tool_result"`)

// ReadClaude parses a Claude Code transcript. Subagent transcripts live at
// <project>/<parent-session>/subagents/agent-<id>.jsonl; their first user message is
// the task the parent gave them, not something the human typed.
func ReadClaude(path string) (Session, error) {
	s := Session{Harness: "claude", Source: path}
	s.ID, s.Parent = claudeIdentity(path)
	subagent := s.Parent != ""
	if subagent {
		s.Title = subagentDescription(strings.TrimSuffix(path, ".jsonl") + ".meta.json")
	}
	aiTitle := ""
	err := scanLines(path, func(line []byte) error {
		var rec record
		if !bytes.Contains(line, toolResult) && json.Unmarshal(line, &rec) == nil {
			s.apply(rec, subagent, &aiTitle)
		}
		return nil
	})
	if err != nil {
		return Session{}, err
	}
	if s.Title == "" {
		s.Title = aiTitle
	}
	if !subagent && s.Cwd != "" {
		s.ResumeArgv = claudeResume(s.ID)
	}
	return s, nil
}

// scanLines calls fn with each line of a JSONL file, stopping at the first error.
func scanLines(path string, fn func(line []byte) error) error {
	f, err := os.Open(path)
	if err != nil {
		return err
	}
	defer f.Close()
	r := bufio.NewReader(f)
	for {
		line, err := r.ReadBytes('\n')
		if len(line) > 0 {
			if err := fn(line); err != nil {
				return err
			}
		}
		if errors.Is(err, io.EOF) {
			return nil
		}
		if err != nil {
			return err
		}
	}
}

// claudeIdentity derives the session ID, and for subagents the parent session, from
// the transcript path alone, so staleness can be checked without parsing.
func claudeIdentity(path string) (id, parent string) {
	id = strings.TrimSuffix(filepath.Base(path), ".jsonl")
	if filepath.Base(filepath.Dir(path)) == "subagents" {
		parent = filepath.Base(filepath.Dir(filepath.Dir(path)))
	}
	return id, parent
}

func (s *Session) apply(rec record, subagent bool, aiTitle *string) {
	s.seen(rec.Timestamp, rec.Cwd, rec.GitBranch)
	if s.Entrypoint == "" {
		s.Entrypoint = rec.Entrypoint
	}
	switch rec.Type {
	case "custom-title":
		s.Title = rec.CustomTitle
	case "ai-title":
		*aiTitle = rec.AITitle
	case "pr-link":
		if !slices.Contains(s.PRs, rec.PRURL) {
			s.PRs = append(s.PRs, rec.PRURL)
		}
	case "user":
		kind := rec.PromptSource
		// A headless run's prompt ("sdk") is not marked human, but it is the session's task.
		if rec.Origin.Kind != "human" && kind != "sdk" {
			// A subagent has no human; its first message, the task its parent gave it, is
			// kept as its "prompt".
			if !subagent || len(s.Messages) > 0 {
				return
			}
			kind = "prompt"
		}
		s.said(rec.Timestamp, kind, rec.Message.Content)
	}
}

// seen folds one line's context into the session: when, where, and on which branch.
func (s *Session) seen(timestamp, cwd, branch string) {
	if timestamp != "" {
		if s.Started == "" {
			s.Started = timestamp
		}
		s.LastActive = timestamp
	}
	if cwd != "" {
		s.Cwd = cwd
	}
	if branch != "" && !slices.Contains(s.Branches, branch) {
		s.Branches = append(s.Branches, branch)
	}
}

// said records a message the human (or, for a subagent, its parent) sent.
func (s *Session) said(timestamp, kind string, content json.RawMessage) {
	if text := messageText(content); text != "" {
		s.Messages = append(s.Messages, Message{Time: timestamp, Kind: kind, Text: text})
	}
}

var (
	commandName = regexp.MustCompile(`<command-name>(.*?)</command-name>`)
	commandArgs = regexp.MustCompile(`(?s)<command-args>(.*?)</command-args>`)
)

// messageText flattens message content to its text parts (images are dropped) and
// renders slash-command wrappers back into what the human typed, e.g. "/model opus".
func messageText(raw json.RawMessage) string {
	var text string
	if json.Unmarshal(raw, &text) != nil {
		var parts []struct{ Type, Text string }
		json.Unmarshal(raw, &parts)
		texts := []string{}
		for _, p := range parts {
			if p.Type == "text" {
				texts = append(texts, p.Text)
			}
		}
		text = strings.Join(texts, "\n")
	}
	if m := commandName.FindStringSubmatch(text); m != nil {
		if a := commandArgs.FindStringSubmatch(text); a != nil {
			return strings.TrimSpace(m[1] + " " + a[1])
		}
		return m[1]
	}
	return strings.TrimSpace(text)
}

func subagentDescription(metaPath string) string {
	var meta struct{ Description string }
	if b, err := os.ReadFile(metaPath); err == nil {
		json.Unmarshal(b, &meta)
	}
	return meta.Description
}

func claudeResume(id string) []string { return []string{"claude", "--resume", id} }

// ResumeLine is the copy-paste form of the resume command, for humans. Programs use
// Cwd and ResumeArgv directly and never go through a shell.
func ResumeLine(cwd string, argv []string) string {
	if len(argv) == 0 {
		return ""
	}
	words := []string{"cd", shellQuote(cwd), "&&"}
	for _, a := range argv {
		words = append(words, shellQuote(a))
	}
	return strings.Join(words, " ")
}

// shellQuote single-quotes unconditionally: resume lines are meant to be pasted into
// a shell, and a cwd is arbitrary text.
func shellQuote(s string) string {
	return "'" + strings.ReplaceAll(s, "'", `'\''`) + "'"
}

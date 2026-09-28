package main

import (
	"bufio"
	"bytes"
	"encoding/json"
	"errors"
	"io"
	"os"
	"path/filepath"
	"strings"
)

// codexRecord holds only the stable outer envelope. The rollout payload is intentionally
// decoded by event below: tool output dominates these files and is not session identity.
type codexRecord struct {
	Timestamp string          `json:"timestamp"`
	Type      string          `json:"type"`
	Payload   json.RawMessage `json:"payload"`
}

type codexMeta struct {
	ID             string          `json:"id"`
	ParentThreadID string          `json:"parent_thread_id"`
	Timestamp      string          `json:"timestamp"`
	Cwd            string          `json:"cwd"`
	Source         json.RawMessage `json:"source"`
}

func codexIdentity(path string) (id, parent string, err error) {
	f, err := os.Open(path)
	if err != nil {
		return "", "", err
	}
	defer f.Close()
	var rec codexRecord
	if err := json.NewDecoder(f).Decode(&rec); err != nil {
		return "", "", err
	}
	var meta codexMeta
	if rec.Type != "session_meta" || json.Unmarshal(rec.Payload, &meta) != nil || meta.ID == "" {
		return "", "", errors.New("codex transcript has no session_meta")
	}
	if meta.ParentThreadID != meta.ID {
		parent = meta.ParentThreadID
	}
	return meta.ID, parent, nil
}

// ReadCodex parses a Codex rollout. Human prompts come from event_msg/user_message,
// which avoids indexing duplicated response_item records and synthetic user-role
// environment/instruction envelopes. Pull URLs may come from any record, including a
// `gh pr create` result, so they are extracted before the selective JSON decode.
func ReadCodex(path string) (Session, error) {
	f, err := os.Open(path)
	if err != nil {
		return Session{}, err
	}
	defer f.Close()

	s := Session{Harness: "codex", Source: path}
	r := bufio.NewReader(f)
	for {
		line, readErr := r.ReadBytes('\n')
		if len(line) > 0 {
			if bytes.Contains(line, []byte("github.com")) {
				s.addPullRequests(string(line), false)
			}
			if bytes.Contains(line, []byte(`"type":"session_meta"`)) ||
				bytes.Contains(line, []byte(`"type":"event_msg"`)) ||
				bytes.Contains(line, []byte(`"type":"response_item"`)) {
				var rec codexRecord
				if json.Unmarshal(line, &rec) == nil {
					s.applyCodex(rec)
				}
			}
		}
		if errors.Is(readErr, io.EOF) {
			break
		}
		if readErr != nil {
			return Session{}, readErr
		}
	}
	if s.ID == "" {
		return Session{}, errors.New("codex transcript has no session_meta")
	}
	s.Title = codexTitle(path, s.ID)
	return s, nil
}

func (s *Session) applyCodex(rec codexRecord) {
	if rec.Timestamp != "" {
		if s.Started == "" {
			s.Started = rec.Timestamp
		}
		s.LastActive = rec.Timestamp
	}
	switch rec.Type {
	case "session_meta":
		var meta codexMeta
		if json.Unmarshal(rec.Payload, &meta) != nil {
			return
		}
		s.ID, s.Cwd = meta.ID, meta.Cwd
		if meta.ParentThreadID != "" && meta.ParentThreadID != meta.ID {
			s.Parent = meta.ParentThreadID
		}
		if meta.Timestamp != "" {
			s.Started = meta.Timestamp
		}
		var source string
		if json.Unmarshal(meta.Source, &source) == nil {
			s.Entrypoint = source
		}
	case "event_msg":
		var event struct {
			Type    string `json:"type"`
			Message string `json:"message"`
		}
		if json.Unmarshal(rec.Payload, &event) == nil && event.Type == "user_message" {
			text := strings.TrimSpace(event.Message)
			if text != "" {
				s.Messages = append(s.Messages, Message{
					Time: rec.Timestamp, Kind: "typed", Text: text,
				})
				s.addPullRequests(text, true)
			}
		}
	case "response_item":
		var item struct {
			Type    string `json:"type"`
			Role    string `json:"role"`
			Content []struct {
				Type string `json:"type"`
				Text string `json:"text"`
			} `json:"content"`
		}
		if json.Unmarshal(rec.Payload, &item) != nil || item.Type != "message" || item.Role != "user" {
			return
		}
		var texts []string
		for _, part := range item.Content {
			if part.Type == "input_text" && !codexEnvelope(part.Text) {
				texts = append(texts, part.Text)
			}
		}
		text := strings.TrimSpace(strings.Join(texts, "\n"))
		if text != "" {
			s.Messages = append(s.Messages, Message{
				Time: rec.Timestamp, Kind: "typed", Text: text,
			})
			s.addPullRequests(text, true)
		}
	}
}

func codexEnvelope(text string) bool {
	text = strings.TrimSpace(text)
	for _, prefix := range []string{
		"<environment_context>", "<permissions instructions>",
		"# AGENTS.md instructions for ",
	} {
		if strings.HasPrefix(text, prefix) {
			return true
		}
	}
	return false
}

// codexTitle reads the small append-only title index next to sessions. A session can be
// renamed, so the last matching record wins. Missing/malformed indexes simply mean the
// session is untitled; the transcript remains fully useful.
func codexTitle(transcript, id string) string {
	dir := filepath.Dir(transcript)
	for filepath.Base(dir) != "sessions" {
		parent := filepath.Dir(dir)
		if parent == dir {
			return ""
		}
		dir = parent
	}
	f, err := os.Open(filepath.Join(filepath.Dir(dir), "session_index.jsonl"))
	if err != nil {
		return ""
	}
	defer f.Close()
	title := ""
	scanner := bufio.NewScanner(f)
	for scanner.Scan() {
		var row struct {
			ID   string `json:"id"`
			Name string `json:"thread_name"`
		}
		if json.Unmarshal(scanner.Bytes(), &row) == nil && row.ID == id {
			title = row.Name
		}
	}
	return title
}

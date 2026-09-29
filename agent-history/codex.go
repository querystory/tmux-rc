package main

import (
	"bytes"
	"encoding/json"
	"errors"
	"io/fs"
	"os"
	"path/filepath"
	"slices"
	"strconv"
	"strings"
	"syscall"
)

// codexRecord holds the few fields we use from a Codex rollout line.
type codexRecord struct {
	Timestamp string `json:"timestamp"`
	Type      string `json:"type"`
	Payload   struct {
		Type   string          `json:"type"`
		Parent string          `json:"parent_thread_id"`
		Cwd    string          `json:"cwd"`
		Source json.RawMessage `json:"source"` // "cli", "vscode", "exec"; an object for subagents
		Git    struct {
			Branch string `json:"branch"`
		} `json:"git"`
		Message json.RawMessage `json:"message"` // user_message, in older rollouts
		Item    struct {
			Type    string          `json:"type"`
			Content json.RawMessage `json:"content"`
		} `json:"item"`
	} `json:"payload"`
}

// Rollouts are mostly model and tool traffic. Only lines carrying one of these are
// decoded; quotes inside a JSON string are escaped, so message text can't match.
var codexKept = [][]byte{[]byte(`"type":"session_meta"`), []byte(`"type":"user_message"`), []byte(`"type":"UserMessage"`)}

// errNotIndexed marks a transcript that is deliberately left out of the index.
var errNotIndexed = errors.New("not indexed")

// ReadCodex parses one Codex thread from its rollout files, oldest first: a resumed
// thread continues in a new file under the same ID. Subagent threads are left out:
// here they are approval reviews whose task is a copy of the parent's transcript.
func ReadCodex(files []string) (Session, error) {
	s := Session{Harness: "codex", Source: files[0]}
	s.ID, _ = codexIdentity(files[0])
	var last []byte
	for _, f := range files {
		err := scanLines(f, func(line []byte) error {
			last = line
			var rec codexRecord
			if !slices.ContainsFunc(codexKept, func(k []byte) bool { return bytes.Contains(line, k) }) ||
				json.Unmarshal(line, &rec) != nil {
				return nil
			}
			p := rec.Payload
			switch {
			case rec.Type == "session_meta" && p.Parent != "":
				return errNotIndexed
			case rec.Type == "session_meta":
				s.seen(rec.Timestamp, p.Cwd, p.Git.Branch)
				json.Unmarshal(p.Source, &s.Entrypoint)
			case p.Type == "user_message":
				s.said(rec.Timestamp, "user", p.Message, true, false)
			case p.Item.Type == "UserMessage":
				s.said(rec.Timestamp, "user", p.Item.Content, true, false)
			}
			return nil
		})
		if err != nil {
			return Session{}, err
		}
	}
	var tail struct{ Timestamp string }
	json.Unmarshal(last, &tail)
	s.seen(tail.Timestamp, "", "")
	s.Title = codexThreadName(s.ID)
	if s.Cwd != "" {
		s.ResumeArgv = []string{"codex", "resume", s.ID}
	}
	return s, nil
}

// codexIdentity is the thread ID in a rollout's file name,
// rollout-<YYYY-MM-DDThh-mm-ss>-<id>[_<segment>].jsonl, so it needs no parsing.
func codexIdentity(path string) (id, parent string) {
	name := strings.TrimSuffix(filepath.Base(path), ".jsonl")
	id, _, _ = strings.Cut(name[min(len(name), len("rollout-2006-01-02T15-04-05-")):], "_")
	return id, ""
}

// codexThreadName is the thread's name, which Codex keeps outside the rollout in an
// append-only log where the last entry for an ID wins. The log is the Codex harness's
// sidecar, so a rename rebuilds entries even for a thread that is no longer active.
func codexThreadName(id string) string {
	name := ""
	scanLines(codexNames(), func(line []byte) error {
		var e struct {
			ID   string `json:"id"`
			Name string `json:"thread_name"`
		}
		if json.Unmarshal(line, &e) == nil && e.ID == id {
			name = e.Name
		}
		return nil
	})
	return name
}

// codexSessions groups the rollout files by thread, oldest first. find lists
// directories in name order and rollouts are named by start time, so a thread's
// continuations always follow its first file.
func codexSessions() ([][]string, error) {
	files, err := find(codexSessionsDir(), ".jsonl", 4)
	var out [][]string
	at := map[string]int{}
	for _, f := range files {
		id, _ := codexIdentity(f)
		if i, ok := at[id]; ok {
			out[i] = append(out[i], f)
			continue
		}
		at[id] = len(out)
		out = append(out, []string{f})
	}
	return out, err
}

// RunningCodex lists live Codex threads by ID. Codex keeps no registry, but a running
// process holds its thread's rollout open, so the user's processes' open files say
// which threads are live, and where (see pane). A process
// whose files can't be read is skipped, unless it is Codex itself: then liveness is
// unknown.
func RunningCodex() (map[string]Running, error) {
	out := map[string]Running{}
	// Open files show resolved paths, so compare against the resolved directory.
	dir, err := filepath.EvalSymlinks(codexSessionsDir())
	if errors.Is(err, fs.ErrNotExist) {
		return out, nil // no sessions directory, so no rollout can be open
	}
	if err != nil {
		return nil, err
	}
	dir += string(filepath.Separator)
	procs, err := os.ReadDir("/proc")
	if err != nil {
		return nil, err
	}
	for _, p := range procs {
		pid, err := strconv.Atoi(p.Name())
		info, statErr := p.Info()
		if err != nil || statErr != nil || !owned(info) {
			continue
		}
		fds, err := os.ReadDir(filepath.Join("/proc", p.Name(), "fd"))
		if errors.Is(err, fs.ErrPermission) {
			comm, _ := os.ReadFile(filepath.Join("/proc", p.Name(), "comm"))
			if strings.TrimSpace(string(comm)) != "codex" {
				continue // not dumpable (ssh-agent, sandboxes): not ours to see
			}
		}
		if errors.Is(err, fs.ErrNotExist) {
			continue // exited while we looked
		}
		if err != nil {
			return nil, err
		}
		for _, fd := range fds {
			target, err := os.Readlink(filepath.Join("/proc", p.Name(), "fd", fd.Name()))
			if err == nil && strings.HasPrefix(target, dir) && strings.HasSuffix(target, ".jsonl") {
				id, _ := codexIdentity(target)
				out[id] = Running{PID: pid, TmuxPane: pane(pid)}
			}
		}
	}
	return out, nil
}

func owned(info fs.FileInfo) bool {
	st, ok := info.Sys().(*syscall.Stat_t)
	return ok && int(st.Uid) == os.Getuid()
}

// pane is the tmux pane a process was started in, or "" if it has since left every
// terminal: Codex's shared app-server daemon holds the rollouts of the threads its
// clients show, and the pane in its environment is just where it was first spawned.
func pane(pid int) string {
	if tty, _ := procStat(pid, 7); tty == "" || tty == "0" {
		return ""
	}
	data, _ := os.ReadFile(filepath.Join("/proc", strconv.Itoa(pid), "environ"))
	for kv := range strings.SplitSeq(string(data), "\x00") {
		if v, ok := strings.CutPrefix(kv, "TMUX_PANE="); ok {
			return v
		}
	}
	return ""
}

func codexDir() string {
	if dir := os.Getenv("CODEX_HOME"); dir != "" {
		return dir
	}
	home, _ := os.UserHomeDir()
	return filepath.Join(home, ".codex")
}

func codexNames() string { return filepath.Join(codexDir(), "session_index.jsonl") }

func codexSessionsDir() string { return filepath.Join(codexDir(), "sessions") }

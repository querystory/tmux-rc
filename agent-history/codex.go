package main

import (
	"bytes"
	"cmp"
	"encoding/json"
	"errors"
	"io"
	"io/fs"
	"os"
	"path/filepath"
	"slices"
	"strconv"
	"strings"
	"time"
)

// codexRecord holds the few fields we use from a Codex rollout line.
type codexRecord struct {
	Timestamp string `json:"timestamp"`
	Type      string `json:"type"`
	Payload   struct {
		Type   string          `json:"type"`
		Parent string          `json:"parent_thread_id"`
		Cwd    string          `json:"cwd"`
		Source json.RawMessage `json:"source"` // "cli", "vscode", "exec"; for subagents, see codexSubagent
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

// codexSubagent is a subagent thread's source: work delegated with a task
// (thread_spawn), or an approval review ("other": "guardian").
type codexSubagent struct {
	Subagent struct {
		Other       string `json:"other"`
		ThreadSpawn struct {
			AgentPath string `json:"agent_path"` // e.g. "/root/menu_grounding"
		} `json:"thread_spawn"`
	} `json:"subagent"`
}

// ReadCodex parses one Codex thread from its rollout files, oldest first: a resumed
// thread continues in a new file under the same ID. Delegated subagents are indexed
// under their parent, named by their task's path (the task itself is encrypted);
// approval reviews are left out, as their task is a copy of the parent's transcript.
func ReadCodex(files []string) (Session, error) {
	// The source is the latest file, the one retention deletes last.
	s := Session{Harness: "codex", Source: files[len(files)-1]}
	s.ID = codexThreadID(files[0])
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
			case rec.Type == "session_meta":
				var sub codexSubagent
				if json.Unmarshal(p.Source, &sub) == nil && sub.Subagent.Other == "guardian" {
					return errNotIndexed
				}
				s.Parent, s.Title = p.Parent, filepath.Base(sub.Subagent.ThreadSpawn.AgentPath)
				s.seen(rec.Timestamp, p.Cwd, p.Git.Branch)
				json.Unmarshal(p.Source, &s.Entrypoint)
			case p.Type == "user_message":
				s.said(rec.Timestamp, "user", p.Message)
			case p.Item.Type == "UserMessage":
				s.said(rec.Timestamp, "user", p.Item.Content)
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
	name, err := codexThreadName(s.ID)
	if err != nil {
		return Session{}, err
	}
	s.Title = cmp.Or(name.Name, s.Title)
	if s.Parent == "" && s.Cwd != "" {
		s.ResumeArgv = []string{"codex", "resume", s.ID}
	}
	return s, nil
}

// codexThreadID is the thread ID in a rollout's file name,
// rollout-<YYYY-MM-DDThh-mm-ss>-<id>[_<segment>].jsonl, so it needs no parsing.
func codexThreadID(path string) string {
	name := strings.TrimSuffix(filepath.Base(path), ".jsonl")
	id, _, _ := strings.Cut(name[min(len(name), len("rollout-2006-01-02T15-04-05-")):], "_")
	return id
}

// codexIdentity adds a subagent's parent, from the rollout's first line.
func codexIdentity(path string) (id, parent string) {
	scanLines(path, func(line []byte) error {
		var rec codexRecord
		json.Unmarshal(line, &rec)
		parent = rec.Payload.Parent
		return io.EOF // just the first line
	})
	return codexThreadID(path), parent
}

type codexName struct {
	Name    string    `json:"thread_name"`
	Renamed time.Time `json:"updated_at"`
}

// nameLog caches Codex's thread-name log, parsed once per version of the file.
var nameLog struct {
	path  string
	mtime time.Time
	names map[string]codexName
}

// codexThreadName is the thread's name and when it was last set. Codex keeps names
// outside the rollout, in an append-only log where the last entry for an ID wins, so
// the rename time also dates the entry: renaming an idle thread rebuilds only it.
func codexThreadName(id string) (codexName, error) {
	path := filepath.Join(codexDir(), "session_index.jsonl")
	info, err := os.Stat(path)
	if errors.Is(err, fs.ErrNotExist) {
		return codexName{}, nil // no thread has been named yet
	}
	if err != nil {
		return codexName{}, err
	}
	if path != nameLog.path || !info.ModTime().Equal(nameLog.mtime) {
		names := map[string]codexName{}
		err := scanLines(path, func(line []byte) error {
			var e struct {
				ID string `json:"id"`
				codexName
			}
			if json.Unmarshal(line, &e) == nil {
				names[e.ID] = e.codexName
			}
			return nil
		})
		if err != nil {
			return codexName{}, err // cache only a complete read
		}
		nameLog.path, nameLog.mtime, nameLog.names = path, info.ModTime(), names
	}
	return nameLog.names[id], nil
}

func codexRenamed(id string) (time.Time, error) {
	n, err := codexThreadName(id)
	return n.Renamed, err
}

// codexSessions groups the rollout files by thread, oldest first. find lists
// directories in name order and rollouts are named by start time, so a thread's
// continuations always follow its first file.
func codexSessions() ([][]string, error) {
	files, err := find(codexSessionsDir(), ".jsonl", 4)
	var out [][]string
	at := map[string]int{}
	for _, f := range files {
		id := codexThreadID(f)
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
// Codex holds its thread's rollout open, so the open files of the user's codex
// processes say which threads are live, and where (see pane). Only codex processes
// count: an editor or `tail -f` on a rollout is not the session, and neither is a
// codex-linux-sandbox helper, which runs tool commands under the name codex. A codex
// process whose files can't be read makes Codex liveness unknown.
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
		if err != nil {
			continue // not a process
		}
		// Each read runs only if the last succeeded; any failure but the process exiting
		// makes liveness unknown. comm, status and cmdline are readable whatever the
		// process's owner or dumpability, so only a real candidate's fds are read.
		proc := func(name string) (string, error) {
			b, err := os.ReadFile(filepath.Join("/proc", p.Name(), name))
			return string(b), err
		}
		comm, err := proc("comm")
		if err == nil && strings.TrimSpace(comm) != "codex" {
			continue
		}
		var status, cmdline string
		if err == nil {
			status, err = proc("status")
		}
		if err == nil {
			cmdline, err = proc("cmdline")
		}
		_, uid, _ := strings.Cut(status, "\nUid:\t") // real, effective, saved, fs
		mine := strings.HasPrefix(uid, strconv.Itoa(os.Getuid())+"\t")
		argv0, _, _ := strings.Cut(cmdline, "\x00")
		var fds []os.DirEntry
		if err == nil && mine && filepath.Base(argv0) != "codex-linux-sandbox" {
			fds, err = os.ReadDir(filepath.Join("/proc", p.Name(), "fd"))
		}
		if errors.Is(err, fs.ErrNotExist) {
			continue // exited while we looked
		}
		if err != nil {
			return nil, err
		}
		for _, fd := range fds {
			target, err := os.Readlink(filepath.Join("/proc", p.Name(), "fd", fd.Name()))
			if errors.Is(err, fs.ErrNotExist) {
				continue // closed while we looked
			}
			if err != nil {
				return nil, err
			}
			if strings.HasPrefix(target, dir) && strings.HasSuffix(target, ".jsonl") {
				out[codexThreadID(target)] = Running{PID: pid, TmuxPane: pane(pid)}
			}
		}
	}
	return out, nil
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

func codexSessionsDir() string { return filepath.Join(codexDir(), "sessions") }

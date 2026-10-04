package main

import (
	"encoding/json"
	"errors"
	"fmt"
	"io/fs"
	"maps"
	"os"
	"path/filepath"
	"strconv"
	"strings"
)

// Running is a live harness process attached to a session.
type Running struct {
	PID      int    `json:"pid"`
	TmuxPane string `json:"tmux_pane,omitempty"` // e.g. "%48"; empty when not in tmux
	Status   string `json:"status,omitempty"`    // the harness's own word: idle, busy, ...
}

// LiveSessions merges every harness's live sessions, with the errors of any harness
// that can't tell: only that harness's sessions are unknown.
func LiveSessions() (map[string]Running, map[string]error) {
	out, errs := map[string]Running{}, map[string]error{}
	for _, h := range harnesses {
		running, err := h.running()
		if err != nil {
			errs[h.name] = err
			report(fmt.Errorf("%s liveness: %w", h.name, err))
		}
		maps.Copy(out, running)
	}
	return out, errs
}

// RunningClaude lists live Claude Code sessions by session ID. Claude Code registers
// each running session in <config>/sessions/<pid>.json with its tmux pane. A file
// outlives a crashed process, and pids get reused, so an entry counts only while
// its pid is alive with the same kernel start time it registered. An error means
// liveness is unknown, which callers must not read as "nothing is running".
func RunningClaude() (map[string]Running, error) {
	out := map[string]Running{}
	files, err := find(filepath.Join(claudeDir(), "sessions"), ".json", 1)
	if err != nil {
		return nil, err
	}
	for _, f := range files {
		var reg struct {
			PID       int    `json:"pid"`
			SessionID string `json:"sessionId"`
			ProcStart string `json:"procStart"`
			Tmux      string `json:"tmux"` // "<session>:@<window>.%<pane>"
			Status    string `json:"status"`
		}
		data, err := os.ReadFile(f)
		if errors.Is(err, os.ErrNotExist) {
			continue // the process exited and removed it between listing and reading
		}
		if err != nil {
			return nil, err
		}
		// A registration we can't read (half-written, or a format change) could be a
		// live session; say so rather than drop it.
		if err := json.Unmarshal(data, &reg); err != nil || reg.SessionID == "" || reg.ProcStart == "" || reg.PID <= 0 {
			return nil, fmt.Errorf("%s: unreadable registration", f)
		}
		start, err := procStat(reg.PID, 22)
		if err != nil {
			return nil, err
		}
		if start != reg.ProcStart {
			continue // a verified mismatch: that process is gone
		}
		pane := ""
		if i := strings.LastIndex(reg.Tmux, ".%"); i >= 0 {
			pane = reg.Tmux[i+1:]
		}
		out[reg.SessionID] = Running{PID: reg.PID, TmuxPane: pane, Status: reg.Status}
	}
	return out, nil
}

// procStat is one numbered field of /proc/<pid>/stat (22 is the start time in clock
// ticks, 7 the controlling terminal), or "" if there is no such process. Any other
// failure is an error: it proves nothing about whether the process is alive. The
// command name (field 2) may contain spaces and parentheses, so fields are counted
// from its closing parenthesis.
func procStat(pid, field int) (string, error) {
	data, err := os.ReadFile(filepath.Join("/proc", strconv.Itoa(pid), "stat"))
	if errors.Is(err, os.ErrNotExist) {
		return "", nil
	}
	if err != nil {
		return "", err
	}
	i := strings.LastIndexByte(string(data), ')')
	fields := strings.Fields(string(data)[i+1:])
	if i < 0 || len(fields) < 20 {
		return "", fmt.Errorf("pid %d: unrecognized /proc stat", pid)
	}
	return fields[field-3], nil
}

// openTranscripts lists the live sessions of a harness that keeps no registry but
// holds its session's transcript open: the open files of the user's processes named
// comm say which sessions are live, and where (see pane). Only those processes count:
// an editor or `tail -f` on a transcript is not the session, and neither is a helper
// skip rejects by argv[0]. id names the session a transcript under dir belongs to ("" for
// none). A candidate whose files can't be read makes the harness's liveness unknown.
func openTranscripts(comm, dir string, skip func(argv0 string) bool, id func(path string) string) (map[string]Running, error) {
	out := map[string]Running{}
	// Open files show resolved paths, so compare against the resolved directory.
	resolved, err := filepath.EvalSymlinks(dir)
	if errors.Is(err, fs.ErrNotExist) {
		return out, nil // no sessions directory, so no transcript can be open
	}
	if err != nil {
		return nil, err
	}
	resolved += string(filepath.Separator)
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
		name, err := proc("comm")
		if err == nil && strings.TrimSpace(name) != comm {
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
		if err == nil && mine && (skip == nil || !skip(argv0)) {
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
			// Named under dir as configured, so id can tell where in it the file is.
			if rel, ok := strings.CutPrefix(target, resolved); ok && strings.HasSuffix(rel, ".jsonl") {
				if session := id(filepath.Join(dir, rel)); session != "" {
					out[session] = Running{PID: pid, TmuxPane: pane(pid)}
				}
			}
		}
	}
	return out, nil
}

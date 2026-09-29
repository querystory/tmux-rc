package main

import (
	"encoding/json"
	"errors"
	"fmt"
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

// LiveSessions merges every harness's live sessions. If any harness can't tell, the
// whole answer is unknown: session IDs don't say which harness to doubt.
func LiveSessions() (map[string]Running, error) {
	out := map[string]Running{}
	for _, h := range harnesses {
		running, err := h.running()
		if err != nil {
			return nil, fmt.Errorf("%s liveness: %w", h.name, err)
		}
		maps.Copy(out, running)
	}
	return out, nil
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

package main

import (
	"encoding/json"
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

// RunningClaude lists live Claude Code sessions by session ID. Claude Code registers
// each running session in <config>/sessions/<pid>.json with its tmux pane. A file
// outlives a crashed process, and pids get reused, so an entry counts only while
// its pid is alive with the same kernel start time it registered.
func RunningClaude() map[string]Running {
	out := map[string]Running{}
	files, _ := find(filepath.Join(claudeDir(), "sessions"), ".json", 1)
	for _, f := range files {
		var reg struct {
			PID       int    `json:"pid"`
			SessionID string `json:"sessionId"`
			ProcStart string `json:"procStart"`
			Tmux      string `json:"tmux"` // "<session>:@<window>.%<pane>"
			Status    string `json:"status"`
		}
		data, err := os.ReadFile(f)
		if err != nil || json.Unmarshal(data, &reg) != nil || reg.SessionID == "" {
			continue
		}
		if reg.ProcStart == "" || procStart(reg.PID) != reg.ProcStart {
			continue
		}
		pane := ""
		if i := strings.LastIndex(reg.Tmux, ".%"); i >= 0 {
			pane = reg.Tmux[i+1:]
		}
		out[reg.SessionID] = Running{PID: reg.PID, TmuxPane: pane, Status: reg.Status}
	}
	return out
}

// procStart is field 22 of /proc/<pid>/stat, the process start time in clock ticks,
// or "" if the process is gone. The command name (field 2) may contain spaces and
// parentheses, so fields are counted from its closing parenthesis.
func procStart(pid int) string {
	data, err := os.ReadFile(filepath.Join("/proc", strconv.Itoa(pid), "stat"))
	if err != nil {
		return ""
	}
	i := strings.LastIndexByte(string(data), ')')
	fields := strings.Fields(string(data)[i+1:])
	if i < 0 || len(fields) < 20 {
		return ""
	}
	return fields[19]
}
